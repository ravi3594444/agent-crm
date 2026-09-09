# Session opener — the agenda: one mechanism instead of a module per behaviour

Fresh session in `plus-agent/` of `agent-crm`. Everything needed is in this message.

**First commit:** save this whole message as `docs/prompt-agenda.md` on a branch off `main`, push it.

**This replaces the plazos brief.** plazos ships as a row in the new mechanism, not as a fourth sweep.

---

# Why

The same concept is hand-written four times today: `app/solicitudes.py::tick`, `app/pendientes.py::tick`, `app/digest.py::tick`, and plazos would be the fifth. Each one re-implements: a durable "this is due at T", a sweep that finds due items, exactly-once delivery, quiet hours, re-read-before-acting, and fail-closed on an unreadable value.

That is why a one-line change to the *idea* breaks ten tests in four files, and why every new time-based behaviour costs a module.

Write it once. Then "nudge the customer before the delivery", "close an abandoned draft", "chase the owner", "expire a request", and anything not yet imagined are **rows**, not modules.

**This is not new architecture — it is the `solicitudes.py` pattern extracted.** That module already does durable-record-plus-Redis-index-rebuilt-from-source, correctly, with its reasoning in the docstring. Read it first; you are generalising proven code, not inventing.

# What to build

**`app/agenda.py`** — a durable list of things due later.

A row is small and boring:

| field | meaning |
|---|---|
| `id` | unique |
| `sobre` | which document (Sales Order name) or which customer phone |
| `tipo` | what should happen — one of a **whitelist** |
| `vence` | when, UTC |
| `estado` | `pendiente` / `hecho` / `cancelado` |
| `params` | small JSON: which text key, which amount, nothing interpretive |

**Where it lives:** durable as an append-only ERPNext comment on the document (`[agenda] {...}`), with a Redis index for cheap reads, rebuilt from ERPNext when the index is empty. Exactly the discipline `solicitudes.py` documents and for the same reason: Redis is a cache and cannot be the source of truth for something promised to a customer.

**One sweep, called from the existing thread in `main.py`** — do not add a thread. For each due row: re-read the document, check the row is still relevant, dispatch to a small handler, mark done. All five shared concerns live **here and only here**:

- exactly-once, through `avisos.encolar` keyed by `(tipo, sobre)`
- quiet hours 22:00–07:00 `BUSINESS_TIMEZONE` — defer, never skip
- re-read before acting: an order confirmed thirty seconds ago is not nudged
- fail-closed: an unreadable row, document or limit means do nothing and log, never guess
- a bounded batch per tick so a backlog cannot stall the sweep

A handler is then ~10 lines: compose one message from data, return. **No handler touches Redis, timing, or idempotency.**

# What ships on it now

Two rows, and the second is plazos:

- **`cierre_borrador`** — port the existing closer from `pendientes.py` to a row. Proves the mechanism against behaviour that already has tests.
- **`aviso_antes_de_entrega`** — the plazos feature. New owner limit `AVISO_ANTES_DE_ENTREGA_HORAS` (default `3`, max `24`, `opcional`, English alias). When an order is created with a `delivery_date`, write a row due that many hours before it. Still unconfirmed when it fires → tell the customer honestly: *"Todavía no te lo pude confirmar para las 17, apenas lo vea el encargado te aviso."* No new day, no new hour, no price. Whichever of this and `PENDIENTE_AVISO_HORAS` comes first wins.

Plus one nearly-free addition: the **owner's alert names the deadline** (`Necesita respuesta antes de 14:00 o no llega`) and gets **one** re-ping as it approaches — which is itself just another row.

**Do not migrate `solicitudes.py` or the digest.** They work and they have tests. Migrate opportunistically later, or never. The win is that nothing *new* needs a module.

# The part that makes it a harness

Give the model one tool: **`recordar(sobre, cuando, por_que)`** — "come back to this order at this time, for this reason."

It **proposes** a row; Python validates and stores it. That is the four-layer rule intact: the model gets richer *perception and proposal*, never authority. A row can only ever cause a **message** — never a confirmation, a submit, a cancel, or a payment.

Guards, all in Python:
- `tipo` is forced to `seguimiento` (a follow-up message) — the model cannot choose a privileged row type
- `cuando` bounded: not in the past, not beyond a configured horizon (7 days)
- one live `seguimiento` per order at a time; a second replaces the first
- the reason is stored verbatim as **data** and shown to a person; it is never re-interpreted as an instruction
- quoted customer text is stripped first (`formato.sin_citas`), so a customer cannot plant a follow-up

Now a situation nobody coded works: *"llamame mañana sobre este pedido"* → the model sets a row → tomorrow the sweep wakes it and it follows up. That is the generality you want, and it is safe because the worst outcome is a message that didn't need sending.

# Why this stops "one line breaks ten tests"

The concept is tested **once** — durability, exactly-once, quiet hours, re-read, fail-closed, the batch cap. Each behaviour then has one or two tests about *its own* logic only. Adding a behaviour is a row type plus a handler plus two tests. Changing the concept touches one file, and the tests that break are the concept's tests — which is correct, not noise.

**While you are here, one cheap rule for the tests you write:** assert the **rule**, not the wording. `"the customer was notified before the deadline"`, not the exact sentence. Wording-pinning tests are the other half of why a one-line change breaks ten.

# Constraints

- Nothing here confirms, rejects, submits, cancels or moves money.
- No customer message promises a day, hour or price.
- New strings through `idioma.t` in ES and EN; the new limit gets an English alias.
- New behaviour defaults to **off** — `AVISO_ANTES_DE_ENTREGA_HORAS` and the closer stay `NINGUNO` until the owner arms them. Merging changes nothing a customer sees.
- No new thread; reuse the sweep thread in `main.py`.

# Done when

- `ruff check app demo tests`; `pytest -q` (Redis Stack at `REDIS_URL`, db 0); `python -m demo.piloto --modo offline` — 48 turns, no new failures (read the count, not the wrapper's exit code).
- The ported closer passes its **existing** tests unchanged. That is the proof the mechanism is equivalent.
- Four tests on the mechanism: a row survives a Redis flush and is rebuilt from ERPNext; a due row whose order was confirmed in between does nothing; a row due at 23:00 fires at 07:00; an unreadable row is skipped and logged, not guessed.
- Two on plazos: a 17:00 delivery with a 3-hour lead fires at 14:00 and not at the flat-hours time; a missing `delivery_date` falls back instead of losing the deadline.
- Two on `recordar`: the model cannot create a non-`seguimiento` row; a date in the past or beyond the horizon is refused.
- **The check that counts:** on the live number, place an order for a delivery a few hours out, leave it unconfirmed, and watch the customer get told before the deadline and the owner's alert carry the time.
