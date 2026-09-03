# plus-agent

WhatsApp order agent for the Plus dairy stack. Runs as one container on the
same server as ERPNext, talking to it over the internal Docker network.

**Before you deploy anything, read [PRUEBAS.md](PRUEBAS.md).** It is a
staged verification guide — nothing advances until the current stage passes.
Start with `make test` (about a second, no credentials, no Redis, no network).

## Architecture

The service keeps the LLM behind narrow ERPNext tools, a deterministic
auto-confirm policy, a durable Redis-backed webhook queue and signed Meta
webhooks. ERPNext remains the system of record; neither agent accesses MariaDB
or invents an order number.

## The files

LangGraph is one line in `requirements.txt`. Everything else here is yours.

| File | What it is |
|---|---|
| `app/main.py` | Webhook: signature, size limit, idempotency, durable Redis queue, worker thread with lease/heartbeat, status webhooks |
| `app/erpnext.py` | REST client with **three credential scopes** (customer / manager / policy) selected through a `contextvars` scope. The agent never touches MariaDB. |
| `app/runtime_context.py` | Per-request identity (customer code, inbound message, actor phone) that tools read for authorization — the model cannot forge it |
| `app/router.py` | Phone-based agent routing (`TELEFONOS_EQUIPO`) — a security boundary |
| `app/graph.py` | The two agents, the Redis checkpointer, tool-error containment |
| `app/prompts.py`, `app/prompts_gerencia.py` | Rioplatense Spanish system prompts |
| `app/tools/catalogo.py` | Read tools — products, prices, stock levels, order status |
| `app/tools/pedidos.py` | Write tools — **drafts only** — then hands the order to the policy |
| `app/tools/gerencia.py` | Management read tools (owner assistant) |
| `app/tools/captura.py` | **Offline-sale capture** — the hard part |
| `app/clientes.py` | Customer lookup by phone (`buscar_por_telefono`), against hand-entered data |
| `app/policy.py` | **Auto-confirm engine** — deterministic, LLM-proof, fail-closed |
| `app/locks.py` | Redis distributed locks: evaluate-then-submit is serialized so stock cannot double-sell |
| `app/notificar.py` | WhatsApp template alerts to staff with approval buttons |
| `app/aprobacion.py` | Button taps -> ERPNext submit -> templated customer confirmation |
| `app/outbound_status.py` | Delivery tracking of every outbound message (sent / delivered / read / failed) |
| `app/whatsapp.py` | Outbound messages and templates |
| `app/briefing.py` | 07:00 WhatsApp morning briefing (`deploy/crontab`) |
| `deploy/seed_dairy.py` | Demo catalog and customers for an empty staging ERPNext |
| `deploy/crontab` | Host cron line for the briefing |
| `docker-compose.yml` | Agent + Redis Stack (+ `briefing` on demand) |
| `Dockerfile`, `Makefile`, `.env.example`, `pyproject.toml` | Build, shortcuts, configuration, lint config |
| `.github/workflows/ci.yml` | Lint, tests, image build, container boot against a real Redis Stack |
| `tests/` | 260 tests + 3 strict xfails that document known gaps. No ERPNext, no Redis, no LLM, ~1 second. |

## Two agents, one webhook

Route by phone number in `router.py`:

- **staff phone** -> management agent: broad read across sales, stock,
  receivables, customers. Stronger model. Still cannot submit.
- **anyone else** -> customer agent: narrow, account-scoped tools and
  draft-only writes.

This is a **security boundary**, not a convenience. A customer-facing bot with
full system read is one prompt injection away from leaking the customer list,
margins and supplier prices. Separate agents, separate ERPNext users,
separate roles.

## Three ERPNext users, not one

The single most important configuration decision. Each is a separate ERPNext
user with its own API key, and `make check-env` refuses to proceed if any two
of them share a key.

