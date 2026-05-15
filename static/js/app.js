/* oficio-geral-ui — utilidades globais */

/* Watchlist toggle — usado pelo paj_detail.html como x-data="watchlistToggle('PAJ-...')".
   Verifica se o PAJ esta na watchlist e expoe acoes adicionar/remover. */
function watchlistToggle(pajNorm) {
    return {
        watched: false,
        item: null,
        loading: false,

        async init() {
            await this.refresh();
        },

        async refresh() {
            try {
                const r = await fetch('/api/watchlist/check/' + encodeURIComponent(pajNorm));
                const data = await r.json();
                this.watched = !!data.watched;
                this.item = data.item || null;
            } catch (e) {
                this.watched = false;
                this.item = null;
            }
        },

        async adicionar() {
            this.loading = true;
            try {
                const r = await fetch('/api/watchlist', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paj_norm: pajNorm, motivo: '' }),
                });
                if (!r.ok) {
                    const err = await r.json().catch(() => ({}));
                    showToast('Erro: ' + (err.detail || 'falha ao adicionar'), 'error');
                    return;
                }
                showToast('Adicionado à watchlist', 'success');
                await this.refresh();
            } catch (e) {
                showToast('Falha de rede', 'error');
            } finally {
                this.loading = false;
            }
        },

        async remover() {
            if (!confirm('Remover ' + pajNorm + ' da watchlist?')) return;
            this.loading = true;
            try {
                const r = await fetch('/api/watchlist/' + encodeURIComponent(pajNorm), { method: 'DELETE' });
                if (!r.ok) {
                    showToast('Erro ao remover', 'error');
                    return;
                }
                showToast('Removido da watchlist', 'info');
                await this.refresh();
            } catch (e) {
                showToast('Falha de rede', 'error');
            } finally {
                this.loading = false;
            }
        },
    };
}

function showToast(msg, type) {
    type = type || 'info';
    const container = document.getElementById('toast-container');
    if (!container) return;

    const alertClass = {
        'success': 'alert-success',
        'error': 'alert-error',
        'warning': 'alert-warning',
        'info': 'alert-info'
    }[type] || 'alert-info';

    let el = document.createElement('div');
    el.className = 'alert ' + alertClass + ' text-sm py-2 px-4 shadow-lg';
    el.innerHTML = '<span>' + msg + '</span>';
    container.appendChild(el);

    setTimeout(function() {
        el.style.opacity = '0';
        el.style.transition = 'opacity 0.3s';
        setTimeout(function() { el.remove(); }, 300);
    }, 3000);
}

/* HTMX global event handlers */
document.addEventListener('htmx:responseError', function(evt) {
    showToast('Erro na requisição: ' + evt.detail.xhr.status, 'error');
});

/* Sincronizacao da caixa SISDPU — streaming SSE */
let _syncSource = null;

function _syncBtnCancel(show) {
    let btn = document.getElementById('sync-cancel-btn');
    if (btn) btn.style.display = show ? '' : 'none';
}

