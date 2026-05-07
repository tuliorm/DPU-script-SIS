"""Cliente SISDPU via Playwright headless (async).

Portado de dpu-workspace-main/dpuscript/mcp_servers/sisdpu/client.py.
Mantido como funcoes modulo-level com estado global de browser/page para
simplificar uso; concorrencia de sincronizacao e' serializada por um
asyncio.Lock em services/sync_service.py.

Credenciais vem das env vars SISDPU_USERNAME e SISDPU_PASSWORD (carregadas do
.env pelo config.py).
"""

from __future__ import annotations

import os
import re
from typing import Any

from playwright.async_api import Browser, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError
import contextlib


SISDPU_URL = "https://sisdpu.dpu.def.br/sisdpu"


class CredenciaisInvalidas(Exception):
    """SISDPU rejeitou usuario/senha no login."""
    pass


_playwright: Any = None
_browser: Browser | None = None
_page: Page | None = None


# JS que espera o PrimeFaces terminar todos os AJAX pendentes.
_WAIT_PF_AJAX = """
() => new Promise((resolve) => {
    const check = () => {
        if (typeof PrimeFaces === 'undefined'
            || !PrimeFaces.ajax
            || !PrimeFaces.ajax.Queue
            || PrimeFaces.ajax.Queue.isEmpty()) {
            resolve(true);
        } else {
            setTimeout(check, 100);
        }
    };
    check();
})
"""


def _sel(element_id: str) -> str:
    """Gera seletor CSS para IDs JSF (que contem ':')."""
    return f'[id="{element_id}"]'


async def _get_page() -> Page:
    """Retorna a page Playwright, criando browser se necessario.

    O Chromium e' iniciado com flags que FORCAM download de PDFs em vez de
    abrir o preview interno — importante pro `listar_arquivos_paj`, ja que
    queremos capturar o arquivo bruto via evento `download`, nao previsualizar.
    """
    global _playwright, _browser, _page
    if _page is not None:
        try:
            # Acessa atributo so para verificar que a page ainda esta viva;
            # se foi fechada, levanta exception e caimos no reset abaixo.
            _ = _page.url
            return _page
        except Exception:
            _page = None
            _browser = None

    if _playwright is None:
        _playwright = await async_playwright().start()

    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            # Desabilita o PDF viewer interno do Chromium: PDFs viram download
            "--disable-features=PDFViewerUpdate,PdfUnseasoned",
        ],
    )
    context = await _browser.new_context(
        viewport={"width": 1280, "height": 1024},
        accept_downloads=True,
    )
    _page = await context.new_page()
    return _page


async def _wait_pf_ajax(page: Page, timeout: int = 10000) -> None:
    """Espera o PrimeFaces terminar AJAX pendente, com timeout de seguranca."""
    try:
        await page.evaluate(_WAIT_PF_AJAX, timeout=timeout)
    except Exception:
        await page.wait_for_timeout(1000)


async def _login() -> None:
    """Faz login no SISDPU."""
    user = os.environ.get("SISDPU_USERNAME", "")
    senha = os.environ.get("SISDPU_PASSWORD", "")
    if not user or not senha:
        raise RuntimeError(
            "SISDPU_USERNAME/SISDPU_PASSWORD não definidos no ambiente (.env)"
        )
    page = await _get_page()
    await page.goto(f"{SISDPU_URL}/login.xhtml", wait_until="domcontentloaded")
    await page.wait_for_selector(_sel("frmLogin:epaj_input_usuario"), timeout=15000)
    await page.fill(_sel("frmLogin:epaj_input_usuario"), user)
    await page.fill(_sel("frmLogin:epaj_input_senha"), senha)
    await page.evaluate('PrimeFaces.ab({s:"frmLogin:loginButton",f:"frmLogin"})')
    try:
        await page.wait_for_url("**/caixaEntrada**", timeout=15000)
    except PWTimeoutError:
        # Nao chegou na caixa — checar se SISDPU recusou as credenciais.
        # PrimeFaces exibe erros em .ui-messages-error, .mensagem-erro, ou
        # um <div class="ui-growl-message"> com texto de usuario/senha.
        msg_recusa = await page.evaluate(
            """() => {
                const seletores = [
                    '.ui-messages-error-detail',
                    '.ui-messages-error',
                    '.mensagem-erro',
                    '.ui-growl-message',
                    '#frmLogin .ui-message-error',
                ];
                for (const s of seletores) {
                    const el = document.querySelector(s);
                    if (el && el.innerText && el.innerText.trim()) {
                        return el.innerText.trim();
                    }
                }
                // fallback: varrer body por frase caracteristica
                const body = (document.body.innerText || '').toLowerCase();
                const pistas = [
                    'usu\u00e1rio ou senha', 'usuario ou senha',
                    'senha incorreta', 'senha inv\u00e1lida', 'senha invalida',
                    'credenciais inv\u00e1lidas', 'credenciais invalidas',
                    'login inv\u00e1lido', 'login invalido',
                ];
                for (const p of pistas) {
                    const i = body.indexOf(p);
                    if (i >= 0) return body.substr(Math.max(0, i - 20), 120);
                }
                return null;
            }"""
        )
        if msg_recusa:
            raise CredenciaisInvalidas(msg_recusa[:200]) from None
        # Nao foi login fail explicito — repropaga como timeout.
        raise