| `.env` pair | Who uses it | Can |
|---|---|---|
| `ERPNEXT_API_KEY` / `_SECRET` | the **customer agent** (the LLM that talks to customers) | Read + draft Create on Item, Bin, Customer, Lead, Sales Order, ToDo, Item Price, Comment. **No Submit.** |
| `ERPNEXT_MANAGER_API_KEY` / `_SECRET` | the **management agent** (staff phones), only while a `gerencia`/`captura` tool runs | Broad reads (sales, stock, receivables, customers) + draft Sales Invoice, Stock Reconciliation, Delivery Note, ToDo, Comment. **No Submit.** |
| `ERPNEXT_POLICY_API_KEY` / `_SECRET` | `app/policy.py` (via `app/tools/pedidos.py`) and `app/aprobacion.py` — **never a tool** | The privileged policy reports and the **only Submit** on Sales Order |

`app/erpnext.py` switches between them with a `contextvars` scope, so a tool
running for a customer physically cannot reach the manager or policy client.

## Removing the wait

Nobody waits for milk. Three mechanisms, in order of impact:

**1. Auto-confirm by exception (`app/policy.py`)**
Most orders are boring — known customer, usual products, list price, in stock.
Those confirm INSTANTLY. Only unusual ones wake a human. Every rule must pass:

| Rule | Default | Who sets it |
|---|---|---|
| Order total under ceiling | 0 = off | owner, from WhatsApp |
| No more than X of one product, in stock units | 0 = nothing passes | owner |
| Stock above the buffer, minus what other open orders already promised | 20% | owner |
| A customer with no real history stays under their own ceiling | 0 = they always wait | owner |
| Delivery address allowed: with both `ZONAS_ENTREGA_CP` and `ZONAS_ENTREGA_LOCALIDADES` set, postcode AND locality must be present and listed; with one list, that list decides; contradictions, missing or unreadable values stay pending | no zones = nothing delivers | `ZONAS_ENTREGA_*` |
| No overdue balance | 0 | owner |
| Any discount goes to a person | yes | owner |
| If not, line + document discount combined stays under the cap | 5% | owner |
| Not wildly above customer's own average | 2x | `AUTO_CONFIRM_MULT` |
| Enough order history to have an average | 3 confirmed orders | `AUTO_CONFIRM_MIN_ORDERS` |
| List price, no rate above it | — | — |

The ones marked *owner* are his to change, from the same WhatsApp thread he
already uses: "mostrame los límites", "subime el tope a 30 mil". A change needs
a four-digit code he types back, applies from the next order with no restart,
and is recorded with his number, the time and both values
(`historial_limites`), in Redis and on the ERPNext Company document. The
`.env` values are only what the system starts with. If the limits cannot be
read — or the store comes back empty while ERPNext remembers a change — nothing
auto-confirms, because a bootstrap value can be looser than what he set.
Proof that a real restart keeps them: `deploy/verificar_persistencia_limites.sh`.

**The safety property:** `policy.py` is deterministic Python. It never sees the
customer's words, and it reads the owner's limits itself, on every evaluation,
including the last one inside the submit lock. The management agent can move a
number the owner asked it to move; it cannot decide an order. The agent has no submit tool and no way to call it. Prompt
injection cannot widen the envelope — it can only produce a draft that then
fails the rules. That is how you get instant confirmation without handing an
LLM the keys.

Evaluate-and-submit runs under a Redis lock (`app/locks.py`) and re-reads the
order while holding it. Without that, two customers ordering the last of
something both pass the check and both get confirmed — in ERPNext a draft
Sales Order does not reserve stock; `reserved_qty` only rises on submit.

The lock serializes the check, but it cannot make visible a promise ERPNext
does not count. So the rule also subtracts, as a virtual reservation, the
quantity promised in every OTHER order that still holds it
(`policy._comprometido_en_borradores`, read with the policy identity, inside
the same lock). Which orders those are is asked of ERPNext first: `docstatus`
0, same company, and `status` not in Closed / Cancelled / On Hold — the same
three ERPNext's own `get_reserved_qty` skips, and where a manual rejection
tries to leave the draft (whether ERPNext keeps that status on a draft is
version-dependent and unverified on this installation — the rejection's audit
comment records what actually happened). Two exclusions matter as much as the
subtraction: the order being evaluated (its quantity is the one being checked)
and any order asked for *later* — FIFO on `(creation, order id)`, because two
drafts can share a timestamp and on the timestamp alone each would defer to the
other, leaving two customers waiting for units the dairy has. If the lookup fails, is
truncated, or returns a quantity that cannot be converted to the item's stock
unit, the order stays a draft for a person — uncertain stock is never a
confirmation.