function cancelarSync() {
    let statusEl = document.getElementById('sync-status');
    let btn = document.getElementById('sync-cancel-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'cancelando...'; }
    if (statusEl) {
        statusEl.textContent = 'cancelando...';
        statusEl.className = 'badge badge-sm badge-warning';
    }
    fetch('/api/sync/cancel', {method: 'POST'})
        .then(function(r) { return r.json(); })
        .then(function(j) {
            if (!j.ok) {
                showToast('Sem sync rodando pra cancelar', 'info');
                if (btn) { btn.disabled = false; btn.textContent = 'Cancelar'; }
            } else {
                showToast('Cancelamento solicitado — aguardando o PAJ atual terminar', 'warning');
            }
        })
        .catch(function() {
            showToast('Falha ao pedir cancelamento', 'error');
            if (btn) { btn.disabled = false; btn.textContent = 'Cancelar'; }
        });
}

function sincronizarCaixa(baixarAnexos) {
    if (typeof baixarAnexos === 'undefined') baixarAnexos = true;
    let modal = document.getElementById('sync-modal');
    let logEl = document.getElementById('sync-log');
    let statusEl = document.getElementById('sync-status');
    let closeBtn = document.getElementById('sync-close-btn');
    if (!modal || !logEl) return;

    logEl.textContent = '';
    statusEl.textContent = baixarAnexos ? 'sincronizando...' : 'sincronizando (rápido)...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    const cancelBtn = document.getElementById('sync-cancel-btn');
    if (cancelBtn) { cancelBtn.disabled = false; cancelBtn.textContent = 'Cancelar'; }
    _syncBtnCancel(true);
    modal.showModal();

    if (_syncSource) {
        _syncSource.close();
        _syncSource = null;
    }

    let url = baixarAnexos ? '/api/sync' : '/api/sync?anexos=0';
    _syncSource = new EventSource(url);

    _syncSource.addEventListener('log', function(e) {
        logEl.textContent += e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    });

    _syncSource.addEventListener('done', function(e) {
        logEl.textContent += '\n' + e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = 'concluído';
        statusEl.className = 'badge badge-sm badge-success';
        closeBtn.disabled = false;
        _syncBtnCancel(false);
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
        showToast('Caixa sincronizada — recarregando dashboard', 'success');
        setTimeout(function() {
            if (!modal.open) {
                window.location.reload();
            } else {
                modal.addEventListener('close', function once() {
                    modal.removeEventListener('close', once);
                    window.location.reload();
                });
            }
        }, 500);
    });

    _syncSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        _syncBtnCancel(false);
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
    };
}

/* Sincronizacao de UM unico PAJ — mesmo modal, endpoint /api/sync/paj/{paj} */
function sincronizarPaj(pajNorm, baixarAnexos) {
    if (typeof baixarAnexos === 'undefined') baixarAnexos = true;
    let modal = document.getElementById('sync-modal');
    let logEl = document.getElementById('sync-log');
    let statusEl = document.getElementById('sync-status');
    let closeBtn = document.getElementById('sync-close-btn');
    if (!modal || !logEl) {
        showToast('Modal de sync não encontrado na página', 'error');
        return;
    }

    logEl.textContent = '';
    statusEl.textContent = (baixarAnexos ? 'sincronizando ' : 'sincronizando (rápido) ') + pajNorm + '...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    const cancelBtn2 = document.getElementById('sync-cancel-btn');
    if (cancelBtn2) { cancelBtn2.disabled = false; cancelBtn2.textContent = 'Cancelar'; }
    _syncBtnCancel(true);
    modal.showModal();

    if (_syncSource) {
        _syncSource.close();
        _syncSource = null;
    }

    let url = '/api/sync/paj/' + encodeURIComponent(pajNorm);
    if (!baixarAnexos) url += '?anexos=0';
    _syncSource = new EventSource(url);

    _syncSource.addEventListener('log', function(e) {
        logEl.textContent += e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    });

    _syncSource.addEventListener('done', function(e) {
        logEl.textContent += '\n' + e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = 'concluído';
        statusEl.className = 'badge badge-sm badge-success';
        closeBtn.disabled = false;
        _syncBtnCancel(false);
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
        showToast('PAJ sincronizado — recarregando página', 'success');
        setTimeout(function() {
            if (!modal.open) {
                window.location.reload();
            } else {
                modal.addEventListener('close', function once() {
                    modal.removeEventListener('close', once);
                    window.location.reload();
                });
            }
        }, 500);
    });

    _syncSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        _syncBtnCancel(false);
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
    };
}

/* Baixar anexos antigos a partir de uma data — completa overflow do sync padrao.
   Reusa o modal de sync (sync-modal) para o log SSE. */
