# Session opener — W4: the customer can take back their own draft

Fresh session in `plus-agent/` of `agent-crm`. Everything you need is in this message.

**First commit:** save this whole message as `docs/prompt-w4.md` on a branch off `main`, push it.

## Where things stand

Read `CLAUDE.md` at the repo root first — it is committed and its rules are not optional,
particularly hard rule 2 (three ERPNext identities) and the three about tests. Then read
`plus-agent/docs/MAPA.md`: it says what each big file does and what its traps are, and the working
agreement is that whoever changes a module updates its row in the same commit.

**Blocked on one PR.** `docs/prompt-agenda-guard.md` fixes three `agenda.py` handlers that decide
"is this order still live?" by `docstatus` alone. A draft closed by a customer withdrawal is
`docstatus=0, status="Closed"` — still "live" by that test — so without that fix, this feature makes
the agent message a customer *"your order arrives at 17:00"* about the order they just took back.
**Do not start until it is merged.** If it is not merged yet, say so and stop.

Merged and relevant:

- **`app/agenda.py`** (#38) — one durable row plus one sweep. Every new time-based behaviour is a
  row, not a module, and the five shared concerns (exactly-once, quiet hours, re-read before
  acting, fail closed, bounded batch) live in the sweep and only there. **You need it. Read it
  first.**
- **`app/marcas.py`** (#30) — the registry for every durable ERPNext marker. Your marker is a row
  **there**, never a module-local constant.
- **`app/reloj.py`** (#26) and `RelojDePrueba` in `tests/conftest.py` (#31) — one business clock,
  and fixtures that name their own moment and zone.
- **Five CI cells** on every PR: default, `IDIOMA_DEFAULT=en`, `IDIOMA_GERENCIA=en`,
  `BUSINESS_TIMEZONE=Asia/Kolkata`, `LOCALE=en_US`. All five must be green.

**Open a PR when green and tell the owner. Do not merge, and let Qodo's review land before you call
it ready** — it has found something real on every PR in this repo, including two security issues.

---

# The behaviour

A shopkeeper writes *"cancelá el pedido, me equivoqué"* about a draft that is still `docstatus=0`
and is theirs. Today nothing happens: the customer has no way to undo their own mistake, so a wrong
order sits in the queue until a human notices, and the owner spends attention on something the
customer already knew was wrong.

After this, the customer can take it back. The draft is **closed, never deleted** — ERPNext stops
reserving the stock, the audit trail survives. A `[baja-por-cliente]` marker goes on the order, the
owner is told, and the customer gets one line.

**On a confirmed order (`docstatus=1`) the tool refuses.** A confirmed order is a commitment and
only staff may cancel one, inside `CANCELACION_HORAS`. The model escalates as it does today.

---

# The shape: perceive and propose in the model, authority in Python

This is the part that decides the design, so it comes before the details.

Closing a draft means writing its `status` field, and the only helper in the whole ERPNext client
that writes a status is `erpnext.policy_update_status` — **policy identity**. `CLAUDE.md` hard rule
2 says that identity is "not reachable from any tool", and the rule is not relaxable: it is what the
product is sold on. So the tool cannot do the write.

It does not have to. **A customer tool can already write a durable agenda row with the customer
credential** — `marcas.escribir` → `erpnext.registrar_comentario` → `_active_client()`, which
respects `customer_scope()`. #38's `recordar` does exactly this in production today. And the sweep
is a background thread with no scope, which calls `policy_*` explicitly — the pattern `MAPA.md`
already documents.

So:

```
model            understands "cancelá el pedido, me equivoqué" — any phrasing, any language
  ↓
tool (Python)    authenticates, checks ownership, checks state, writes an agenda ROW
                 with the CUSTOMER credential. No privileged write. Answers the customer.
  ↓
sweep (Python)   ≤60 s later, with the POLICY credential: closes the draft, writes the marker,
                 schedules the owner's notice.
```

No regex word list — the model keeps the job of understanding, which is why we are not routing this
deterministically. No privileged write inside a tool. Both halves of hard rule 2 intact.

## Two rows, not one — and this is the trap

The obvious design is one row that closes the draft and tells the owner. It is wrong, and the
reason is quiet hours.

`agenda._despachar` defers every row whose type is in `_HABLAN_CON_ALGUIEN` between 22:00 and 07:00
(`app/agenda.py:672-679`). A message to a person at 03:00 is not read, it is resented — so that
deferral is correct **for the message**. It is wrong for the release: a customer who cancels at
23:00 would have their stock held until 07:00 the next morning, for no reason anybody could
explain.

One row, two consumers, two different correct answers. So: **two row types.**

| type | in `_HABLAN_CON_ALGUIEN`? | what it does |
|---|---|---|
| `baja_de_pedido` | **no** | `soltar_reserva`, then the marker. Runs at 03:00 if that is when it is due. |
| `aviso_baja_al_dueno` | **yes** | tells the owner. Deferred to 07:00 like every other human-facing notice. |

The release row creates the notice row **only when the release is proven** — same idiom as #38's
owner re-ping, which is itself another row. Two rows, two handlers of about ten lines each, no new
mechanism.

---

# What to build

## The tool

**`dar_de_baja_pedido(numero, config)`** — a new customer tool in `app/tools/pedidos.py`, registered
in **`TOOLS_CLIENTES` in `app/graph.py`**. Defining the `@tool` does not make it reachable: the
agent is built from that hand-written list at import time, and an unregistered tool is answered with
"that tool does not exist for this conversation". Every behavioural test below can be green while the
feature is unreachable, so **assert the registration itself**: `dar_de_baja_pedido` is in
`TOOLS_CLIENTES` and not in `TOOLS_GERENCIA`.

**On the name.** Not `retirar_pedido`: `retiro`/`retirar` already means **pickup at the shop**
throughout this codebase — a staff command (`main.py:407`), an owner action (`acciones.py:189`),
three settings (`limites.py:573+`) and a customer-facing catalogue string
(`entrega.respaldo_retiro`). Reusing it for the opposite meaning poisons the model's vocabulary and
the customer's line. And not `cancelar_*`: `tests/test_frontera_decisiones.py:111-116` forbids any
tool whose name contains `cancelar`. `dar_de_baja` is clean in both directions.

In order:

1. **Authenticate.** `actor, cuenta = _cuenta_del_remitente(config)`, catching `RuntimeContextError`
   as `recordar` does.
2. **Read the order and check ownership**, following `recordar` at `app/tools/pedidos.py:1056-1060`
   — it is the pattern #38 added and Qodo reviewed:
   ```python
   try:
       doc = erpnext.policy_get_doc("Sales Order", pedido)
   except Exception:
       return NO_ENCONTRADO          # one string, built once
   if not actor.gerencia_verificada and str(doc.get("customer") or "") != cuenta:
       return NO_ENCONTRADO          # the SAME string, from the same place
   ```
   **One token, one string, both branches.** Not two tokens: a tool's return value is read by the
   *model*, which then writes the customer's line, so two distinct tokens tell it in plain text
   whether that order number exists and a customer can enumerate orders by watching the replies.
   #38 returns one byte-identical string; so does `catalogo.estado_pedido`. Build it at a single
   `return` site so the two paths cannot drift, and test it as `assert a == b`.
   Keep the `gerencia_verificada` carve-out, for the reason `recordar` states: the scope comes from
   the webhook, but touching anybody's order is enabled only by a phone still on the team list.
3. **Refuse anything that is not a draft**, in the tool, explicitly:
   `if int(doc.get("docstatus") or 0) != 0: return YA_CONFIRMADO` — the model then escalates to a
   person. Do **not** lean on `soltar_reserva` for this. It does refuse non-drafts, but it runs in
   the sweep, an hour later and out of the customer's sight; and a test that only asserts "the
   document was untouched" stays green with this guard deleted, which makes it worthless.
4. **Refuse while any decision is in flight.** `s = solicitudes.leer(pedido)`, then:
   ```python
   if s is not None and s.estado not in solicitudes.TERMINALES:
       return HAY_DECISION_EN_CURSO      # the model escalates to a person
   ```
   **One condition, over `TERMINALES`** (`app/solicitudes.py:147`). Do **not** write a subset of
   states: `ABIERTOS` deliberately excludes `REVISION_HUMANA`, and gating on `abierta` refuses the
   two states where the customer has *not* accepted while permitting the one where they have —
   exactly backwards. Anything not terminal still carries a live deadline the sweep must honour, and
   letting a withdrawal race it is how a cancelled order gets a fallback offer, an "acepto", and a
   Submit. At `AUTO_CONFIRM_MAX=0` a human sees every one of these anyway, so refusing the rare
   overlap costs nothing.
5. **Write the row** — `agenda.crear(pedido, BAJA_DE_PEDIDO, vence=now, …)`. Customer credential,
   no privileged write. If it does not come back durable, say so: a row that was not written did
   not happen.
6. **Answer the customer**, honestly: the order has been **taken back**, it will not be prepared,
   and nothing about when anything else happens. Not "done", not a time, not a promise.

## The two handlers

**`_baja_de_pedido`** — re-read inside the lock (the sweep does this for you), then:

- `docstatus != 0` or `policy.sin_reserva(status)` already true → terminal `Resultado`, nothing sent.
  Somebody else closed it; that is a convergent ending, not a failure.
- otherwise `ok, frase = solicitudes.soltar_reserva(pedido)`.
  - `ok` → write the `[baja-por-cliente]` marker, create the `aviso_baja_al_dueno` row, terminal.
  - `not ok` → **no marker, no owner notice, row stays live and retries.** An unproven release is
    not a withdrawal, and writing the marker anyway would put "taken back" in the audit trail of an
    order still holding stock — the class of lie `app/confirmacion.py` exists to prevent. If it is
    still failing after the row's retry budget, `notificar.avisar_dueno` so a person knows an order
    the customer was told was cancelled is still open.

**`_aviso_baja_al_dueno`** — compose one message from the row's data and return. Ten lines. It
touches no Redis, no timing, no idempotency: that is the sweep's job and only the sweep's.

## Marker and idempotency

`[baja-por-cliente]` is a row in `marcas.py`'s `_FILAS`, with `porque_el_techo` filled in.

`soltar_reserva` is idempotent — it returns `(True, "el borrador ya estaba cerrado…")` when the
draft is already closed — but **`marcas.escribir` is not**. It is a plain `add_comment` with no
dedup, so a second withdrawal writes a second marker. Write the marker only on the transition:
check `marcas.existe` first, or let the row's own exactly-once carry it. Note that the
already-closed branch means "closed by *anybody*" — including a draft staff rejected — so the
marker must not be written there either.

`avisos.encolar` dedupes by `(evento, pedido)` for 30 days. If the owner's notice goes through it,
**fold the row id into the event name**, or a second withdrawal on the same order is silently
swallowed. `MAPA.md` documents this.

## Bonus fix, its own commit

`decisiones.rechazar` tells the manager *"Marcado como Closed: ya no compromete stock"* on the
strength of `_marcar_sin_reserva` (`app/decisiones.py:228`). That helper re-reads **before** writing
— it refuses anything that is not a draft — but it never re-reads **after**, so it can claim a
release ERPNext did not keep. Make it delegate to `soltar_reserva` and carry the proven phrase.

Before you write this: read where that manager-facing sentence actually goes and who quotes it. If
the string is durable audit vocabulary quoted elsewhere, changing its wording is a separate decision
from changing its truthfulness — say which you did.

## Tone, its own commit

`CÓMO HABLÁS` in `app/prompts.py` binds the model but not the fixed strings in `app/idioma.py`, so
those strings can break the agent's own rules and nothing notices. One does: `pedido.pendiente`
(`app/idioma.py:179`) says *"vuelvo a chequear el stock antes de cerrarlo"* / *"I will re-check stock
before closing it"*, which `prompts.py:44` explicitly forbids — *"Nunca cuentes lo que hacés por
dentro"*.

**Rewrite `pedido.pendiente`, ES and EN.** Say only what the customer needs: it is noted, it is not
confirmed, someone will answer. Nothing about re-checking stock, nothing about asking the manager.

**Do not build a lint test for this.** It was considered and dropped: a sentence counter cannot
express "one message the length of theirs", counting `¿` as a second question mark flags nine
correct Spanish strings, and the customer/staff split the test would need does not exist in the
catalogue. Tone gets judged by ear, once a working model key makes the agent audible.

Check whether any test pins the old wording. If one does, say whether it protected real behaviour
or just pinned a string, per `CLAUDE.md`.

---

# Constraints

- Every new string through `idioma.t`, ES and EN. New keys at the **end** of the block.
- The customer message promises nothing — no day, no hour, no price.
- `tests/test_frontera_decisiones.py` must still pass. This tool takes back a request that was never
  granted; it does not confirm, reject, or cancel anything committed. Say that in the PR
  description — and note that the file's tool-boundary assertion is a substring scan over tool
  *names*, so passing it is a naming fact, not proof of the boundary. The proof is that no tool
  reaches a privileged write.
- Nothing under `REGLAS QUE NO PODÉS ROMPER` changes. One line in the `CÓMO HABLÁS` state list
  (`pedido dado de baja -> …`), plus a tool docstring saying when to call it — an explicit request
  to cancel or undo *this* draft — and when not to: vague regret, which escalates to a person, and a
  change of quantity, which is a new order after the withdrawal rather than an edit.

# Done when

- `ruff check app demo tests deploy`; `pytest -q`; green in all five CI cells.
- **Read the collected test count before calling `pytest` green.** `app/graph.py` builds the Redis
  checkpointer at import: with no Redis Stack on `REDIS_URL`, nine modules fail at collection and
  pytest aborts having run **zero** tests. A run that collected nothing is not a green run. CI has
  Redis and is the real gate.
- Tests:
  - a `docstatus=1` document is left untouched, and the answer is the escalating one — assert the
    token, not only that nothing was written;
  - another customer's order is refused, and the two refusals compare **equal**;
  - a non-terminal `solicitud` blocks the withdrawal, checked with at least one state from
    `ABIERTOS` **and** with `REVISION_HUMANA`, because a subset condition passes one and fails the
    other;
  - a second withdrawal of the same order is a no-op, writes no second marker, and sends the owner
    nothing twice;
  - `soltar_reserva` returning `False` writes **no** marker, creates **no** owner-notice row, and
    leaves the row live;
  - the release row fires during quiet hours and the owner-notice row does not;
  - `dar_de_baja_pedido` is in `TOOLS_CLIENTES`.
- **Registering the marker turns three hand-written tests red** — the ones asserting marker counts.
  Updating them by hand is the point, per `MAPA.md`; generating them kills them. Read what each
  actually asserts before editing: one of them is derived from the registry and may not need a
  change at all.
- Mutations, per `CLAUDE.md` — each must kill **exactly one** test, and pick the mutation against
  what the test does *not* say:
  - the ownership comparison;
  - the `docstatus` guard **in the tool** (not in `soltar_reserva`: that helper has four production
    callers and mutating it kills their tests too — that number measures coupling, not protection);
  - the `TERMINALES` condition;
  - the `not ok` branch that withholds the marker;
  - the membership of `baja_de_pedido` in `_HABLAN_CON_ALGUIEN` — two rows, two consumers of the
    quiet-hours rule, so mutate each separately.
  Write each mutation and its result into the commit message.
- `python -m demo.piloto --modo offline` — 48 turns, no new failures (read the scenario count, not
  the wrapper's exit code). It needs Docker; if this container has none, say so plainly rather than
  reporting it as passed. Note that the demo replays scripted tool calls, so it exercises this
  feature only if a scenario is added for it — say which you did.
- `docs/MAPA.md` updated in the same commit for every module changed.
