"""Servico de sincronizacao da caixa SISDPU.

Expoe `rodar_sync()` como async generator de linhas de log (para SSE).
Usa asyncio.Lock global para garantir que uma unica sincronizacao roda por vez.

Cancelamento: `solicitar_cancelamento()` sinaliza o worker corrente via um
asyncio.Event. O sincronizador consulta `deve_cancelar()` entre PAJs e aborta
de forma limpa (fechando a sessao Playwright).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator

from config import SISDPU_PASSWORD, SISDPU_USERNAME, TIMEOUT_TOTAL
from ingestao import sincronizador
from ingestao.sisdpu_client import CredenciaisInvalidas
from services.paj_service import invalidar_cache_listagem


_sync_lock = asyncio.Lock()
_cancel_event: asyncio.Event | None = None


def solicitar_cancelamento() -> bool:
    """Sinaliza o worker em execucao pra parar na proxima oportunidade.

    Retorna True se havia uma sync rodando (sinal enviado), False caso contrario.
    """
    global _cancel_event
    if _cancel_event is None or _cancel_event.is_set():
        return False
    _cancel_event.set()
    return True


def deve_cancelar() -> bool:
    """Consultado pelo sincronizador entre PAJs. True => abortar laco."""
    return _cancel_event is not None and _cancel_event.is_set()


def esta_rodando() -> bool:
    return _sync_lock.locked()


async def _rodar_com_cancel(
    coro_factory,
    emit,
) -> AsyncGenerator[str, None]:
    """Helper comum pro rodar_sync e rodar_sync_paj.

    `coro_factory` e' uma closure que retorna a coroutine a executar (recebe
    `emit` ja configurado). Isolamos aqui a logica de lock, cancel_event,
    queue SSE e cleanup.
    """
    global _cancel_event
    if _sync_lock.locked():
        yield "[ERRO] ja existe uma sincronizacao em andamento — aguarde terminar"
        return

    if not SISDPU_USERNAME or not SISDPU_PASSWORD:
        yield (
            "[ERRO] credenciais SISDPU ausentes — preencha SISDPU_USERNAME e "
            "SISDPU_PASSWORD no arquivo .env e reinicie o painel"
        )
        return

    async with _sync_lock:
        _cancel_event = asyncio.Event()
        queue: asyncio.Queue = asyncio.Queue()

        def _emit(linha: str) -> None:
            with contextlib.suppress(Exception):
                queue.put_nowait(linha)

        async def _worker() -> None:
            try:
                await asyncio.wait_for(coro_factory(_emit), timeout=TIMEOUT_TOTAL)
            except TimeoutError:
                _emit(f"[ERRO] timeout de {TIMEOUT_TOTAL}s atingido")
            except asyncio.CancelledError:
                _emit("[CANCELADO] sincronizacao interrompida pelo usuario")
                raise
            except CredenciaisInvalidas as e:
                _emit(f"[ERRO] credenciais SISDPU rejeitadas: {e}")
                _emit("[DICA] ajuste SISDPU_USERNAME/SISDPU_PASSWORD no .env e reinicie o painel")
            except Exception as e:
                _emit(f"[ERRO FATAL] {type(e).__name__}: {e}")
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(_worker())

        try:
            while True:
                linha = await queue.get()
                if linha is None:
                    break
                yield linha
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(Exception):
                    await task
            _cancel_event = None
            # Sync mexeu nos metadata.json — invalida o cache de listagem
            # pra que a proxima chamada do dashboard veja o estado atual.
            invalidar_cache_listagem()


async def rodar_sync(baixar_anexos: bool = True) -> AsyncGenerator[str, None]:
    """Gera linhas de log da sincronizacao em tempo real.

    `baixar_anexos=False` pula o download/OCR dos anexos (sync rapido).
    """
    async for linha in _rodar_com_cancel(
        lambda emit: sincronizador.rodar(
            emit, deve_cancelar=deve_cancelar, baixar_anexos=baixar_anexos,
        ),
        None,
    ):
        yield linha


async def rodar_sync_paj(
    paj_identificador: str,
    baixar_anexos: bool = True,
) -> AsyncGenerator[str, None]:
    """Sincroniza um unico PAJ via SSE (teste/refresh pontual).

    Reutiliza o mesmo lock global — evita que uma sync da caixa inteira colida
    com uma sync pontual, e vice-versa.
    """
    async for linha in _rodar_com_cancel(
        lambda emit: sincronizador.rodar_paj_unico(
            paj_identificador, emit,
            deve_cancelar=deve_cancelar, baixar_anexos=baixar_anexos,
        ),
        None,
    ):
        yield linha


async def rodar_sync_anexos_desde_data(
    paj_identificador: str,
    data_inicio,
) -> AsyncGenerator[str, None]:
    """Baixa anexos de um PAJ a partir de uma data — SSE.

    Reusa o lock global pra nao colidir com outras sincronizacoes em curso.
    `data_inicio` deve ser `datetime.date`.
    """
    async for linha in _rodar_com_cancel(
        lambda emit: sincronizador.rodar_anexos_desde_data(
            paj_identificador, data_inicio, emit,
            deve_cancelar=deve_cancelar,
        ),
        None,
    ):
        yield linha