Start `AUTO_CONFIRM_MAX=0` (everything reviewed). Watch `make decisiones` for
a week, then raise it a notch at a time (stage 9 of PRUEBAS.md). By month two
most orders never touch him.

**2. One-tap approval (`app/notificar.py`, `app/aprobacion.py`)**
Orders that need review arrive on the owner's WhatsApp with
[Confirmar] [Ver detalle]. Preferred channel: an approved utility template,
because a customer's message does not open a 24-hour window for the owner's
separate phone. While the templates are not yet approved, the alert is sent
free-form with the same two buttons **only if the owner wrote to the bot in the
last 24 hours** (any message, e.g. "pendientes?"); otherwise it fails closed and
the order stays visible as a draft in ERPNext with an audit comment saying so.

The owner can also act by text, without buttons and without the LLM:

```
ver SAL-ORD-2026-00008                       (also: detalle)
confirmar SAL-ORD-2026-00008                 (also: ok / aprobar / apruebo)
rechazar SAL-ORD-2026-00008                  (also: rechazo / no) — the customer is told
preparar SAL-ORD-2026-00008                  draft Delivery Note for a CONFIRMED order
despachar SAL-ORD-2026-00008                 submits that draft: a separate human step
cancelar SAL-ORD-2026-00008 <reason>         confirmed order, within CANCELACION_HORAS (24 h)
despreparar SAL-ORD-2026-00008               deletes the agent's own draft Delivery Note
```

`cancelar` (`app/decisiones.py`) re-checks the sender, requires a reason, and
under the distributed lock re-reads the order with the policy identity: it must
be submitted and confirmed by this system less than 24 hours ago. That deadline
is a durable ERPNext comment on the order (`app/confirmacion.py`), not a Redis
key, so it survives a restart or an empty Redis; an order confirmed directly in
ERPNext has no such record and is refused. Linked documents get three different
answers, and none of them is a cascade: a **submitted** Delivery Note or Sales
Invoice blocks the cancellation outright, a **draft** Delivery Note sends the
manager to `despreparar` first, and a **draft** Sales Invoice goes to ERPNext.
The cancellation is audited with the reason, repeated commands are idempotent,
and the customer is told once.

`despreparar` undoes a preparation, and it is the only command allowed to remove
a linked draft. It deletes ONE draft Delivery Note and only if the agent created
it (`preparar` stamps a marker in its remarks) and nobody has edited it: same
customer, same company, exactly the order's own lines and quantities. Several
drafts, a hand-made one, an edited one, any invoice or anything submitted are
refused and sent to ERPNext. The reason and the draft's lines are written onto
the Sales Order **before** the deletion, since a deleted document cannot be
commented on. `erpnext.policy_delete_doc` re-reads the document and refuses
anything that is not a draft.

**The customer's confirmation is data, not a prompt.** After a successful
confirmation — automatic or human — the order id, lines, total and fulfilment
details are built from the ERPNext document and queued in `app/avisos.py` under
an idempotency key of (event, order). A worker thread delivers it inside the
customer's own 24-hour window, falls back to an optional template outside it,
retries transient failures with bounded backoff, and after
`AVISOS_MAX_INTENTOS` parks the notice and opens one deduplicated ERPNext ToDo.
The model may add conversational text in the same turn; it is never responsible
for the confirmation itself.

**Templates are optional in the pilot.** Customers always write first, so every
reply to them (acknowledgement, confirmation, rejection, cancellation) goes out
as free text inside their own 24-hour window. The owner keeps their window open
by writing to the bot each day, which is enough for alerts and the 18:00 digest.
A template is only needed when someone lets more than 24 hours pass; a missing
template is an AVISO in `make check-env`, never a blocker. The confirmed-order
alert is informational: nothing to answer, the order stays confirmed, and a
customer who read "confirmado" in the conversation never gets a second
confirmation message.

