@echo off
rem ============================================================
rem  DPU-script-SIS — inicia o servidor (se precisar) e abre browser
rem  Usar: dar duplo clique, ou criar atalho no desktop/barra de tarefas
rem ============================================================

setlocal

rem Caminho deste .bat = raiz do projeto
set ROOT=%~dp0
cd /d "%ROOT%"

set PYTHON=%ROOT%.venv\Scripts\python.exe
set URL=http://127.0.0.1:8001/

rem Checa se o servidor ja esta rodando (netstat na porta 8001)
netstat -ano -p TCP | findstr /R /C:":8001 .* LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo Servidor ja esta rodando. Abrindo browser...
    start "" "%URL%"
    goto :fim
)

rem Nao esta rodando — inicia em janela separada e abre browser
echo Iniciando servidor DPU-script-SIS...
start "DPU-script-SIS" /MIN "%PYTHON%" "%ROOT%app.py"

rem Aguarda 4s pro servidor subir
timeout /T 4 /NOBREAK >nul

echo Abrindo browser em %URL%
start "" "%URL%"

:fim
endlocal
