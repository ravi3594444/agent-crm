"""The agent supervisor is recognised by an exact marker, never by "uvicorn".

start.sh keeps the supervisor's pid in a file and, after a Codespace restart,
asks whether it is still alive before launching another. Two wrong answers
used to be possible: a pid recycled by the OS (any process passed `kill -0`),
and then any process whose command line merely CONTAINED "uvicorn" — another
project's server, a stray test — so start.sh waited for its /health and the
WhatsApp agent never came up. deploy/supervisor.sh answers with one exact
argument the supervisor carries, `plus-agent-supervisor=<pidfile>`, and the
supervisor removes the pidfile itself when it exits.

These tests drive the real shell functions with real processes.
"""
from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import time

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[2]
LIB = RAIZ / "plus-agent" / "deploy" / "supervisor.sh"
START = RAIZ / "start.sh"


def _bash(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}"\n{script}', "_", *args],
        capture_output=True, text=True, timeout=30, check=False,
    )


def vivo(pidfile: pathlib.Path) -> bool:
    return _bash('supervisor_vivo "$1"', str(pidfile)).returncode == 0


def _esperar(condicion, segundos: float = 5.0) -> bool:
    fin = time.monotonic() + segundos
    while time.monotonic() < fin:
        if condicion():
            return True
        time.sleep(0.05)
    return False


def _proceso_vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill -0; read its state.
    try:
        estado = pathlib.Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
    except OSError:
        return False
    return estado != "Z"


@pytest.fixture
def carpeta(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    yield tmp_path


@pytest.fixture
def procesos():
    """Everything spawned here is killed at teardown, whole process group."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        for objetivo in (-pid, pid):
            try:
                os.kill(objetivo, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def lanzar_supervisor(carpeta, procesos, *comando: str) -> tuple[int, pathlib.Path]:
    pidfile = carpeta / "agente.pid"
    log = carpeta / "agente.log"
    res = _bash(
        'supervisor_lanzar "$1" "$2" "$3" "$4" "${@:5}"',
        str(carpeta), "0", str(pidfile), str(log), *comando,
    )
    assert res.returncode == 0, res.stderr
    assert _esperar(lambda: pidfile.exists() and pidfile.read_text().strip().isdigit()), (
        "the supervisor never wrote its pid"
    )
    pid = int(pidfile.read_text().strip())
    procesos.append(pid)
    return pid, pidfile


# ------------------------------------------------------------ recognition


def test_the_real_supervisor_is_recognised(carpeta, procesos):
    pid, pidfile = lanzar_supervisor(carpeta, procesos, "sleep", "30")
    cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    assert f"plus-agent-supervisor={pidfile}".encode() in cmdline
    assert vivo(pidfile) is True


def test_an_unrelated_uvicorn_process_is_rejected(carpeta, procesos):
    """A process whose command line says uvicorn, even on our port, is not ours."""
    otro = subprocess.Popen(
        ["bash", "-c", 'exec -a "uvicorn app.main:app --host 0.0.0.0 --port 8081" sleep 30'],
        start_new_session=True,
    )
    procesos.append(otro.pid)
    assert _esperar(lambda: b"uvicorn" in pathlib.Path(f"/proc/{otro.pid}/cmdline").read_bytes())
    pidfile = carpeta / "agente.pid"
    pidfile.write_text(f"{otro.pid}\n")
    assert vivo(pidfile) is False


def test_another_agent_instance_with_its_own_pidfile_is_not_this_one(carpeta, procesos, tmp_path_factory):
    """Two deployments on one box: each supervisor answers only for its pidfile."""
    otra = tmp_path_factory.mktemp("otra")
    (otra / ".venv" / "bin").mkdir(parents=True)
    pid_otro, _ = lanzar_supervisor(otra, procesos, "sleep", "30")
    pidfile = carpeta / "agente.pid"
    pidfile.write_text(f"{pid_otro}\n")
    assert vivo(pidfile) is False


@pytest.mark.parametrize("contenido", ["", "   ", "abc", "12 34", "12abc", "-1", "0x1F"])
def test_an_invalid_pidfile_is_rejected(carpeta, contenido):
    pidfile = carpeta / "agente.pid"
    pidfile.write_text(contenido)
    assert vivo(pidfile) is False


def test_a_missing_pidfile_is_rejected(carpeta):
    assert vivo(carpeta / "agente.pid") is False


def test_a_stale_pid_of_an_exited_process_is_rejected(carpeta):
    muerto = subprocess.Popen(["sleep", "0"])
    muerto.wait(timeout=10)
    pidfile = carpeta / "agente.pid"
    pidfile.write_text(f"{muerto.pid}\n")
    assert vivo(pidfile) is False


# --------------------------------------------------------------- shutdown


def test_a_normal_shutdown_removes_the_pidfile_and_stops_the_child(carpeta, procesos):
    pid, pidfile = lanzar_supervisor(carpeta, procesos, "sleep", "30")
    assert vivo(pidfile)
    hijos = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
    assert hijos, "the supervised command is not running"

    os.kill(pid, signal.SIGTERM)

    assert _esperar(lambda: not pidfile.exists()), "the pidfile survived the shutdown"
    assert _esperar(lambda: not _proceso_vivo(pid))
    assert _esperar(lambda: all(not _proceso_vivo(int(h)) for h in hijos)), "an orphan child kept running"
    assert vivo(pidfile) is False


def test_the_supervisor_restarts_the_command_when_it_exits(carpeta, procesos):
    """The loop that start.sh always had: a dead agent comes back on its own."""
    marcador = carpeta / "arranques"
    _, pidfile = lanzar_supervisor(
        carpeta, procesos, "bash", "-c", f'echo x >>"{marcador}"; exit 1',
    )
    assert _esperar(lambda: marcador.exists() and marcador.read_text().count("x") >= 1)
    assert vivo(pidfile)
    assert "reinicio en 3 s" in (carpeta / "agente.log").read_text()


# ------------------------------------------------------ start.sh contract


def test_start_sh_uses_the_library_and_nothing_else_for_the_supervisor():
    """Startup and health behaviour stay in start.sh; identity lives in the lib."""
    texto = START.read_text()
    assert '. "$APP/deploy/supervisor.sh"' in texto
    assert 'supervisor_vivo "$PIDFILE"' in texto
    assert 'supervisor_lanzar "$APP" "$PORT" "$PIDFILE" "$LOG"' in texto
    assert "grep -q uvicorn" not in texto, "the substring check is what let a stranger in"
    # The health handshake around it is the same as before.
    assert 'curl -sf -m 3 "http://localhost:$PORT/health"' in texto
    assert 'echo "[start] LISTO"' in texto
    for archivo in (START, LIB):
        assert subprocess.run(["bash", "-n", str(archivo)], capture_output=True).returncode == 0


def test_the_default_command_is_the_agents_own_uvicorn(carpeta, procesos):
    """With no command, supervisor_lanzar runs the venv's uvicorn for app.main:app."""
    falso = carpeta / ".venv" / "bin" / "uvicorn"
    falso.write_text('#!/bin/bash\necho "uvicorn $*" >>"$(dirname "$0")/../../llamadas"\nsleep 30\n')
    falso.chmod(0o755)
    _, pidfile = lanzar_supervisor(carpeta, procesos)
    llamadas = carpeta / "llamadas"
    assert _esperar(lambda: llamadas.exists())
    assert llamadas.read_text().strip() == "uvicorn app.main:app --host 0.0.0.0 --port 0 --no-access-log"
    assert vivo(pidfile)