Every command runs a deterministic handler in `app/aprobacion.py` /
`app/decisiones.py` after `router.es_equipo()` authenticates the sender; none
of them is an LLM tool, and the LLM has no submit or dispatch tool at all
(`tests/test_frontera_decisiones.py`, `tests/test_etapa_2e.py`).

**Confirmed-order notice (Stage 2e).** After every confirmation, automatic or
human, the manager receives exactly ONE notice per Sales Order (a Redis claim
keyed by the order id, released only if nobody could be reached): order id,
customer, items with quantities and UOM, total with currency, delivery address
and date, source (automatic or human) and confirmation time. Template when
`WHATSAPP_STAFF_CONFIRMED_TEMPLATE` is configured, free text inside the
manager's own 24-hour window otherwise. A notice that reaches nobody is parked
in the Redis list `wa:{inbound}:dead-notify` and opens one deduplicated ERPNext
ToDo; the same applies to pending-order alerts and exception alerts.

**18:00 digest (Stage 2e, `app/digest.py`).** Deterministic, no model: confirmed
orders waiting for preparation/dispatch, orders waiting for the manager, stock
counts expired or about to expire, and failed notifications / dead-letter
counts. The agent sends it once a day from `DIGEST_HORA` in `BUSINESS_TIMEZONE`
(`DIGEST_ACTIVO=false` disables the in-process scheduler); `python -m app.digest`
or the `digest` job in `deploy/crontab` sends it on demand. Both share one
per-day marker in Redis.

