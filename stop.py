#!/usr/bin/env python3
"""Para o servidor dpuscript-ui (le PID file + limpa porta)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
import contextlib

ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / ".server.pid"
PORT = 8001


def _killp_windows(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def main() -> int:
    # PID file
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if os.name == "nt":
                _killp_windows(pid)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
            print(f"[stop] matou PID {pid} (do PID file)")
        except ValueError:
            pass
        PID_FILE.unlink(missing_ok=True)

    # Garante que a porta ta livre
    if os.name == "nt":
        result = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
        seen: set[int] = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[3] != "LISTENING":
                continue
            if not parts[1].endswith(f":{PORT}"):
                continue
            try:
                pid = int(parts[-1])
                if pid > 4 and pid not in seen:
                    _killp_windows(pid)
                    seen.add(pid)
                    print(f"[stop] matou processo na porta {PORT}: PID {pid}")
            except ValueError:
                pass

    print("[stop] servidor parado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