function baixarAnexosDesde(pajNorm) {
    let input = document.getElementById('anexos-desde-data');
    let data = (input && input.value || '').trim();
    if (!data) {
        showToast('Escolha uma data de corte', 'warning');
        return;
    }

    let modal = document.getElementById('sync-modal');
    let logEl = document.getElementById('sync-log');
    let statusEl = document.getElementById('sync-status');
    let titleEl = document.getElementById('sync-title');
    let closeBtn = document.getElementById('sync-close-btn');
    if (!modal || !logEl) {
        showToast('Modal de sync não encontrado na página', 'error');
        return;
    }

    logEl.textContent = '';
    if (titleEl) titleEl.textContent = 'Baixar anexos a partir de ' + data;
    statusEl.textContent = 'baixando...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    _syncBtnCancel(false);  // este endpoint nao suporta cancel — esconde o botao
    modal.showModal();

    if (_syncSource) { _syncSource.close(); _syncSource = null; }

    let url = '/api/sync/paj/' + encodeURIComponent(pajNorm)
            + '/anexos-desde?data=' + encodeURIComponent(data);
    _syncSource = new EventSource(url);

    _syncSource.addEventListener('log', function(e) {
        logEl.textContent += e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    });

    _syncSource.addEventListener('done', function(e) {
        logEl.textContent += '\n' + e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = 'concluído';
        statusEl.className = 'badge badge-sm badge-success';
        closeBtn.disabled = false;
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
        showToast('Anexos baixados — recarregando página', 'success');
        setTimeout(function() {
            if (!modal.open) {
                window.location.reload();
            } else {
                modal.addEventListener('close', function once() {
                    modal.removeEventListener('close', once);
                    window.location.reload();
                });
            }
        }, 500);
    });

    _syncSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
    };
}

/* Docgen — streaming SSE do gerar_docx.py / gerar_peticao.py */
let _docgenSource = null;

function runDocgen(pajNorm, arquivo, formato, titulo) {
    let modal = document.getElementById('docgen-modal');
    let logEl = document.getElementById('docgen-log');
    let statusEl = document.getElementById('docgen-status');
    let titleEl = document.getElementById('docgen-title');
    let closeBtn = document.getElementById('docgen-close-btn');

    if (!modal || !logEl) return;

    // Reset
    logEl.textContent = '';
    titleEl.textContent = titulo || ('Gerar ' + formato.toUpperCase());
    statusEl.textContent = 'gerando...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    modal.showModal();

    if (_docgenSource) {
        _docgenSource.close();
        _docgenSource = null;
    }

    let url = '/api/gerar/' + encodeURIComponent(pajNorm)
              + '?arquivo=' + encodeURIComponent(arquivo)
              + '&formato=' + encodeURIComponent(formato);

    _docgenSource = new EventSource(url);

    _docgenSource.addEventListener('log', function(e) {
        logEl.textContent += e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    });

    _docgenSource.addEventListener('done', function(e) {
        logEl.textContent += '\n' + e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = 'concluído';
        statusEl.className = 'badge badge-sm badge-success';
        closeBtn.disabled = false;
        _docgenSource.close();
        _docgenSource = null;
        showToast(formato.toUpperCase() + ' gerado com sucesso', 'success');
    });

    _docgenSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        if (_docgenSource) {
            _docgenSource.close();
            _docgenSource = null;
        }
    };
}

/* Elaborar Peca (background + polling + modal global) */

// Guarda o PAJ atual do modal pra funcoes auxiliares
let _resumoPajAtual = null;

async function abrirResumo(pajNorm) {
    _resumoPajAtual = pajNorm;
    const modal = document.getElementById('resumo-modal');
    const content = document.getElementById('resumo-content');
    const label = document.getElementById('resumo-paj-label');
    const linkPaj = document.getElementById('resumo-abrir-paj');
    if (!modal || !content) return;

    if (label) label.textContent = pajNorm;
    if (linkPaj) linkPaj.href = '/paj/' + pajNorm;

    try {
        const resp = await fetch('/api/elaborar/status/' + pajNorm);
        const data = await resp.json();
        content.textContent = data.summary || '(resumo vazio — elaboração ainda não concluída?)';
    } catch (e) {
        content.textContent = 'Erro ao carregar resumo: ' + e.message;
    }

    modal.showModal();
}

async function enviarCorrecaoAtual() {
    if (!_resumoPajAtual) {
        showToast('Nenhum PAJ ativo', 'warning');
        return;
    }
    await enviarCorrecao(_resumoPajAtual);
}

function elaborarApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        status: 'idle',
        lastAction: '',
        summary: '',
        error: '',
        _pollTimer: null,
        // Skills disponiveis no Oficio Geral (carregadas via /api/skills)
        skills: [],
        grupos: [],
        skillSlug: '',        // '' = "Claude decide"
        skillsLoading: true,

        async init() {
            await Promise.all([this.fetchStatus(), this.carregarSkills()]);
            if (this.status === 'running') {
                this.startPolling();
            }
        },

        async carregarSkills() {
            try {
                const resp = await fetch('/api/skills?paj=' + encodeURIComponent(this.pajNorm));
                const data = await resp.json();
                this.skills = data.skills || [];
                // grupos na ordem em que aparecem, preservando unicidade
                const vistos = new Set();
                this.grupos = [];
                for (const s of this.skills) {
                    if (!vistos.has(s.grupo)) {
                        vistos.add(s.grupo);
                        this.grupos.push(s.grupo);
                    }
                }
                // Pre-seleciona a primeira skill destacada (da area do PAJ)
                const destacada = this.skills.find(s => s.destaque);
                if (destacada) {
                    this.skillSlug = destacada.slug;
                }
            } catch (e) {
                console.warn('Falha ao carregar skills:', e);
            } finally {
                this.skillsLoading = false;
            }
        },

        skillsDoGrupo(grupo) {
            // Destacadas primeiro dentro do grupo, resto depois
            const todasDoGrupo = this.skills.filter(s => s.grupo === grupo);
            return todasDoGrupo.sort((a, b) => (b.destaque ? 1 : 0) - (a.destaque ? 1 : 0));
        },

        async fetchStatus() {
            try {
                const resp = await fetch('/api/elaborar/status/' + this.pajNorm);
                const data = await resp.json();
                this.status = data.status || 'idle';
                this.lastAction = data.last_action || '';
                this.summary = data.summary || '';
                this.error = data.error || '';
            } catch (e) {
                this.error = 'Erro ao consultar status: ' + e.message;
                this.status = 'error';
            }
        },

        async iniciar() {
            this.status = 'running';
            this.lastAction = this.skillSlug ? ('invocando /' + this.skillSlug) : 'iniciando...';
            this.error = '';
            try {
                await fetch('/api/elaborar/start/' + this.pajNorm, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({skill: this.skillSlug || ''}),
                });
                const msg = this.skillSlug
                    ? ('Claude iniciou — skill /' + this.skillSlug)
                    : 'Claude iniciou a elaboração';
                showToast(msg + ' — pode navegar livremente', 'info');
                this.startPolling();
            } catch (e) {
                this.status = 'error';
                this.error = 'Falha ao iniciar: ' + e.message;
            }
        },

        startPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                await this.fetchStatus();
                if (this.status === 'done' || this.status === 'error' || this.status === 'idle') {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    if (this.status === 'done') {
                        // startPolling() so' roda quando a elaboracao foi
                        // iniciada nesta sessao — transicao running -> done
                        // significa peca recem-gerada. Recarrega pra atualizar
                        // as abas Pecas/Despachos (SSR) com os novos arquivos.
                        showToast('Peça elaborada — recarregando para exibir arquivos gerados', 'success');
                        setTimeout(() => window.location.reload(), 1200);
                    }
                }
            }, 2000);
        },

        verResumo() {
            abrirResumo(this.pajNorm);
        }
    };
}

/** Dispara o polling do componente elaborarApp presente na pagina (se houver). */
function _triggerElaborarPolling() {
    const el = document.querySelector('[x-data^="elaborarApp"]');
    if (!el) return;
    // Alpine v3 expoe os dados via _x_dataStack
    const data = el._x_dataStack && el._x_dataStack[0];
    if (data && typeof data.startPolling === 'function') {
        data.status = 'running';
        data.startPolling();
    }
}

let _correcaoDiretaPajAtual = null;

