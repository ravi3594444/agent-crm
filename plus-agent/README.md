# plus-agent

WhatsApp order agent for the Plus dairy stack. Runs as one container on the
same server as ERPNext, talking to it over the internal Docker network.

**Before you deploy anything, read [PRUEBAS.md](PRUEBAS.md).** It is a
staged verification guide — nothing advances until the current stage passes.
Start with `make test` (about two seconds, no credentials needed).

## The point of this repo

LangGraph is one line in `requirements.txt`. Everything else here is yours.

| File | What it is |
|---|---|
| `app/erpnext.py` | REST client. The agent never touches MariaDB. |
| `app/telefono.py` | Argentine phone normalization — **without this nothing matches** |
| `app/clientes.py` | Customer lookup by phone, against hand-entered data |
| `app/tools/catalogo.py` | Read tools — products, stock, order status |
| `app/tools/pedidos.py` | Write tools — **drafts only** |
| `app/tools/alcance.py` | **The authorization boundary** — which customer a tool may act as |
| `app/prompts.py` | Rioplatense Spanish system prompt |
| `app/graph.py` | The two agents |
| `app/main.py` | Webhook: signature, idempotency, background processing |
| `app/whatsapp.py` | Outbound messages, with retries and status checks |
| `app/tools/gerencia.py` | Management read tools (owner assistant) |
| `app/router.py` | Phone-based agent routing (security boundary) |
| `app/briefing.py` | 07:00 WhatsApp morning briefing |
| `app/tools/captura.py` | **Offline-sale capture** — the hard part |
| `app/policy.py` | **Auto-confirm engine** — deterministic, LLM-proof |
| `app/lock.py` | Serializes evaluate-then-submit, so stock can't double-sell |
| `app/notificar.py` | WhatsApp approval buttons and alerts |
| `app/aprobacion.py` | Button taps -> ERPNext submit |
| `app/formato.py` | `$12.000`, not `$12,000` — an Argentine reads those differently |
| `app/log.py` | Logging, so silent-by-design failures are still visible |
| `tests/` | 245 tests. No ERPNext, no Redis, no LLM, ~2 seconds. |

## Two agents, one webhook

Route by phone number in `router.py`:

- **staff phone** -> management agent: broad read across sales, stock,
  receivables, customers. Stronger model. Still cannot submit.
- **anyone else** -> customer agent: narrow tools, draft-only writes,
  **scoped to that one customer**.

This is a **security boundary**, not a convenience. A customer-facing bot
with full system read is one prompt injection away from leaking the customer
list, margins and supplier prices.

### The customer is not a parameter

The customer code is resolved from the phone number by the webhook and
travels in `config.configurable`, which the model can neither read nor
write. `crear_pedido` has no `cliente` argument at all — check
`crear_pedido.args`, and see `tests/test_autorizacion.py`.

This matters because the earlier design passed the customer code through the
system prompt and let the model supply it as a tool argument. The only thing
standing between a customer and someone else's order history was a line of
prompt text. A prompt is not an access control.

## Removing the wait

Nobody waits for milk. Three mechanisms, in order of impact:

**1. Auto-confirm by exception (`app/policy.py`)**
Most orders are boring — known customer, usual products, list price, in
stock. Those confirm INSTANTLY. Only unusual ones wake a human. Every rule
must pass:

| Rule | Default |
|---|---|
| Order total under ceiling | `AUTO_CONFIRM_MAX` |
| Not wildly above customer's own average | 2x |
| Customer has real order history | 3+ confirmed orders |
| No overdue balance | 0 |
| Stock above buffer, **minus what drafts already promised** | — |
| List price on the order's own price list, no document discount | — |
| Delivery date neither in the past nor beyond 30 days | — |

**The safety property:** `policy.py` is deterministic Python. It never sees
the customer's words. The agent has no submit tool and no way to call it.
Prompt injection cannot widen the envelope — it can only produce a draft
that then fails the rules. `tests/test_policy.py` asserts this directly by
injecting hostile text into every field of a Sales Order and checking the
decision does not move.

