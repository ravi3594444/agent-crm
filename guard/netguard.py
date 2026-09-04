"""pytest plugin: block and record any non-loopback socket connect / DNS."""
import ipaddress
import json
import os
import socket
import threading

_LOG = os.environ["NETGUARD_LOG"]
_lock = threading.Lock()
_seen: dict = {}
_violations: list = []
_oc, _oce, _oga = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo


def _loop(host) -> bool:
    if host in ("localhost", "", None):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return str(host).endswith(".localhost")


def _rec(kind, target, ok):
    with _lock:
        k = f"{kind} {target}"
        _seen[k] = _seen.get(k, 0) + 1
        if not ok:
            _violations.append(f"{k} <- {os.environ.get('PYTEST_CURRENT_TEST','?').split(' ')[0]}")


def _chk(sock, address):
    if sock.family == getattr(socket, "AF_UNIX", object()):
        return
    host, port = address[0], address[1]
    ok = _loop(host)
    _rec("connect", f"{host}:{port}", ok)
    if not ok:
        raise ConnectionRefusedError(f"netguard: blocked {host}:{port}")


def _c(self, a):
    _chk(self, a)
    return _oc(self, a)


def _ce(self, a):
    _chk(self, a)
    return _oce(self, a)


def _ga(host, port, *a, **k):
    h = host.decode() if isinstance(host, bytes) else host
    ok = _loop(h)
    _rec("dns", f"{h}:{port}", ok)
    if not ok:
        raise socket.gaierror(f"netguard: blocked DNS {h!r}")
    return _oga(host, port, *a, **k)


socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = _c, _ce, _ga


def pytest_sessionfinish(session, exitstatus):
    with open(_LOG, "w") as fh:
        json.dump({"seen": _seen, "violations": _violations}, fh, indent=1, sort_keys=True)