function abrirCorrecaoDireta(pajNorm, nomeArquivo) {
    _correcaoDiretaPajAtual = pajNorm;
    const modal = document.getElementById('correcao-direta-modal');
    if (!modal) {
        showToast('Modal de correção não encontrado', 'error');
        return;
    }
    const label = document.getElementById('correcao-direta-paj-label');
    if (label) label.textContent = pajNorm;
    const ctx = document.getElementById('correcao-direta-contexto');
    if (ctx) {
        ctx.textContent = nomeArquivo
            ? ('Arquivo de referência: ' + nomeArquivo)
            : '';
    }
    const ta = document.getElementById('correcao-direta-text');
    if (ta) ta.value = '';
    modal.showModal();
    setTimeout(() => ta && ta.focus(), 50);
}

async function enviarCorrecaoDireta() {
    const pajNorm = _correcaoDiretaPajAtual;
    if (!pajNorm) {
        showToast('PAJ não identificado', 'warning');
        return;
    }
    const textarea = document.getElementById('correcao-direta-text');
    const text = (textarea?.value || '').trim();
    if (!text) {
        showToast('Digite a correção primeiro', 'warning');
        return;
    }
    try {
        const resp = await fetch('/api/elaborar/correcao/' + pajNorm, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: text}),
        });
        if (!resp.ok) {
            const err = await resp.json();
            showToast('Erro: ' + (err.erro || resp.status), 'error');
            return;
        }
        const data = await resp.json();
        const msg = data.last_action && data.last_action !== 'iniciando...'
            ? 'Correção enviada — ' + data.last_action
            : 'Correção enviada — Claude refazendo a peça';
        showToast(msg, 'info');
        textarea.value = '';
        document.getElementById('correcao-direta-modal')?.close();
        _triggerElaborarPolling();
    } catch (e) {
        showToast('Erro: ' + e.message, 'error');
    }
}

async function enviarCorrecao(pajNorm) {
    const textarea = document.getElementById('correcao-text');
    const text = (textarea?.value || '').trim();
    if (!text) {
        showToast('Digite a correção primeiro', 'warning');
        return;
    }
    try {
        const resp = await fetch('/api/elaborar/correcao/' + pajNorm, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: text}),
        });
        if (!resp.ok) {
            const err = await resp.json();
            showToast('Erro: ' + (err.erro || resp.status), 'error');
            return;
        }
        const data = await resp.json();
        const msg = data.last_action && data.last_action !== 'iniciando...'
            ? 'Correção enviada — ' + data.last_action
            : 'Correção enviada — Claude refazendo a peça';
        showToast(msg, 'info');
        textarea.value = '';
        document.getElementById('resumo-modal')?.close();
        // Inicia o polling para mostrar progresso e recarregar ao concluir
        _triggerElaborarPolling();
    } catch (e) {
        showToast('Erro: ' + e.message, 'error');
    }
}

/* =========================================================
   Limpeza de anexos (apaga binarios apos OCR)
   ========================================================= */
let _limpezaPajAtual = null;
let _limpezaPodeLimpar = false;

function _fmtBytes(n) {
    if (!n && n !== 0) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / (1024 * 1024)).toFixed(2) + ' MB';
}

function _limpezaResetUI() {
    document.getElementById('limpeza-loading').style.display = 'block';
    document.getElementById('limpeza-conteudo').style.display = 'none';
    document.getElementById('limpeza-bloqueios').style.display = 'none';
    document.getElementById('limpeza-resultado').style.display = 'none';
    document.getElementById('limpeza-erro').style.display = 'none';
    document.getElementById('limpeza-btn-executar').disabled = true;
    const chk = document.getElementById('limpeza-confirmar');
    if (chk) chk.checked = false;
    const force = document.getElementById('limpeza-forcar');
    if (force) force.checked = false;
}

async function abrirLimpezaModal(pajNorm) {
    _limpezaPajAtual = pajNorm;
    let modal = document.getElementById('limpeza-modal');
    if (!modal) return;
    _limpezaResetUI();
    modal.showModal();

    try {
        let resp = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/limpar-anexos/preview');
        let data = await resp.json();
        if (!resp.ok || !data.ok) {
            document.getElementById('limpeza-loading').style.display = 'none';
            document.getElementById('limpeza-conteudo').style.display = 'block';
            document.getElementById('limpeza-erro').style.display = 'flex';
            document.getElementById('limpeza-erro-msg').textContent =
                data.erro || ('HTTP ' + resp.status);
            return;
        }
        _limpezaRenderPreview(data);
    } catch (e) {
        document.getElementById('limpeza-loading').style.display = 'none';
        document.getElementById('limpeza-conteudo').style.display = 'block';
        document.getElementById('limpeza-erro').style.display = 'flex';
        document.getElementById('limpeza-erro-msg').textContent = 'Erro: ' + e.message;
    }
}