Both paths run the same authorized handler (`manejar_boton`): only numbers in
`TELEFONOS_EQUIPO` are accepted, a second tap is idempotent, and a successful
confirmation starts the customer confirmation (template, or free-form while
the customer's own 24-hour window is open).

**3. "Lo de siempre" (`pedido_habitual`)**
Dairy customers reorder the same thing forever. The bot retrieves only that
authenticated account's last order, then reconfirms products, units and a new
delivery date before creating anything.

**Plus: a stuck recipient never blocks the queue.** A final reply that Meta
rejects permanently (expired or invalid token, error 190; recipient not in the
test allow-list, 131030; closed 24-hour window, 131047; template errors) is
parked on the first attempt in the Redis list `wa:{inbound}:dead`, an audit
comment naming the HTTP status and Meta code is added to any order quoted in
that reply, and the FIFO moves on to the next customer. Only timeouts, HTTP 429
and 5xx (plus Meta's own rate-limit codes) are retried, with exponential
backoff from 2 s up to `WHATSAPP_RETRY_MAX_SECONDS`, at most
`WHATSAPP_SEND_MAX_ATTEMPTS` times, and then parked the same way. Redis
failures never count towards that limit.

**Plus: two visible replies, never silence.** As soon as a text request is
accepted for processing, the customer gets a short acknowledgement such as
*"Dame un momento mientras verifico disponibilidad."* The agent then sends a
separate final result. A created draft must include its real ERPNext number —
for example, *"pedido SO-0042 tomado; te confirmamos en unos minutos"* — while
an auto-confirmed order must say explicitly that it is confirmed. The first
message means only "we are checking"; the order number and status in the final
message are the customer's proof that the request reached ERPNext.

## Inventory truth — read this before launch

He sells offline: counter, truck, phone, cash. None of it touches ERPNext,
so system stock drifts from reality within a day. **A bot that promises milk
already sitting in someone's fridge is worse than no bot** — it costs him
customers.

The design decouples order-taking from stock accuracy:

**Phase 1 — `STOCK_CONFIABLE=false` (launch here)**
The bot takes orders and never promises stock: *"te lo cargo, el equipo te
confirma disponibilidad."* Honest, works from day one with zero inventory
accuracy, while accepted requests remain in the durable queue across worker
restarts and outbound-send failures.

**Phase 2 — capture works, flip to `true`**
Staff report sales by WhatsApp to the management agent, in their own words:

- "vendí 20 litros a Don José"  -> `registrar_venta_offline` -> draft Sales Invoice
- "quedan 12 kilos de queso"    -> `contar_stock`            -> draft Stock Reconciliation
- "entregué el SO-0042"         -> `confirmar_entrega`       -> draft Delivery Note

No data entry, no new app, no training. The same WhatsApp they already use.

**Trust in the inventory is earned per product, and it expires.**
`STOCK_CONFIABLE=true` used to be a promise written once in the `.env`: the
system then promised stock for ever, even if nobody had counted anything in
three weeks. Now a product is trustworthy only while somebody has **counted
that product and confirmed the adjustment** within `STOCK_CONFIABLE_HORAS`
(default 24). Counting the milk says nothing about the cheese. Without a recent
confirmed count the bot says so and the order waits for a person — and
`STOCK_CONFIABLE=false` still turns everything off in one move.

The count is a draft until a human confirms it: `contar_stock` turns "quedan 12
kilos" into a draft Stock Reconciliation and sends **one button**. The tap
submits it with the policy credential — neither agent has Submit permission,
and `decisiones.confirmar_conteo` is in no tool list.

Even then the bot answers in **levels, not numbers** — DISPONIBLE /
POCO STOCK / SIN STOCK — with `STOCK_BUFFER_PCT` (default 20%) absorbing
sales not yet loaded.
The rule that actually confirms an order with nobody watching is stricter: it
subtracts submitted orders (`reserved_qty`), the safety buffer, AND everything
promised in other open orders, all while holding the business lock.

**Daily rhythm that makes it work**

| When | Who | What |
|---|---|---|
| 07:00 | system | Morning briefing to owner (`deploy/crontab`) |
| 07:15 | whoever opens | Counts key products -> `contar_stock` -> taps *Confirmar conteo* |
| all day | counter/truck | Reports sales as they happen |
| all day | owner | Confirms drafts from his phone |
| 18:00 | owner | Clears remaining drafts |

If the 07:15 count does not happen, do not flip `STOCK_CONFIABLE` to true.

## The four rules baked in

1. **No SQL.** Everything goes through `erpnext.py` over REST.
2. **Draft only.** `create_doc` forces `docstatus: 0`. The agent's ERPNext
   Role has no submit permission, so this is enforced by ERPNext — not by
   the prompt. Prompt injection cannot escalate it. Submit lives behind a
   third identity that no tool can import.
3. **Idempotency.** Meta retries webhooks. Hashed inbound-message markers in
   Redis plus a deterministic ERPNext purchase-order reference stop one retry
   becoming two orders without storing phone numbers in Redis key names.
4. **Audit.** AI-created orders carry a hashed inbound reference and the
   service adds best-effort ERPNext audit comments for writes and delivery
   failures.

## Setup

1. **In ERPNext, create THREE users** (see the table above). This is the most
   important step.
   - **a) The customer agent** — Role "Agente IA": grant only the reads and
     draft creates used by the customer tools: Item, Bin, Customer, Lead,
     Sales Order, ToDo, Item Price and Comment. **Do not grant Submit.**
     Generate its API key and secret -> `ERPNEXT_API_KEY` / `ERPNEXT_API_SECRET`.
   - **b) The management agent** — the required broad reads plus draft-only
     Sales Invoice, Stock Reconciliation, Delivery Note, ToDo and Comment
     writes, but **no Submit** -> `ERPNEXT_MANAGER_API_KEY` / `_SECRET`.
   - **c) The policy** — the narrowly scoped report reads plus Submit on
     Sales Order, nothing more -> `ERPNEXT_POLICY_API_KEY` / `_SECRET`.
     Never reuse the policy key in either LLM-facing agent.
2. `cp .env.example .env`, immediately run `chmod 600 .env`, and fill in every
   empty value. Every variable the code reads is documented in `.env.example`.
   Prefer the deployment platform's secret manager. Never commit `.env`, API
   credentials, or WhatsApp tokens. Then `make check-env`.
3. Set `ERPNEXT_COMPANY` and `ERPNEXT_WAREHOUSE` to the exact ERPNext names.
   The warehouse must be enabled, must not be a group, and must belong to that
   company. Order creation fails closed without both; explicit values prevent a
   new or unrelated warehouse from silently becoming the order default.
