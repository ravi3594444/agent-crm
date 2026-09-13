# Session opener — the agenda rows that keep talking about a dead order

Fresh session in `plus-agent/` of `agent-crm`. Everything you need is in this message.

**First commit:** save this whole message as `docs/prompt-agenda-guard.md` on a branch off `main`,
push it.

Read `CLAUDE.md` at the repo root first, then `plus-agent/docs/MAPA.md`. Both are committed and
their rules are not optional — particularly the three about tests, and the working agreement that
whoever changes a module updates its `MAPA.md` row in the same commit.

**This PR is small on purpose. It is a bug fix on `main`, it ships alone, and another piece of work
(W4, the customer withdrawing their own draft) is blocked behind it.**

---

# The bug

A Sales Order can stop being live in two different ways, and three of the four `agenda.py` handlers
only know about one of them.

| how it stops being live | what the document looks like | who does it |
|---|---|---|
| confirmed or cancelled | `docstatus` 1 or 2 | the policy path |
| **closed while still a draft** | **`docstatus=0`, `status="Closed"`** | `decisiones.rechazar`, the expiry sweep, `agenda._cerrar_borrador` |

Three handlers check only `docstatus`:

- `_avisar_antes_de_entrega` (`app/agenda.py:977-979`) — messages the **customer** *"your order
  arrives at 17:00"*
- `_recordar_al_dueno` (`app/agenda.py:1041-1043`) — chases the **owner** about the deadline
- `_seguimiento` (`app/agenda.py:1080`) — messages the **team**; it has no document guard at all

So after staff reject a draft, or after a request expires and the sweep closes it, those rows keep
firing at an order nobody will ever deliver. The customer is told a delivery time for an order that
is dead.

`_cerrar_borrador` does **not** have this bug, and the reason is the fix: it routes through
`pendientes._sigue_esperando` (`app/pendientes.py:311-334`), which re-reads and then checks **both**
`docstatus == 0` **and** `not policy.sin_reserva(doc.get("status"))`.

It is latent today only because `.env.example` ships `AVISO_ANTES_DE_ENTREGA_HORAS=-`. It stops
being latent the moment anyone turns that on, and W4 makes it reachable from a customer message.

# What to build

Add the missing half of the check to the three handlers, next to the `docstatus` check each already
has (or, for `_seguimiento`, as the guard it does not have):

```python
from app import policy
...
if policy.sin_reserva(doc.get("status")):
    return Resultado(detalle="el pedido ya no está vivo")   # TERMINAL, not None
```

`policy.sin_reserva` is public (`app/policy.py:116`) and is the single definition of "ERPNext does
not count this against stock". Use it directly — do **not** call `pendientes._sigue_esperando`,
which is private and carries `pendientes`' own meaning.

**Return a terminal `Resultado`, never `None`.** `None` leaves the row live and it retries every
tick forever against a document that will never change back. A dead order should close its rows,
not accumulate them.

Check `programar_para_entrega` too: it already refuses anything that is not `docstatus=0`, but a
draft that is already Closed should not get rows scheduled on it either.

# Constraints

- No new strings unless a `Resultado.detalle` needs one; if it does, it goes through `idioma.t` in
  ES and EN like everything else.
- Nothing under `REGLAS QUE NO PODÉS ROMPER` changes. No prompt change at all.
- No new environment variable, no new marker, no new row type.

# Done when

- `ruff check app demo tests deploy`; `pytest -q`; green in all five CI cells.
- **Read the collected test count before calling `pytest` green.** `app/graph.py` builds the Redis
  checkpointer at import, so with no Redis Stack on `REDIS_URL` nine modules fail at collection and
  pytest aborts having executed **zero** tests. A run that collected nothing is not a green run.
  CI has Redis and is the real gate.
- Tests, one per handler: a row of each of the three types, scheduled on an order that is then
  Closed while still a draft, **sends nothing and reaches a terminal state**. Assert both halves —
  asserting only "nothing was sent" passes for a row that is merely stuck.
- Mutations, per `CLAUDE.md`: deleting the new `sin_reserva` check in each handler must kill
  exactly one test, and a different one each time. Three handlers, three separate mutations —
  they are three consumers of the same rule, and `CLAUDE.md` says mutate each consumer separately.
  Write each mutation and its result into the commit message.
- `docs/MAPA.md`: update the `app/agenda.py` row to say that a handler's "still live?" test is
  `docstatus == 0` **and** `not policy.sin_reserva(status)`, because a closed draft is neither.

**Open a PR when green and tell the owner. Do not merge, and let Qodo's review land before you call
it ready.** Say in the PR description that this is a bug already on `main`, that it affects
`decisiones.rechazar` and the expiry sweep today, and that W4 is blocked behind it.
