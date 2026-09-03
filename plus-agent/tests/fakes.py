"""One in-memory Redis double, shared by every test that needs markers.

Why it is here and not copied into each file: app/avisos.py runs its queue
through Lua, because "write the idempotency key and the queue entry" has to be
one step. A double that answered every ``eval`` with a canned value would let
a test pass while the real script did nothing, so this one actually interprets
the three scripts the application uses. They are short, they change rarely, and
a divergence shows up as a failing test rather than as a queue that silently
drops a customer's confirmation.
"""

from __future__ import annotations

import time


class FakeMarcas:
    """Strings, lists, counters and one sorted set — no server, no network."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.caido = False

    # ----------------------------------------------------------- strings
    def _vivo(self) -> None:
        if self.caido:
            raise RuntimeError("redis de prueba caído")

    def set(self, key, value, nx=False, ex=None):
        self._vivo()
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        self._vivo()
        return self.values.get(key)

    def delete(self, key):
        self._vivo()
        self.values.pop(key, None)
        return 1

    def incr(self, key):
        self._vivo()
        nuevo = int(self.values.get(key) or 0) + 1
        self.values[key] = str(nuevo)
        return nuevo

    def scan_iter(self, match="*", count=100):
        self._vivo()
        return iter([k for k in self.values if k.startswith(match.rstrip("*"))])

    # ------------------------------------------------------------- lists
    def rpush(self, key, value):
        self._vivo()
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def llen(self, key):
        self._vivo()
        return len(self.lists.get(key, []))

    def lrange(self, key, start, end):
        self._vivo()
        datos = self.lists.get(key, [])
        total = len(datos)
        desde = max(0, total + start) if start < 0 else start
        hasta = total + end if end < 0 else end
        return datos[desde : hasta + 1]

    # -------------------------------------------------------- sorted set
    def zadd(self, key, mapping):
        self._vivo()
        destino = self.zsets.setdefault(key, {})
        nuevos = sum(1 for m in mapping if m not in destino)
        destino.update({m: float(s) for m, s in mapping.items()})
        return nuevos

    def zrem(self, key, member):
        self._vivo()
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    def zcard(self, key):
        self._vivo()
        return len(self.zsets.get(key, {}))

    def zscore(self, key, member):
        self._vivo()
        return self.zsets.get(key, {}).get(member)

    def _due(self, key, tope: float) -> list[str]:
        miembros = self.zsets.get(key, {})
        return sorted(
            (m for m, s in miembros.items() if s <= tope), key=lambda m: miembros[m]
        )

    # -------------------------------------------------------------- Lua
    def eval(self, script, numkeys, *args):
        self._vivo()
        keys = [str(k) for k in args[:numkeys]]
        argv = [a.decode() if isinstance(a, bytes) else str(a) for a in args[numkeys:]]

        if "ZRANGEBYSCORE" in script:
            # avisos._RECLAMAR_LUA: take the earliest due entry and lease it.
            due = self._due(keys[0], float(argv[0]))
            if not due:
                return False
            self.zsets[keys[0]][due[0]] = float(argv[1])
            return due[0]

        if "ZADD" in script and "EXISTS" in script:
            # avisos._ENCOLAR_LUA: idempotency key and queue entry, together.
            if keys[0] in self.values:
                return 0
            self.zadd(keys[1], {argv[1]: float(argv[0])})
            self.values[keys[0]] = "1"
            return 1

        # outbound_status._RECORD_LUA: only its return value is ever read.
        for key in keys[1:2]:
            self.values.setdefault(key, "accepted_by_meta")
        return self.values.get(keys[1] if len(keys) > 1 else keys[0], "accepted_by_meta")


def entrada_de_cola(marcas: FakeMarcas, cola: str) -> list[str]:
    """Queued notices, earliest first — what a worker would pick up."""
    return marcas._due(cola, time.time() + 10**9)
