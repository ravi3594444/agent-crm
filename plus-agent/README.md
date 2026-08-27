# plus-agent

WhatsApp order agent for the Plus dairy stack. Runs as one container on the
same server as ERPNext, talking to it over the internal Docker network.

## The point of this repo

LangGraph is one line in `requirements.txt`. Everything else here is yours,
and it is roughly 350 lines. That is the entire "we still have to write code"
problem, measured.

| File | Lines | What it is |
|---|---|---|
| `app/erpnext.py` | ~75 | REST client. The agent never touches MariaDB. |
| `app/tools/catalogo.py` | ~55 | Read tools — products, stock, order status |
| `app/tools/pedidos.py` | ~85 | Write tools — **drafts only** |
| `app/prompts.py` | ~30 | Rioplatense Spanish system prompt |
| `app/graph.py` | ~50 | The agent itself |
| `app/main.py` | ~90 | Webhook: signature, idempotency, customer lookup |
| `app/whatsapp.py` | ~25 | Outbound messages |
| `app/tools/gerencia.py` | ~135 | Management read tools (owner assistant) |
| `app/prompts_gerencia.py` | ~30 | Management prompt |
| `app/router.py` | ~20 | Phone-based agent routing (security boundary) |
| `app/briefing.py` | ~30 | 07:00 WhatsApp morning briefing |
| `app/tools/captura.py` | ~180 | **Offline-sale capture** — the hard part |
| `app/policy.py` | ~130 | **Auto-confirm engine** — deterministic, LLM-proof |
| `app/notificar.py` | ~50 | WhatsApp approval buttons |
| `app/aprobacion.py` | ~65 | Button taps -> ERPNext submit |

## Two agents, one webhook

Route by phone number in `router.py`:

- **staff phone** -> management agent: broad read across sales, stock,
  receivables, customers. Stronger model. Still cannot submit.
- **anyone else** -> customer agent: 6 narrow tools, draft-only writes.

This is a **security boundary**, not a convenience. A customer-facing bot with
full system read is one prompt injection away from leaking the customer list,
margins and supplier prices. Separate agents, separate ERPNext users,
separate roles.

## Removing the wait

Nobody waits for milk. Three mechanisms, in order of impact:

**1. Auto-confirm by exception (`app/policy.py`)**
Most orders are boring — known customer, usual products, list price, in stock.
Those confirm INSTANTLY. Only unusual ones wake a human. Every rule must pass:

| Rule | Default |
|---|---|
| Order total under ceiling | `AUTO_CONFIRM_MAX` |
| Not wildly above customer's own average | 2x |
| Customer has real order history | 3+ confirmed orders |
| No overdue balance | 0 |
| Stock verified above safety buffer | — |
| List price, no negotiated rate | — |

**The safety property:** `policy.py` is deterministic Python. It never sees the
customer's words. The agent has no submit tool and no way to call it. Prompt
injection cannot widen the envelope — it can only produce a draft that then
fails the rules. That is how you get instant confirmation without handing an
LLM the keys.

Start `AUTO_CONFIRM_MAX=0` (everything reviewed). Raise it week by week as he
watches the decisions land. By month two most orders never touch him.

**2. One-tap approval (`app/notificar.py`, `app/aprobacion.py`)**
Orders that DO need him arrive as a WhatsApp with buttons —
[Confirmar] [Rechazar] [Ver detalle]. He taps from his lock screen, ERPNext
submits, the customer is notified automatically. Two seconds, no app.

**3. "Lo de siempre" (`pedido_habitual`)**
Dairy customers reorder the same thing forever. One message, one tap, done.

**Plus: never leave silence.** Even a held order gets an instant reply with a
real order number — *"pedido SO-0042 tomado, te confirmo en unos minutos."*
The wait people hate is the silence, not the delay.

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
sales not yet loaded, and ERPNext's `reserved_qty` stopping two customers
being sold the same milk.

**Daily rhythm that makes it work**

| When | Who | What |
|---|---|---|
| 07:00 | system | Morning briefing to owner |
| 07:15 | whoever opens | Counts key products -> `contar_stock` |
| all day | counter/truck | Reports sales as they happen |
| all day | owner | Confirms drafts from his phone |
| 18:00 | owner | Clears remaining drafts |

If the 07:15 count does not happen, do not flip `STOCK_CONFIABLE` to true.

## The four rules baked in

1. **No SQL.** Everything goes through `erpnext.py` over REST.
2. **Draft only.** `create_doc` forces `docstatus: 0`. The agent's ERPNext
   Role has no submit permission, so this is enforced by ERPNext — not by
   the prompt. Prompt injection cannot escalate it.
3. **Idempotency.** Meta retries webhooks. `wa:seen:{message_id}` in Redis
   stops one retry becoming two orders.
4. **Audit.** Every AI write leaves a Comment on the record.

## Setup

1. In ERPNext: create Role "Agente IA" — grant Create+Read on Item, Bin,
   Customer, Lead, Sales Order, ToDo. **Do not grant Submit.** Create user
   `agente@…`, assign the role, generate API key + secret.
2. `cp .env.example .env` and fill it in.
3. Add to the stack's `docker-compose.yml` on the same network as `backend`.
4. Point the Meta webhook at `https://…/webhook/whatsapp`.

## Deploying client #2

Copy `.env`, change `NOMBRE_NEGOCIO`, point at that client's ERPNext,
edit the prompt. No new build.

## Not done yet

- Outbound confirmation when a human submits the order
  (Frappe Webhook on `Sales Order` `on_submit` -> `POST /hooks/order-confirmed`)
- Media/audio messages (customers send voice notes constantly in Argentina)
- Business-hours handling
- Wire `briefing.py` to cron at 07:00 America/Argentina/Buenos_Aires
- Evals: a fixed set of ~30 real customer messages, run on every deploy