4. Set `BUSINESS_TIMEZONE` to the client's IANA timezone, such as
   `America/Argentina/Buenos_Aires`. The host may run in UTC; customer dates and
   business rules must still use the client's local date.
5. Set `AUTO_CONFIRM_PRICE_LIST` and `AUTO_CONFIRM_CURRENCY` to the exact
   authorized ERPNext selling price list and currency. Auto-confirmation fails
   closed if either is missing or if a line has a different UOM, rate, validity
   window, customer-specific price or discount.
6. `make up` — brings up the agent and Redis Stack on port **8081** (8080 is
   ERPNext) and waits for `/health`. To run inside the existing ERPNext stack
   instead, copy the `agente` and `redis` services from `docker-compose.yml`
   into that stack's compose file, on the same network as `backend`, with
   `ERPNEXT_URL=http://backend:8000`. Standalone against an ERPNext on the
   host, use `ERPNEXT_URL=http://host.docker.internal:8080`.
7. Set `TELEFONOS_EQUIPO` to the owner's/test staff number in international
   format. An empty value disables management routing and staff alerts.
8. Point the Meta callback at `https://…/webhook/whatsapp`, subscribe the WABA
   to the `messages` field, and use the same `META_VERIFY_TOKEN` on both sides.
9. Install the morning briefing cron: see `deploy/crontab`. **Check the host's
   timezone** — 07:00 in Argentina is 10:00 UTC.
10. Work through [PRUEBAS.md](PRUEBAS.md) in order.

### Required WhatsApp templates

Create and obtain Meta approval for these templates before enabling staff
alerts. The names and locale must match the corresponding `.env` values, and
each recipient must have opted in to the relevant order/status messages.

- `WHATSAPP_STAFF_PENDING_TEMPLATE`: seven body variables in this order —
  order number, status, customer, line summary, total, delivery date and review
  reason — plus two quick-reply buttons titled Confirmar and Ver detalle.
- `WHATSAPP_STAFF_CONFIRMED_TEMPLATE`: the same seven body variables, without
  action buttons.
- `WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE`: two body variables — order number and
  delivery date.

The service supplies an order-specific payload for each staff quick reply.
Until a template is configured, an alert is sent free-form only to a recipient
whose own 24-hour window is open (they wrote to the bot within 24 hours, tracked
by hashed phone in Redis); otherwise nothing is attempted, the Sales Order
remains in ERPNext, and an audit comment records the required manual follow-up.
The startup log prints one `[config] WARN` line per missing template.

### Models

Both agents run on Qwen through Alibaba Model Studio (DashScope) using its
OpenAI-compatible endpoint, with ONE key (`DASHSCOPE_API_KEY`). Defaults, all
overridable from the environment and nowhere else (`app/modelos.py`):