async def _ensure_logged_in() -> None:
    """Garante que estamos logados. Refaz login se sessao expirou."""
    global _browser, _page
    page = await _get_page()
    try:
        url = page.url
        if "login" in url or not url.startswith("http"):
            await _login()
            return
        await page.goto(
            f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
            wait_until="domcontentloaded",
            timeout=10000,
        )
        await page.wait_for_timeout(500)
        if "login" in page.url:
            await _login()
    except Exception:
        try:
            if _browser:
                await _browser.close()
        except Exception:
            pass
        _browser = None
        _page = None
        await _login()


async def caixa_de_entrada() -> dict:
    """Navega para a caixa de entrada e extrai os itens listados."""
    await _ensure_logged_in()
    page = await _get_page()
    await page.goto(
        f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(2000)
    await _wait_pf_ajax(page, timeout=10000)

    body = await page.inner_text("body")

    table_data = await page.evaluate(
        """
        () => {
            const tables = document.querySelectorAll(
                '.ui-datatable-data tr, .ui-datalist-content .ui-datalist-item'
            );
            if (tables.length > 0) {
                return Array.from(tables).map(tr => tr.innerText.trim())
                    .filter(t => t.length > 0);
            }
            const allTr = document.querySelectorAll('table tbody tr');
            return Array.from(allTr).map(tr => tr.innerText.trim())
                .filter(t => t.length > 0);
        }
        """
    )

    return {
        "url": page.url,
        "itens_tabela": table_data or [],
        "texto_pagina": body[:5000],
    }


def parse_item_caixa(item_raw: str) -> dict | None:
    """Extrai {paj, oficio, assistido, data, desc, etiqueta} de uma linha da caixa.

    Aceita etiquetas SISDPU (rotulos livres que o Defensor aplica na caixa, tipo
    "Aleg finais", "Urgente", etc.) presentes como prefixo antes do PAJ. O PAJ e'
    capturado com re.search (posicional), e o texto anterior vira 'etiqueta'.

    Portado de preparar_pajs.py._parse_item.
    """
    parts = [p.strip() for p in item_raw.split("\t")]
    header = parts[0] if parts else ""
    m = re.search(r"(\d{4}/\d{3}-\d+)\s*\n?\(?\s*(.*?)\s*\)?$", header, re.DOTALL)
    if not m:
        return None
    paj = m.group(1).strip()
    oficio = m.group(2).replace("E.", "").strip()
    # Etiqueta = texto ANTES do PAJ (se houver). Strip pra limpar espacos/newlines.
    etiqueta = header[: m.start(1)].strip()
    assistido = parts[1] if len(parts) > 1 else ""
    data = parts[2] if len(parts) > 2 else ""
    descricao = parts[-1] if len(parts) >= 5 else ""
    return {
        "paj": paj,
        "oficio": oficio[:120],
        "assistido": assistido[:120],
        "data": data,
        "desc": descricao,
        "etiqueta": etiqueta[:80],
    }


async def movimentacoes_paj(numero: str, ano: str, unidade: str = "44") -> dict:
    """Retorna dados completos do PAJ incluindo MOVIMENTACOES com URLs de peca.

    Fluxo: Caixa de Entrada -> clica no TEXTO DO PAJ -> Detalhamento do Processo.
    """
    await _ensure_logged_in()
    page = await _get_page()

    await page.goto(
        f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(2000)
    await _wait_pf_ajax(page, timeout=10000)

    paj_texto = f"{ano}/{unidade.zfill(3)}-{numero}"

    clicked = await page.evaluate(
        """
        (pajTexto) => {
            const links = document.querySelectorAll('a');
            for (const a of links) {
                if (a.textContent.includes(pajTexto)) { a.click(); return true; }
            }
            return false;
        }
        """,
        paj_texto,
    )

    if not clicked:
        clicked = await page.evaluate(
            """
            (numero) => {
                const links = document.querySelectorAll('a');
                for (const a of links) {
                    if (a.textContent.includes(numero)) { a.click(); return true; }
                }
                return false;
            }
            """,
            numero,
        )

    if not clicked:
        return {"erro": f"PAJ {paj_texto} não encontrado na caixa de entrada"}

    await _wait_pf_ajax(page, timeout=15000)
    await page.wait_for_timeout(3000)

    dados = await page.evaluate(
        """
        () => {
            const r = {};
            const body = document.body.innerText;

            const assistidoMatch = body.match(
                /Assistido\\(s\\)[\\t\\s]+([^\\n]+)/
            );
            if (assistidoMatch) r.assistido = assistidoMatch[1].trim();

            const pajMatch = body.match(/(\\d{4}\\/\\d{3}-\\d+)\\s*-\\s*PAJ\\s+([^\\n]+)/);
            if (pajMatch) {
                r.paj = pajMatch[1];
                r.status_paj = pajMatch[2].trim();
            }

            const oficioMatch = body.match(/Oficio[\\t\\s]+([^\\n]+)/) ||
                                body.match(/Ofício[\\t\\s]+([^\\n]+)/);
            if (oficioMatch) r.oficio = oficioMatch[1].trim();

            // Pretensao — formato "Pretensão  Area >> Subarea >> ..."
            const pretensaoMatch = body.match(
                /Pretens[ãa]o[\\t\\s]+([^\\n]+)/
            );
            if (pretensaoMatch) r.pretensao = pretensaoMatch[1].trim();

            // Data de abertura
            const aberturaMatch = body.match(
                /(?:Data\\s+de\\s+Abertura|Dt\\.?\\s*Abertura)[\\t\\s]+([^\\n]+)/i
            );
            if (aberturaMatch) r.data_abertura = aberturaMatch[1].trim();

            const procMatch = body.match(
                /(\\d{7}-\\d{2}\\.\\d{4}\\.\\d\\.\\d{2}\\.\\d{4})/
            );
            if (procMatch) r.processo_judicial = procMatch[1];

            const juizoMatch = body.match(
                /Juízo\\/Orgão Julgador\\s*\\n\\s*([^\\n]+)/
            ) || body.match(/Juizo\\/Orgao Julgador\\s*\\n\\s*([^\\n]+)/);
            if (juizoMatch) r.juizo = juizoMatch[1].trim();

            // Tabela de movimentacoes
            const movs = [];
            const movRows = document.querySelectorAll('table tbody tr');
            for (const tr of movRows) {
                const cells = tr.querySelectorAll('td');
                if (cells.length >= 5) {
                    const seq = (cells[0]?.textContent || '').trim();
                    const dataHora = (cells[2]?.textContent || '').trim();
                    const movimentacao = (cells[3]?.textContent || '').trim();
                    const fases = (cells[4]?.textContent || '').trim();
                    const textoCompleto = cells[5]?.textContent || '';
                    const usuario = (cells[6]?.textContent || '').trim();

                    if (seq && /^\\d{1,4}$/.test(seq)
                        && (textoCompleto.trim() || movimentacao)) {
                        const urls = textoCompleto.match(/https?:\\/\\/[^\\s]+/g) || [];
                        const links_decisao = urls.filter(u =>
                            u.includes('stj.jus.br') ||
                            u.includes('stf.jus.br') ||
                            u.includes('portal.stf') ||
                            u.includes('processo.stj') ||
                            u.includes('eproc') ||
                            u.includes('downloadPeca') ||
                            u.includes('documento') ||
                            u.includes('decisao') ||
                            u.endsWith('.pdf')
                        );
                        movs.push({
                            seq: seq,
                            data: dataHora,
                            movimentacao: movimentacao,
                            fases: fases,
                            descricao: textoCompleto.substring(0, 3000),
                            links_decisao: links_decisao,
                            usuario: usuario
                        });
                    }
                }
            }
            if (movs.length > 0) r.movimentacoes = movs.slice(0, 50);

            return r;
        }
        """
    )

    if not dados:
        dados = {}
    dados["paj_numero"] = numero
    dados["paj_ano"] = ano
    dados["paj_unidade"] = unidade
    dados["url"] = page.url

    todas_urls: list[str] = []
    for m in dados.get("movimentacoes", []) or []:
        for u in m.get("links_decisao", []) or []:
            if u not in todas_urls:
                todas_urls.append(u)
    if todas_urls:
        dados["urls_decisoes"] = todas_urls

    return dados


async def baixar_arquivo(url: str, destino) -> bool:
    """Baixa arquivo reaproveitando a sessao autenticada (cookies) do SISDPU.

    Retorna True se o arquivo foi gravado com tamanho > 0.
    """
    try:
        import httpx
    except ImportError:
        return False

    page = await _get_page()
    context = page.context
    cookies = await context.cookies()
    cookie_jar = {c["name"]: c["value"] for c in cookies}

    try:
        async with httpx.AsyncClient(
            cookies=cookie_jar,
            timeout=60,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/127.0 Safari/537.36"
                ),
                "Accept": "application/pdf,*/*;q=0.8",
            },
        ) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            if not resp.content:
                return False
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(resp.content)
            return destino.stat().st_size > 0
    except Exception:
        return False