function _limpezaRenderPreview(data) {
    document.getElementById('limpeza-loading').style.display = 'none';
    document.getElementById('limpeza-conteudo').style.display = 'block';

    _limpezaPodeLimpar = !!data.pode_limpar;

    const aRemover = data.arquivos_a_remover || [];
    const preservados = data.arquivos_preservados || [];
    document.getElementById('limpeza-n-remover').textContent = aRemover.length;
    document.getElementById('limpeza-bytes-liberar').textContent =
        _fmtBytes(data.bytes_total_disponivel || 0) + ' liberaveis';
    document.getElementById('limpeza-n-preservar').textContent = preservados.length;

    // Lista a remover
    const tbR = document.getElementById('limpeza-lista-remover');
    tbR.innerHTML = '';
    aRemover.forEach(function(a) {
        let tr = document.createElement('tr');
        tr.innerHTML =
            '<td class="font-mono text-xs break-all">' + _htmlEscape(a.nome) + '</td>' +
            '<td class="text-xs">' + ((a.tamanho || 0) / 1024).toFixed(1) + '</td>' +
            '<td>' + (a.tem_ocr ? '<span class="badge badge-success badge-xs">sim</span>' :
                                  '<span class="badge badge-error badge-xs">NÃO</span>') + '</td>';
        tbR.appendChild(tr);
    });

    const tbP = document.getElementById('limpeza-lista-preservar');
    tbP.innerHTML = '';
    preservados.forEach(function(a) {
        let tr = document.createElement('tr');
        tr.innerHTML =
            '<td class="font-mono text-xs break-all">' + _htmlEscape(a.nome) + '</td>' +
            '<td class="text-xs">' + ((a.tamanho || 0) / 1024).toFixed(1) + '</td>';
        tbP.appendChild(tr);
    });

    // Bloqueios
    const motivos = data.motivos_bloqueio || [];
    if (motivos.length > 0) {
        document.getElementById('limpeza-bloqueios').style.display = 'flex';
        const ul = document.getElementById('limpeza-motivos');
        ul.innerHTML = '';
        motivos.forEach(function(m) {
            const li = document.createElement('li');
            li.textContent = m;
            ul.appendChild(li);
        });
    }

    // Se nao tem nada a remover, desabilita o botao
    if (aRemover.length === 0) {
        document.getElementById('limpeza-btn-executar').disabled = true;
        document.getElementById('limpeza-resultado').style.display = 'flex';
        document.getElementById('limpeza-resultado-msg').textContent =
            'Nenhum arquivo binário a remover — já está limpo.';
        return;
    }

    // Habilita botao quando usuario marcar confirmar (e forcar, se houver bloqueio)
    const confirmar = document.getElementById('limpeza-confirmar');
    let forcar = document.getElementById('limpeza-forcar');
    let btn = document.getElementById('limpeza-btn-executar');

    function avaliar() {
        let ok = confirmar.checked;
        if (!_limpezaPodeLimpar) {
            ok = ok && forcar.checked;
        }
        btn.disabled = !ok;
    }
    confirmar.onchange = avaliar;
    if (forcar) forcar.onchange = avaliar;
}