| | Sales agent | Management agent |
|---|---|---|
| model | `QWEN_SALES_MODEL` = `qwen3.7-plus-2026-05-26` | `QWEN_MANAGER_MODEL` = `qwen3.8-max` (documented name); set `qwen3.8-max-0902` once `make verificar-qwen` confirms it on your endpoint |
| reasoning | `QWEN_THINKING_CLIENTES` = `false` | `QWEN_THINKING_GERENCIA` = `false`; set `true` only when analysis needs it (`QWEN_THINKING_BUDGET` caps it; DashScope requires streaming with thinking, which the code enables with it) |
| endpoint / timeouts | `DASHSCOPE_BASE_URL`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` | same |

The models are built at import, so a missing key or a `provider:model`-style
name stops the process with the variable named. There is deliberately no
fallback provider and no Gemini/Anthropic package in `requirements.txt`. The
model only converses and calls tools: stock, price, discount, credit, delivery,
confirmation and dispatch decisions stay in Python (`policy.py`, `entrega.py`,
`inventario.py`, `decisiones.py`). `tests/test_modelos.py` mocks the provider
boundary, so CI never calls DashScope.

### Redis Stack is required

`langgraph-checkpoint-redis` stores JSON checkpoints and creates search
indexes, so it requires both RedisJSON and RediSearch. A plain `redis:7-alpine`
dies at boot with `RedisSearchError: unknown command 'FT.INFO'`. Redis 8
includes the modules; for older versions use Redis Stack — the bundled
`docker-compose.yml` uses `redis/redis-stack-server` with AOF persistence and a
non-evicting memory policy. If you point at your own Redis, check it first with
`redis-cli -u "$REDIS_URL" module list` — it must list `ReJSON` and `search`.

Use a dedicated instance at logical database 0 (`.../0`) rather than sharing
one with unrelated applications: without durable AOF and `noeviction`, Redis
cannot guarantee that an HTTP-accepted webhook survives a Redis/container loss.
Restrict Redis network access because queued events temporarily contain
recipient numbers and message text. `CONVERSATION_TTL_DAYS` limits how long
inactive LangGraph conversation checkpoints are retained and refreshes the TTL
when a conversation continues. A server that reaches `/health` has successfully
imported the app and initialized the checkpointer; it has not yet proved
ERPNext or WhatsApp connectivity.

### What the startup log tells you

Right after "Application startup complete" the agent prints its readiness:

- `[config] WARN TELEFONOS_EQUIPO vacío ...` — no manager loop at all.
- `[config] WARN WHATSAPP_*_TEMPLATE vacío ...` — alerts only go out free-form
  inside a 24-hour window.
- `[config] WARN AUTO_CONFIRM_PRICE_LIST/AUTO_CONFIRM_CURRENCY vacíos` — the
  bot answers "precio a confirmar" to every price question.
- `[whatsapp] OK credenciales de WhatsApp válidas` or
  `[whatsapp] ERROR Meta rechazó WHATSAPP_TOKEN ...` — a temporary Getting
  Started token expires within 24 hours; use a System User token.

Conversation memory: the system prompt is rebuilt every turn and never stored
in the Redis checkpoint, and the model only sees the last
`CONVERSATION_MAX_MESSAGES` messages of a thread (default 40). The Gemini
free tier used in staging allows only 20 requests per day per model; the
production deployment uses a paid provider key configured through
`QWEN_SALES_MODEL` / `QWEN_MANAGER_MODEL`.

### Before a live test: readiness and provider checks

```bash
make check-env          # validates .env, Meta token/templates, ERPNext permissions, Redis limits
make check-env-offline  # same, without calling Meta or ERPNext
make verificar-qwen     # one minimal tool-calling request to each Qwen model (manual, never in CI)
```

`make check-env` (`app/readiness.py`) prints one OK / AVISO / FALTA / ERROR line
per item and never shows a value: only presence, lengths, counts, regions and
statuses. It checks the DashScope key and endpoint region, both model names,
the staff phones and country code, the delivery-zone mode, whether the WhatsApp
token is a permanent System User token with the right scopes, whether each
configured template is APPROVED in Meta (needs `WHATSAPP_BUSINESS_ACCOUNT_ID` or
a token that reveals the account), that the three ERPNext credentials are
distinct users where only the policy one can submit a Sales Order, that the
warehouse belongs to the company, the stock-trust window, and every owner limit
as stored in Redis. What it cannot verify it reports as unverified; it never
fills in a value. `deploy/verificar_qwen.py` refuses to run when `CI` is set and
sanitizes anything that looks like a key from its output.

### Install, test, and start

Dependencies are **pinned** in `requirements.txt` to the exact versions the
tests and the deployed agent run with (`langgraph>=0.2.60` used to resolve to
a different major every few months). To upgrade one, bump it, run `make test`,
then commit.

```bash
make install       # .venv with requirements-dev.txt
make test          # 260 tests + 3 xfails, ~1s, no credentials needed
make check         # what CI runs (ruff check + tests)
make check-env     # is .env complete, are the three ERPNext keys distinct
make up            # docker compose up, wait for :8081/health
make logs          # follow the agent
make decisiones    # what the policy and the notifications did, per order
make briefing      # send the morning briefing now
make seed          # demo catalog + customers into a staging ERPNext (see below)
```

Without Docker, from `plus-agent/` (this is what `start.sh` at the repo root
does for the Codespace):

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8081 --no-access-log
curl --fail http://127.0.0.1:8081/health
```