async def listar_arquivos_paj(debug: bool = False) -> dict:
    """Clica no botao "Arquivos" do PAJ atualmente aberto e extrai a lista.

    PRE-CONDICAO: a page ja deve estar no detalhamento do PAJ (chamar
    `movimentacoes_paj(...)` antes). Nao navega — so interage com a pagina atual.

    Retorna dict:
        {
            "ok": bool,
            "itens": [{tipo, descricao, arquivo, row_index, todas_celulas}, ...],
            "diag": {...}   # sempre presente; use pra debug quando itens == []
        }
    `diag` inclui: candidatos_botao_arquivos, clicou, dialogs_visiveis,
    dialog_escolhido, total_linhas, linhas_filtradas_por_texto, html_snippet.
    """
    page = await _get_page()

    # 1) Inventariar candidatos ao botao "Arquivos" ANTES de clicar
    diag: dict = {}
    diag["candidatos_botao_arquivos"] = await page.evaluate(
        """
        () => {
            const out = [];
            const els = Array.from(document.querySelectorAll('a, button, span[role="button"], .ui-button'));
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (txt && /arquivo/i.test(txt) && txt.length < 40) {
                    out.push({
                        tag: el.tagName,
                        texto: txt,
                        id: el.id || '',
                        classes: el.className || '',
                        onclick_has: !!el.getAttribute('onclick'),
                    });
                }
            }
            return out;
        }
        """
    )

    # 2) Clica no link "Arquivos" — agora mais tolerante
    clicked = await page.evaluate(
        """
        () => {
            const els = Array.from(document.querySelectorAll('a, button, span[role="button"], .ui-button'));
            // Preferencia: match EXATO por "Arquivos" depois de trim
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (txt === 'Arquivos' || txt === 'Arquivo(s)') {
                    el.click();
                    return 'exato';
                }
            }
            // Fallback: contem "Arquivos" (mas nao "Arquivo anexo" de movimentacao)
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (/^Arquivos?$/i.test(txt) || /^Arquivos\\s*\\(\\d+\\)$/.test(txt)) {
                    el.click();
                    return 'regex';
                }
            }
            // Fallback extremo: qualquer elemento com "Arquivo" curto
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (/arquivo/i.test(txt) && txt.length < 15) {
                    el.click();
                    return 'fuzzy:' + txt;
                }
            }
            return '';
        }
        """
    )
    diag["clicou"] = clicked or "NADA"
    if not clicked:
        return {"ok": False, "itens": [], "diag": diag}

    # Aguarda AJAX + eventual navegacao (o link "Arquivos" navega para outra tela).
    # Tenta networkidle primeiro; se falhar, cai pra wait_pf_ajax + sleep.
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        await _wait_pf_ajax(page, timeout=15000)
    await page.wait_for_timeout(1500)

    # 3) Inventariar dialogs REALMENTE visiveis (filtra aria-hidden=true)
    diag["dialogs_visiveis"] = await page.evaluate(
        """
        () => {
            const dialogs = document.querySelectorAll('.ui-dialog');
            return Array.from(dialogs)
                .filter(d => {
                    if (d.getAttribute('aria-hidden') === 'true') return false;
                    if (d.classList.contains('ui-overlay-hidden')) return false;
                    const st = window.getComputedStyle(d);
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    return true;
                })
                .map(d => ({
                    id: d.id || '',
                    titulo: (d.querySelector('.ui-dialog-title')?.textContent || '').trim(),
                    linhas_tabela: d.querySelectorAll('table tbody tr').length,
                }));
        }
        """
    )

    # Registra URL pra saber se navegou
    with contextlib.suppress(Exception):
        diag["url_apos_click"] = page.url

    # 4) Extrai linhas — a pagina de Arquivos do SISDPU tem MULTIPLAS tabelas,
    # uma por categoria ("Juntada de documento", "Concluso ao defensor",
    # "Oficio/Carta/Comunicacao expedida", etc.). Iteramos TODAS as tabelas
    # visiveis com botoes "Visualizar" em ordem do DOM e concatenamos as linhas
    # com indice global. A funcao baixar_anexo_por_indice usa a MESMA estrategia
    # de flatten para que `row_index` seja consistente.
    extrato = await page.evaluate(
        """
        () => {
            const diag = {estrategia: '', total_linhas: 0, linhas_filtradas: 0,
                          tabelas_analisadas: 0, tabelas_com_visualizar: 0,
                          categorias: []};

            const isVisivel = (el) => {
                let cur = el;
                while (cur) {
                    if (cur.nodeType === 1) {
                        if (cur.getAttribute && cur.getAttribute('aria-hidden') === 'true') return false;
                        if (cur.classList && (
                            cur.classList.contains('ui-overlay-hidden') ||
                            cur.classList.contains('ui-hidden-container')
                        )) {
                            if (cur.getAttribute('aria-hidden') === 'true') return false;
                        }
                    }
                    cur = cur.parentElement;
                }
                try {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                } catch (e) {}
                return true;
            };

            // Tenta extrair o rotulo da categoria de uma tabela: o texto curto
            // mais proximo (header/legend/div) que aparece imediatamente antes.
            const detectaCategoria = (tbl) => {
                let prev = tbl.previousElementSibling;
                let tentativas = 0;
                while (prev && tentativas < 6) {
                    const t = (prev.textContent || '').trim();
                    if (t && t.length < 100 && t.length > 2 && !/^(Seq\\.|Nenhum registro)/i.test(t)) {
                        return t.split('\\n')[0].trim().slice(0, 80);
                    }
                    prev = prev.previousElementSibling;
                    tentativas++;
                }
                // Fallback: legend de fieldset ancestral
                let p = tbl.parentElement;
                while (p) {
                    const lg = p.querySelector(':scope > legend, :scope > .ui-fieldset-legend');
                    if (lg) {
                        const t = (lg.textContent || '').trim();
                        if (t) return t.slice(0, 80);
                    }
                    p = p.parentElement;
                }
                return '';
            };

            const tables = document.querySelectorAll('table');
            diag.tabelas_analisadas = tables.length;

            // Coleta todas as tabelas uteis (visiveis, com >= 1 linha "Visualizar")
            const uteis = [];
            for (const tbl of tables) {
                const rows = tbl.querySelectorAll('tbody tr');
                if (rows.length === 0) continue;
                let comVis = 0;
                for (const r of rows) {
                    const alvos = r.querySelectorAll('a, button, span[role="button"]');
                    for (const a of alvos) {
                        if (/visualizar/i.test((a.textContent || '').trim())) { comVis++; break; }
                    }
                }
                if (comVis === 0) continue;
                if (!isVisivel(tbl)) continue;
                uteis.push(tbl);
            }
            diag.tabelas_com_visualizar = uteis.length;

            if (uteis.length === 0) {
                // Fallback: tabelas visiveis cujos headers mencionem "arquivo"
                for (const tbl of tables) {
                    const ths = Array.from(tbl.querySelectorAll('th')).map(th => (th.textContent || '').toLowerCase());
                    if (!/arquivo|documento/.test(ths.join('|'))) continue;
                    if (!tbl.querySelector('tbody tr')) continue;
                    if (!isVisivel(tbl)) continue;
                    uteis.push(tbl);
                }
                if (uteis.length > 0) diag.estrategia = 'tabela_por_header';
            } else {
                diag.estrategia = uteis.length > 1
                    ? `multi_tabela_visualizar (${uteis.length})`
                    : 'tabela_com_visualizar';
            }

            if (uteis.length === 0) {
                diag.erro = 'nenhuma_tabela_util_encontrada';
                return {resultado: [], diag};
            }

            // Flatten: percorre todas as tabelas em ordem do DOM e concatena as
            // linhas com indice global crescente. A categoria de cada tabela e'
            // anexada a cada linha — util pra naming/diagnostico.
            const resultado = [];
            let idx = 0;
            const snippets = [];
            for (const tbl of uteis) {
                const categoria = detectaCategoria(tbl);
                diag.categorias.push({categoria: categoria, linhas: 0});
                const catIdx = diag.categorias.length - 1;
                const rows = tbl.querySelectorAll('tbody tr');
                diag.total_linhas += rows.length;
                for (const tr of rows) {
                    if (tr.classList.contains('ui-widget-header')) continue;
                    const cells = tr.querySelectorAll('td');
                    if (cells.length === 0) continue;
                    const textos = Array.from(cells).map(c => (c.textContent || '').trim());
                    const preenchidas = textos.filter(t => t.length > 0).length;
                    if (preenchidas < 1) continue;
                    const alvos = tr.querySelectorAll('a, button, span[role="button"]');
                    let temVis = false;
                    for (const a of alvos) {
                        if (/visualizar/i.test((a.textContent || '').trim())) { temVis = true; break; }
                    }
                    if (!temVis) continue;
                    resultado.push({
                        row_index: idx++,
                        tipo: textos[0] || '',
                        descricao: textos[1] || '',
                        arquivo: textos[2] || '',
                        categoria: categoria,
                        todas_celulas: textos,
                    });
                    diag.categorias[catIdx].linhas++;
                }
                try { snippets.push((tbl.outerHTML || '').slice(0, 800)); } catch (e) {}
            }
            diag.linhas_filtradas = resultado.length;
            diag.html_snippet = snippets.join('\\n---\\n').slice(0, 2000);
            return {resultado, diag};
        }
        """
    )

    diag.update(extrato.get("diag", {}))
    itens = extrato.get("resultado", []) or []
    return {"ok": True, "itens": itens, "diag": diag}