Evaluate-and-submit runs under a Redis lock (`app/lock.py`). Without it, two
customers ordering the last of something both pass the check and both get
confirmed — because in ERPNext a **draft Sales Order does not reserve
stock**; `reserved_qty` only rises on submit.

Start `AUTO_CONFIRM_MAX=0` (everything reviewed). Watch `make decisiones`
for a week, then raise it a notch at a time. See stage 9 of PRUEBAS.md.

**2. One-tap approval (`app/notificar.py`, `app/aprobacion.py`)**
Orders that DO need him arrive as a WhatsApp with buttons —
[Confirmar] [Rechazar] [Ver detalle]. He taps from his lock screen, ERPNext
submits, the customer is notified automatically. Two seconds, no app.

**Rechazar notifies the customer too.** A rejected order that leaves the
customer waiting in silence is the worst outcome this system can produce.

**3. "Lo de siempre" (`pedido_habitual`)**
Dairy customers reorder the same thing forever. One message, one tap, done.

**Plus: never leave silence.** Every inbound message gets a reply — including
voice notes, photos and stickers, which the bot cannot read but always
answers. Even a held order gets an instant reply with a real order number —
*"pedido SO-0042 tomado, te confirmo en unos minutos."* The wait people hate
is the silence, not the delay.

## Inventory truth — read this before launch

He sells offline: counter, truck, phone, cash. None of it touches ERPNext,
so system stock drifts from reality within a day. **A bot that promises milk
already sitting in someone's fridge is worse than no bot** — it costs him
customers.

The design decouples order-taking from stock accuracy:

**Phase 1 — `STOCK_CONFIABLE=false` (launch here)**
The bot takes orders and never promises stock: *"te lo cargo, el equipo te
confirma disponibilidad."* Honest, works from day one with zero inventory
accuracy, and still delivers the real day-one win: no customer message is
ever lost again.

**Phase 2 — capture works, flip to `true`**
Staff report sales by WhatsApp to the management agent, in their own words:

- "vendí 20 litros a Don José"  -> `registrar_venta_offline` -> draft Sales Invoice
- "quedan 12 kilos de queso"    -> `contar_stock`            -> draft Stock Reconciliation
- "entregué el SO-0042"         -> `confirmar_entrega`       -> draft Delivery Note

No data entry, no new app, no training. The same WhatsApp they already use.

Even then the bot answers in **levels, not numbers** — DISPONIBLE /
POCO STOCK / SIN STOCK — with `STOCK_BUFFER_PCT` (default 20%) absorbing
sales not yet loaded.

**Daily rhythm that makes it work**

| When | Who | What |
|---|---|---|
| 07:00 | system | Morning briefing to owner |
| 07:15 | whoever opens | Counts key products -> `contar_stock` |
| all day | counter/truck | Reports sales as they happen |
| all day | owner | Confirms drafts from his phone |
| 18:00 | owner | Clears remaining drafts |

If the 07:15 count does not happen, do not flip `STOCK_CONFIABLE` to true.

## The rules baked in

1. **No SQL.** Everything goes through `erpnext.py` over REST, with filters
   serialized as real JSON (`json.dumps`) so an apostrophe in a customer's
   name cannot break the query or reshape the filter.
2. **Draft only.** `create_doc` forces `docstatus: 0`. The agent's ERPNext
   Role has no submit permission, so this is enforced by ERPNext — not by
   the prompt.
3. **Submit is a separate identity.** `ERPNEXT_POLICY_API_*` is the only
   credential that can submit, it is not importable as an agent tool, and
   `make check-env` refuses to start if it equals the agent's key.
4. **One customer per conversation.** Tools act only on the customer resolved
   from the phone number.
5. **Idempotency.** Meta retries webhooks. A short claim key stops a retry
   becoming two orders — but a message that died mid-processing is released
   so the retry can pick it up instead of being lost forever.