function _htmlEscape(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function executarLimpeza() {
    if (!_limpezaPajAtual) return;
    let forcar = !_limpezaPodeLimpar && document.getElementById('limpeza-forcar').checked;
    let btn = document.getElementById('limpeza-btn-executar');
    btn.disabled = true;
    btn.textContent = 'Apagando...';

    try {
        let url = '/api/paj/' + encodeURIComponent(_limpezaPajAtual) + '/limpar-anexos';
        if (forcar) url += '?forcar=true';
        let resp = await fetch(url, { method: 'POST' });
        let data = await resp.json();
        if (!resp.ok || !data.ok) {
            document.getElementById('limpeza-erro').style.display = 'flex';
            document.getElementById('limpeza-erro-msg').textContent =
                data.erro || ('HTTP ' + resp.status);
            btn.textContent = 'Apagar arquivos';
            return;
        }
        document.getElementById('limpeza-resultado').style.display = 'flex';
        document.getElementById('limpeza-resultado-msg').textContent =
            (data.removidos || 0) + ' arquivo(s) apagado(s) — ' +
            _fmtBytes(data.bytes_liberados || 0) + ' liberados.';
        btn.style.display = 'none';
        showToast('Anexos apagados — recarregando', 'success');
        setTimeout(function() { window.location.reload(); }, 1200);
    } catch (e) {
        document.getElementById('limpeza-erro').style.display = 'flex';
        document.getElementById('limpeza-erro-msg').textContent = 'Erro: ' + e.message;
        btn.textContent = 'Apagar arquivos';
        btn.disabled = false;
    }
}

function fecharLimpezaModal() {
    const m = document.getElementById('limpeza-modal');
    if (m) m.close();
}

/* =========================================================
   Notas pessoais por PAJ (NOTAS.md) — autosave 2s
   ========================================================= */
let _notasTimer = null;
const _notasCarregado = {};  // evita recarregar ao re-entrar na aba

async function carregarNotas(pajNorm) {
    if (_notasCarregado[pajNorm]) return;
    let ta = document.getElementById('notas-textarea');
    let status = document.getElementById('notas-status');
    if (!ta) return;
    if (status) status.textContent = 'carregando...';
    try {
        let resp = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/notas');
        let data = await resp.json();
        ta.value = data.texto || '';
        _notasCarregado[pajNorm] = true;
        if (status) status.textContent = ta.value ? 'carregado' : 'em branco';
    } catch (e) {
        if (status) status.textContent = 'erro ao carregar: ' + e.message;
    }
}

function agendarSalvarNotas(pajNorm) {
    let status = document.getElementById('notas-status');
    if (status) status.textContent = 'digitando...';
    if (_notasTimer) clearTimeout(_notasTimer);
    _notasTimer = setTimeout(function() { salvarNotas(pajNorm); }, 2000);
}

async function salvarNotas(pajNorm) {
    let ta = document.getElementById('notas-textarea');
    let status = document.getElementById('notas-status');
    if (!ta) return;
    if (status) status.textContent = 'salvando...';
    try {
        let resp = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/notas', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ texto: ta.value }),
        });
        let data = await resp.json();
        if (!resp.ok || !data.ok) {
            if (status) status.textContent = 'erro: ' + (data.erro || resp.status);
            showToast('Erro ao salvar notas', 'error');
            return;
        }
        if (status) status.textContent = 'salvo';
    } catch (e) {
        if (status) status.textContent = 'erro: ' + e.message;
    }
}

/* =========================================================
   Busca global (Ctrl+K / Cmd+K)
   ========================================================= */
let _buscaDebounce = null;
let _buscaResultados = [];
let _buscaIndiceSelecionado = -1;