async def baixar_anexo_por_indice(row_index: int, destino):
    """Clica em "Visualizar" da linha `row_index` e salva o arquivo baixado.

    Com o preview de PDF desabilitado no Chromium, todos os tipos disparam
    evento `download`. Usa expect_download com timeout generoso.

    `destino` pode ser:
      - Path com extensao: salva exatamente ali.
      - Path SEM extensao (stem): a extensao e' detectada do
        `download.suggested_filename` (Content-Disposition do SISDPU) e
        apensa ao stem. E' a forma recomendada pra nao forcar `.bin` quando
        o scraper nao tinha a extensao do arquivo na tela.

    Retorna Path do arquivo salvo (com extensao final) ou None se falhou.
    """
    from pathlib import Path as _Path
    destino = _Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)

    page = await _get_page()

    # Identifica o botao "Visualizar" da linha `idx` usando a MESMA estrategia
    # de flatten de listar_arquivos_paj: itera TODAS as tabelas visiveis com
    # linhas "Visualizar" em ordem do DOM e seleciona a `idx`-esima linha
    # (indice global). Para PAJs com multiplas categorias na pagina de Arquivos
    # (ex: "Concluso ao defensor", "Juntada de documento", "Despacho", etc.),
    # cada tabela contribui suas linhas na ordem em que aparece no DOM.
    found = await page.evaluate(
        """
        (idx) => {
            const isVisivel = (el) => {
                let cur = el;
                while (cur) {
                    if (cur.nodeType === 1) {
                        if (cur.getAttribute && cur.getAttribute('aria-hidden') === 'true') return false;
                    }
                    cur = cur.parentElement;
                }
                try {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                } catch (e) {}
                return true;
            };

            const tables = document.querySelectorAll('table');
            // Coleta tabelas uteis em ordem do DOM (mesma logica de listar_arquivos_paj)
            const uteis = [];
            for (const tbl of tables) {
                const rows = tbl.querySelectorAll('tbody tr');
                if (rows.length === 0) continue;
                let comVis = 0;
                for (const r of rows) {
                    const alvos = r.querySelectorAll('a, button, span[role="button"]');
                    for (const a of alvos) {
                        if (/visualizar/i.test((a.textContent || '').trim())) { comVis++; break; }
                    }
                }
                if (comVis === 0) continue;
                if (!isVisivel(tbl)) continue;
                uteis.push(tbl);
            }
            if (uteis.length === 0) return false;

            // Flatten: concatena linhas de todas as tabelas em ordem
            const linhas = [];
            for (const tbl of uteis) {
                const rows = Array.from(tbl.querySelectorAll('tbody tr'))
                    .filter(tr => {
                        if (tr.classList.contains('ui-widget-header')) return false;
                        if (tr.querySelectorAll('td').length === 0) return false;
                        const alvos = tr.querySelectorAll('a, button, span[role="button"]');
                        for (const a of alvos) {
                            if (/visualizar/i.test((a.textContent || '').trim())) return true;
                        }
                        return false;
                    });
                for (const r of rows) linhas.push(r);
            }
            if (idx >= linhas.length) return false;
            const row = linhas[idx];
            const alvos = row.querySelectorAll('a, button, span[role="button"]');
            for (const el of alvos) {
                if (/visualizar/i.test((el.textContent || '').trim())) {
                    el.setAttribute('data-dpu-pick', '1');
                    return true;
                }
            }
            return false;
        }
        """,
        row_index,
    )
    if not found:
        return None

    def _limpar_marker():
        try:
            return page.evaluate(
                "() => document.querySelectorAll('[data-dpu-pick]')"
                ".forEach(el => el.removeAttribute('data-dpu-pick'))"
            )
        except Exception:
            pass

    try:
        async with page.expect_event("download", timeout=60000) as dl_info:
            await page.click('[data-dpu-pick="1"]')
        download = await dl_info.value

        # Detecta extensao a partir do suggested_filename (Content-Disposition do SISDPU)
        sugerido = (download.suggested_filename or "").strip()
        ext_sugerida = _Path(sugerido).suffix.lower() if sugerido else ""
        # Nomes tipicos do SISDPU: "documento.pdf", "foto.jpg", "contrato.docx"
        if not ext_sugerida:
            ext_sugerida = ".bin"

        # Se o destino passado NAO tem extensao (ou tem .bin como placeholder),
        # usa a extensao detectada. Caso contrario respeita o que veio.
        if destino.suffix == "" or destino.suffix.lower() == ".bin":
            destino_final = destino.with_suffix(ext_sugerida)
        else:
            destino_final = destino

        await download.save_as(str(destino_final))
    except Exception:
        await _limpar_marker()
        return None

    await _limpar_marker()

    if destino_final.exists() and destino_final.stat().st_size > 0:
        return destino_final
    return None