6. **Audit.** Every AI write leaves a Comment on the record.
7. **Never silence.** Every message type gets an answer.
8. **Nothing fails silently.** Failures that must not break a customer order
   (audit notes, notifications) are caught — and logged.

## Setup

1. **In ERPNext, create TWO users.** This is the most important step, and the
   difference between them is the main guardrail of the system.

   **a) The agent** — Role "Agente IA": grant Create+Read on Item, Bin,
   Customer, Lead, Sales Order, ToDo, Sales Invoice, Stock Reconciliation,
   Delivery Note. **Do NOT grant Submit.** This is the identity the LLM
   drives. Create user `agente@…`, generate API key + secret ->
   `ERPNEXT_API_KEY` / `ERPNEXT_API_SECRET`.

   **b) The policy** — a second user with Submit on Sales Order and nothing
   more. Only `app/policy.py` (via `app/tools/pedidos.py`) and
   `app/aprobacion.py` reach it, after every deterministic rule passes or a
   human on the staff list taps a button. Create user `politica@…` ->
   `ERPNEXT_POLICY_API_KEY` / `ERPNEXT_POLICY_API_SECRET`.

   If you use the same key for both, the agent can submit and the guardrail
   is gone. `make check-env` will refuse.

2. `cp .env.example .env` and fill it in. Then `make check-env`.

3. `make up` — brings up the agent and Redis, waits for `/ready`.
   **Redis must be Redis Stack, not plain Redis.** LangGraph's checkpointer
   stores checkpoints with RedisJSON and queries them with RediSearch, so a
   plain `redis:7-alpine` dies at boot with
   `RedisSearchError: unknown command 'FT.INFO'`. The bundled
   `docker-compose.yml` uses `redis/redis-stack-server`; if you point at your
   own Redis, check it first with
   `redis-cli -u "$REDIS_URL" module list` — it must list `ReJSON` and `search`.
   To run inside the existing ERPNext stack instead, copy the `agente`
   service from `docker-compose.yml` into that stack's compose file, on the
   same network as `backend`, with `ERPNEXT_URL=http://backend:8000`.

4. Point the Meta webhook at `https://…/webhook/whatsapp`, verify token =
   `META_VERIFY_TOKEN`.

5. Install the morning briefing cron: see `deploy/crontab`. **Check the
   host's timezone** — 07:00 in Argentina is 10:00 UTC.

6. Work through [PRUEBAS.md](PRUEBAS.md) in order.

## Operating it

```bash
make test          # 238 tests, ~2s, no credentials needed
make check         # what CI runs (lint + tests)
make check-env     # is .env complete, are the two credentials distinct
make up            # start, wait for /ready
make logs          # follow
make decisiones    # what the auto-confirm policy decided, per order
make briefing      # send the morning briefing now
make seed          # demo catalog + customers
```

Two health endpoints: `/health` is liveness and touches nothing. `/ready`
checks Redis and ERPNext and returns 503 if either is down — that is the one
to look at when "the bot is not answering."

## Deploying client #2

Copy `.env`, change `NOMBRE_NEGOCIO`, point at that client's ERPNext,
edit the prompt. No new build.

## Not done yet

- Outbound confirmation when a human submits the order **in the ERPNext UI**
  (Frappe Webhook on `Sales Order` `on_submit` -> `POST /hooks/order-confirmed`).
  Approving from the WhatsApp button already notifies the customer.
- Media/audio transcription. Voice notes currently get an honest "no puedo
  escuchar audios todavía, escribime" instead of silence — but Argentine
  customers send them constantly, so transcription is the highest-value
  feature left.
- Business-hours handling (the prompt knows the hours; nothing enforces them).
- Rate limiting per phone number. Nothing stops one number burning LLM budget.
- Conversation-history trimming. The Redis checkpointer grows per thread
  without bound; cost per message creeps up on long-running conversations.
- **Evals: a fixed set of ~30 real customer messages, run on every deploy.**
  The plumbing is covered by tests now; the language is not. See the last
  section of PRUEBAS.md — this is the highest-value thing to collect.