For a client launch, begin with `AUTO_CONFIRM_MAX=0` and
`STOCK_CONFIABLE=false`. Exercise the test WhatsApp number against staging
ERPNext first, and verify that one request produces one Sales Order number.
Only then point Meta's production callback at this server.

### Staging seed credentials

`deploy/seed_dairy.py` is a staging/demo bootstrap, not a production migration.
It creates catalog and customer records and therefore needs a temporary ERPNext
setup user with more permissions than any runtime identity. Inject that user's
`ERPNEXT_API_KEY` and `ERPNEXT_API_SECRET` only for the seed process
(`ERPNEXT_API_KEY=… ERPNEXT_API_SECRET=… make seed`), then revoke them; do not
store them in the service `.env`. The stock reconciliation remains a draft for
a person to review and submit, and reruns reuse an identical non-cancelled
reconciliation instead of creating duplicates.

## WhatsApp response and delivery contract

The HTTP `200` returned to Meta acknowledges the webhook and is invisible to
the customer. The visible acknowledgement and final result are two ordinary,
discrete WhatsApp messages:

1. *"Dame un momento mientras verifico…"* is sent immediately.
2. The final message says either **confirmed**, or **taken and pending review**,
   and includes the ERPNext Sales Order number. On failure it says that no
   order was created and escalates to a person; it must never invent a number.

Meta permits a free-form reply within 24 hours of the customer's last message.
Outside that customer-service window, an approved Message Template is required,
so a human confirmation delayed beyond 24 hours must use an approved order
status template. This is the current rule in Meta's
[WhatsApp Business Messaging Policy](https://whatsappbusiness.com/policy/).
Automation must also offer a clear human escalation path.

Streaming is neither needed nor exposed as a customer-visible WhatsApp Cloud
API capability. WhatsApp accepts complete message payloads through its
[message endpoint](https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages)
and reports lifecycle updates through webhooks; it does not render LLM tokens
as they are generated. The acknowledgement followed by one complete final
message is the appropriate UX. `sent`, `delivered`, and `read` status webhooks
are persisted by hashed outbound ID (`app/outbound_status.py`); terminal
failures add an ERPNext follow-up comment when the message is correlated to an
order. Only the ERPNext order number/status proves that the business request
itself was recorded.

The short-lived token generated in Meta's Getting Started dashboard is for
testing. Before production, replace `WHATSAPP_TOKEN` with a properly scoped
System User access token and establish a rotation/revocation procedure; Meta's
[Cloud API access-token guidance](https://developers.facebook.com/docs/whatsapp/business-management-api/get-started)
describes the supported token types. Never paste a live token into source,
logs, issues, or a pull request.

## Deploying client #2

Create a fresh secret configuration from `.env.example`, use that client's
separate Meta, ERPNext and Redis credentials, change `NOMBRE_NEGOCIO`, and
approve the templates in that client's WhatsApp Manager. Never copy one
client's live `.env` into another deployment.

## Running in a Codespace

`start.sh` is idempotent and is what `.devcontainer/devcontainer.json` runs on
every start: ERPNext stack, Redis Stack, public port 8081 for Meta, then the
agent under a small restart loop (`agente.pid`, log in `agente.log`). A
Codespace created before that file existed must be rebuilt once ("Rebuild
Container") for the automatic start to kick in; until then run
`bash start.sh` by hand after each resume. The public URL of the webhook
changes if the Codespace is recreated, so re-check Meta's callback URL.

## Not done yet

- Outbound confirmation when a human submits the order **in the ERPNext UI**
  (Frappe Webhook on `Sales Order` `on_submit` -> `POST /hooks/order-confirmed`).
  Approving from the WhatsApp button already notifies the customer.
- Media/audio messages (customers send voice notes constantly in Argentina)
- Business-hours handling
- `ruff format` is not enforced yet (CI runs `ruff check` only); the tree has
  a handful of lint findings to clean up in a formatting-only commit.
- Evals: a fixed set of ~30 real customer messages, run on every deploy