async def fechar_dialogo_arquivos() -> None:
    """Fecha o dialogo 'Arquivos' se estiver aberto (para nao interferir em outro PAJ)."""
    page = await _get_page()
    try:
        await page.evaluate(
            """
            () => {
                const dialogs = document.querySelectorAll('.ui-dialog:not(.ui-overlay-hidden)');
                for (const d of dialogs) {
                    const titulo = d.querySelector('.ui-dialog-title');
                    if (titulo && /arquivo/i.test(titulo.textContent || '')) {
                        const btn = d.querySelector('.ui-dialog-titlebar-close');
                        if (btn) btn.click();
                    }
                }
            }
            """
        )
        await page.wait_for_timeout(500)
    except Exception:
        pass


async def fechar() -> None:
    """Fecha o navegador headless."""
    global _playwright, _browser, _page
    if _browser:
        with contextlib.suppress(Exception):
            await _browser.close()
        _browser = None
        _page = None
    if _playwright:
        with contextlib.suppress(Exception):
            await _playwright.stop()
        _playwright = None


if __name__ == "__main__":
    import asyncio
    import json

    async def _smoke() -> None:
        # Carrega .env do projeto para expor SISDPU_USERNAME/PASSWORD
        from pathlib import Path

        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env")

        try:
            data = await caixa_de_entrada()
            itens = data.get("itens_tabela", [])[:5]
            print(json.dumps(itens, ensure_ascii=False, indent=2))
            print(f"(+{len(data.get('itens_tabela', [])) - len(itens)} itens)")
        finally:
            await fechar()

    asyncio.run(_smoke())