function _buscaEscape(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _renderBuscaResultados() {
    let cont = document.getElementById('busca-results');
    if (!cont) return;
    if (_buscaResultados.length === 0) {
        cont.innerHTML = '<div class="p-4 text-sm text-base-content/50 text-center">Nenhum resultado.</div>';
        return;
    }
    const html = '';
    for (const i = 0; i < _buscaResultados.length; i++) {
        let r = _buscaResultados[i];
        let badge = '';
        if (r.fonte === 'metadata') badge = '<span class="badge badge-xs badge-primary">meta</span>';
        else if (r.fonte === 'sisdpu') badge = '<span class="badge badge-xs badge-info">sisdpu</span>';
        else if (r.fonte === 'ocr') badge = '<span class="badge badge-xs badge-warning">OCR '+_buscaEscape(r.arquivo)+'</span>';
        const selClass = (i === _buscaIndiceSelecionado) ? 'bg-base-300' : '';
        html += '<div class="p-3 border-b border-base-300 cursor-pointer hover:bg-base-300/50 '+selClass+'" ' +
                'data-idx="'+i+'" onclick="_buscaAbrir('+i+')">' +
                '<div class="flex items-center gap-2 mb-1">' +
                '<span class="font-mono text-xs font-semibold">'+_buscaEscape(r.paj_norm)+'</span>' +
                badge +
                '<span class="text-xs text-base-content/60 truncate">'+_buscaEscape(r.assistido || '')+'</span>' +
                (r.etiqueta ? '<span class="badge badge-xs badge-outline badge-accent ml-auto">'+_buscaEscape(r.etiqueta)+'</span>' : '') +
                '</div>' +
                '<div class="text-xs text-base-content/70 leading-snug">'+ (r.trecho || '') +'</div>' +
                '</div>';
    }
    cont.innerHTML = html;
    // Scroll pra o selecionado
    if (_buscaIndiceSelecionado >= 0) {
        let el = cont.querySelector('[data-idx="'+_buscaIndiceSelecionado+'"]');
        if (el) el.scrollIntoView({block: 'nearest'});
    }
}

function _buscaAbrir(i) {
    if (i < 0 || i >= _buscaResultados.length) return;
    let r = _buscaResultados[i];
    let url = '/paj/' + encodeURIComponent(r.paj_norm);
    window.location.href = url;
}

async function _buscaFetch(q) {
    let status = document.getElementById('busca-status');
    let cont = document.getElementById('busca-count');
    if (q.length < 2) {
        _buscaResultados = [];
        _buscaIndiceSelecionado = -1;
        _renderBuscaResultados();
        if (status) status.textContent = 'digite pelo menos 2 caracteres';
        if (cont) cont.textContent = '';
        return;
    }
    if (status) status.textContent = 'buscando...';
    try {
        let resp = await fetch('/api/busca?q=' + encodeURIComponent(q));
        let data = await resp.json();
        _buscaResultados = data.resultados || [];
        _buscaIndiceSelecionado = _buscaResultados.length > 0 ? 0 : -1;
        _renderBuscaResultados();
        if (status) status.textContent = _buscaResultados.length
            ? 'Enter abre o PAJ selecionado' : 'nenhum resultado';
        if (cont) cont.textContent = _buscaResultados.length + ' achados';
    } catch (e) {
        if (status) status.textContent = 'erro: ' + e.message;
    }
}

function abrirBuscaModal() {
    let modal = document.getElementById('busca-modal');
    let input = document.getElementById('busca-input');
    if (!modal || !input) return;
    input.value = '';
    _buscaResultados = [];
    _buscaIndiceSelecionado = -1;
    _renderBuscaResultados();
    document.getElementById('busca-status').textContent = 'digite pelo menos 2 caracteres';
    document.getElementById('busca-count').textContent = '';
    modal.showModal();
    setTimeout(function(){ input.focus(); }, 50);
}

document.addEventListener('keydown', function(e) {
    // Ctrl+K ou Cmd+K abre busca
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        abrirBuscaModal();
        return;
    }
    // Se o modal de busca esta aberto, trata navegacao
    let modal = document.getElementById('busca-modal');
    if (!modal || !modal.open) return;
    if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (_buscaResultados.length) {
            _buscaIndiceSelecionado = Math.min(_buscaResultados.length - 1, _buscaIndiceSelecionado + 1);
            _renderBuscaResultados();
        }
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (_buscaResultados.length) {
            _buscaIndiceSelecionado = Math.max(0, _buscaIndiceSelecionado - 1);
            _renderBuscaResultados();
        }
    } else if (e.key === 'Enter') {
        if (_buscaIndiceSelecionado >= 0) {
            e.preventDefault();
            _buscaAbrir(_buscaIndiceSelecionado);
        }
    }
});

// Debounce no input
document.addEventListener('input', function(e) {
    if (e.target && e.target.id === 'busca-input') {
        if (_buscaDebounce) clearTimeout(_buscaDebounce);
        let q = e.target.value;
        _buscaDebounce = setTimeout(function() { _buscaFetch(q); }, 250);
    }
});
