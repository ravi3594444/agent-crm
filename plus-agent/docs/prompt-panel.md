# Session opener — the admin panel: conversations, customers, and what the agent is about to do

Fresh session in `plus-agent/` of `agent-crm`. Everything you need is in this message.

**First commit:** save this whole message as `docs/prompt-panel.md` on a branch off `main`, push it.

**You build the UI. Someone else builds the endpoints.** This document is the contract between
you. Build every screen against the shapes written here; they will be served by real endpoints on
the same branch or the next one. Where an endpoint does not exist yet, your screen must still work
in demo mode — see *Demo mode is not optional* below.

## Where things stand

Read `CLAUDE.md` at the repo root first. Its rules are not optional, particularly hard rule 2
(three ERPNext identities) and the three about tests. Then read `plus-agent/docs/MAPA.md`: it says
what each big file does and what its traps are, and the working agreement is that whoever changes a
module updates its row in the same commit.

The panel already exists and is **read-only by design**. `app/dashboard.py` (487 lines) serves
`/config`, `/snapshot`, `/controls`, `/operations` and `/orders/{id}`, all GET; anything else gets
`405 {"error": "This dashboard is read-only"}`. `app/dashboard_ui/` is the whole front end: one
`index.html` (20 lines), one `app.js` (532), one `styles.css`.

Merged and relevant: `#29` built the panel, `#38`/`#39`/`#42` built `app/agenda.py` (a durable queue
of future work), `#41` let a customer take back their own draft.

---

# What the owner asked for

A shopkeeper's day runs through WhatsApp, and the owner currently cannot see any of it. They asked
for four things, in their words: see the agent's chats; see which customers it talked to today and
what they wanted; see a customer's orders in detail; and quick controls — including confirming an
order the agent did not auto-confirm.

Three of those are reads and are yours to build now. The fourth is a write and is deliberately not
in this brief; see *What is not in this brief* at the end.

---

# The one constraint that decides the design

**A conversation cannot be traced back to a person.** `app/main.py:221-222`:

```python
def _thread_tag(telefono: str) -> str:
    return f"wa:{hashlib.sha256(telefono.encode()).hexdigest()}"
```

The LangGraph checkpoint key is `checkpoint:cli:wa:<sha256 of the phone>:…`. That hash is one-way.
There are ~33k checkpoint keys in a running Redis and **not one of them can tell you whose it is.**

So this does not exist and must not be built:

> a screen that lists all the agent's conversations

and this does:

> **open a customer → see that customer's conversation**

because the arrow runs the only direction that works:

```
Customer (ERPNext)  →  mobile_no  →  sha256  →  checkpoint:cli:wa:<hash>  →  the transcript
```

`decisiones.telefono_del_cliente` (`app/decisiones.py:72-84`) already walks the first half of that.

Treat this as a feature, not an obstacle. Every transcript is reached through a customer the owner
already has the right to see, under the company scoping the panel already enforces on orders — so
the privacy property comes from the shape of the data rather than from a check somebody has to
remember to write. Say so in the UI where it is relevant; it is a thing worth selling.

**What a transcript does NOT contain:** the system prompt. `app/conversacion.py:1-11` builds it per
turn and never stores it, for an unrelated reason (Gemini concatenating one per turn), and the
happy side effect is that a transcript cannot leak the agent's instructions. You can show the
conversation without showing how the agent works.

---

# The screens

## 1. Conversation, inside the customer

`showCustomer(id)` already opens a detail dialog with the customer's orders (`app.js:231-236`).
The conversation goes **in that dialog**, under the orders — not in a new top-level nav entry.
A new nav item would promise a browsable inbox, which is the thing that cannot exist.

```
GET /api/dashboard/customers/{customerId}/conversation
```
```jsonc
{
  "customerId": "CUST-0007",
  "customerName": "Panadería López",
  "reachable": true,          // false when the Customer has no mobile_no on file
  "messages": [               // oldest first, newest last
    {"role": "customer", "text": "hola, tenés leche entera?", "at": "2026-09-13T09:12:04Z"},
    {"role": "agent",    "text": "Sí. Sachet de 1 L, $1.250…",  "at": "2026-09-13T09:12:11Z"},
    {"role": "note",     "text": "The agent looked up the catalogue.", "at": "…"}
  ],
  "truncated": false,         // true when older turns were dropped
  "retentionDays": 30
}
```

- `role` is exactly one of `customer`, `agent`, `note`. **`note` is how a tool call is rendered** —
  a neutral sentence, never the tool name or its arguments. The owner needs to know the agent did
  something between two messages; they do not need `catalogo.buscar_producto(codigo=…)`.
- `reachable: false` is a real state with its own empty view: *"No WhatsApp number on file for this
  customer, so there is no conversation to show."* It is not an error.
- Never render a phone number. The panel does not return one and you should not ask for one.
- `retentionDays` is the truth behind an honest line: conversations older than this are gone,
  because `CONVERSATION_TTL_DAYS` expires them. Do not imply a permanent archive.

## 2. Today

A new nav entry, `['today', 'Today']`, **first in the list, before Overview.** It is the screen the
owner opens in the morning and the reason they keep the tab open.

```
GET /api/dashboard/today
```
```jsonc
{
  "date": "2026-09-13",
  "conversations": [
    {"customerId": "CUST-0007", "customerName": "Panadería López",
     "turns": 6, "lastAt": "2026-09-13T09:31:00Z",
     "lastLine": "…dejámelo para mañana entonces",
     "orderId": "SAL-ORD-2026-00042"}      // null when they talked but did not order
  ],
  "newCustomers": [...],                    // same shape, first order today
  "truncated": []
}
```

The row that matters most is the one with **`orderId: null`** — someone the agent talked to who did
not order. That is a lost sale the owner has never once been able to see. Make it visible rather
than uniform with the rest.

## 3. What the agent is about to do

This is not on the owner's list and is the highest-value thing in this brief.

After `#38`, `#41` and `#42` the agent keeps a **durable queue of future work** — it will message a
customer before their delivery, re-ping the owner before a deadline, close a draft nobody decided,
follow up. Today none of that is visible anywhere. The owner cannot answer *"what is this thing
going to say to my customers tonight?"*, which is exactly the question a person asks before they
trust an agent with their customers.

Add it to Overview as a card, and give it a nav entry `['queue', 'Coming up']`.

```
GET /api/dashboard/queue
```
```jsonc
{
  "upcoming": [
    {"id": "…", "type": "delivery_notice", "orderId": "SAL-ORD-2026-00042",
     "customer": "Panadería López", "dueAt": "2026-09-13T17:00:00Z",
     "what": "Remind the customer their order arrives at 17:00"}
  ],
  "waitingOnAPerson": [
    {"orderId": "…", "customer": "…", "since": "…", "what": "Price above the auto-confirm limit"}
  ],
  "undelivered": {"replies": 0, "notices": 0}
}
```

`what` is a finished English sentence built server-side. Do not build phrasing from `type` in the
UI — new row types get added and a client-side switch goes stale silently.

`waitingOnAPerson` is the queue the owner is the bottleneck for. If it is non-empty, say so on
Overview with a count, not a badge colour alone.

## 4. Refinements worth doing while you are in here

- **`/operations` is buried.** Redis state, queue depth and failed deliveries live at the bottom of
  the *AI agents* screen. "2 replies never reached a customer" belongs on Overview.
- **Order detail says to go elsewhere.** `renderOrder` (`app.js:223`) tells the owner to use "the
  authorized manager's WhatsApp thread". True today. Leave the sentence until the write path lands
  — do not soften it in advance.
- **Empty states are inconsistent.** `empty()` exists and is good; some paths render bare `<p>`.
- **The 250 cap is invisible.** `truncated` is returned and mostly ignored. If a list is cut, say so
  in the list, not only in settings.

---

# How this codebase does UI

Read `app/dashboard_ui/app.js` end to end before writing anything; it is 532 dense lines and it is
consistent. It is **vanilla JS, no framework, no build step.** Do not introduce one.

A new screen is three edits:

1. `nav` (line 76) — `['today', 'Today']`
2. a render function returning an HTML string
3. the `views` map (line 212) — `{overview, orders, …, today}`

Then it routes for free: `goto(view)` sets `state.view`, writes `#view` to the URL and calls
`render()`, which replaces `$('#app').innerHTML` with `shell()`. `hashchange` is wired (line 353).

The patterns to copy, not reinvent:

| need | use |
|---|---|
| any interpolated value | `escape(v)` — **every one**, no exceptions |
| a modal | `detail(title, htmlString)` |
| an empty state | `empty(title, note)` |
| money | `money(n)` / `moneyFor(n, currency)` — honours the user's display currency |
| a date | `prettyDate(date)` |
| a number that may be null | `number(n)` → `—` |
| an icon | `icon(name)` — add a path to `paths` (line 3) if you need a new one |
| a transient message | `toast(message)` |
| a click | delegate on `document` (line 305) with a `data-…` attribute; do not attach per-node handlers |

Four traps, each of which has a real failure behind it:

- **`validateSnapshot` (line 243) rejects any array longer than 250 and throws for the whole
  response.** A new endpoint needs its own validator in the same style. A 200-message transcript
  routed through the snapshot validator would blank the entire dashboard rather than truncate one
  list.
- **Demo mode is not optional.** `?demo=1` and `disconnectedData()` are what a prospective client is
  shown before they connect. A screen with no demo data is a broken screen in a sales demo. Add
  fixtures to `makeDemo()` (line 53) for every screen you build.
- **Lazy loads are per-view and explicit.** `goto()` calls `loadExtras()` only for `agents`. Follow
  that shape — `snapshot` is the only thing fetched on the refresh timer, and adding a heavy fetch
  to it makes every screen pay for one screen.
- **`cerrarSesion()` (line 264) is the single reset.** A 401 on refresh must clear `data`. If you
  add state, clear it there, or a signed-out panel keeps showing CRM data.

On the API side, `quien()` (`app/dashboard.py:175`) has **three** answers — `None`, `ANONIMO` (`""`,
which is falsy and is a *valid* token), and a phone number. Any guard written `if not mirando`
instead of `is None` has a hole. You are unlikely to touch this, but if you do, that is the trap.

---

# Verification

- `ruff check app demo tests deploy` — clean.
- `pytest -q` in all five cells (default, `IDIOMA_DEFAULT=en`, `IDIOMA_GERENCIA=en`,
  `BUSINESS_TIMEZONE=Asia/Kolkata`, `LOCALE=en_US`). **Read the collected count, not the exit
  code**: without a Redis Stack on `REDIS_URL`, nine modules fail at collection and pytest aborts
  having run zero tests.
- `node --test scripts/test-dashboard.mjs` if it exists — check before assuming.
- Every new screen renders in demo mode with no connection.
- Update `docs/MAPA.md` in the same commit as the module you change.

Open a PR when green and tell the owner. **Do not merge**, and let Qodo's review land before calling
it ready — it has found something real on every PR in this repo, including two security issues.

---

# Confirming an order — the one write that exists

This was held back from the original brief and has now landed. It is the **only** endpoint on the
panel that is not a GET; everything else still answers `405 {"error": "This dashboard is read-only"}`.

```
POST /api/dashboard/orders/{orderId}/confirm
```
**Send no request body.** The endpoint reads nothing but the path and the `Authorization` header —
the order id is in the URL — so `fetch(url, {method: 'POST', headers: {Authorization: 'Bearer ' + token}})`
is the whole call. Sending `Content-Type: application/json` is allowed too (it is on
`access-control-allow-headers`, along with `Authorization`), but there is nothing to put in the body.

That block below is the **response**:

```jsonc
{
  "orderId": "SAL-ORD-2026-00042",   // canonical — may differ in case from what you sent
  "ok": true,                        // the action the manager asked for succeeded
  "submitted": true,                 // the order is now a CONFIRMED Sales Order (docstatus 1)
  "customerNotified": true,          // a message to the customer was queued (see below)
  "detail": "✅ SAL-ORD-2026-00042 confirmado. …"
}
```

- **`ok` is not "it was confirmed" — read `submitted` for that.** They differ in a case that happens
  in normal business: when the customer has an **open counter-offer**, confirming does not submit
  anything. It approves the customer's request and sends them the new terms, which succeeds, so
  `ok` is `true` and `submitted` is `false`. The order is still a draft holding stock and the
  customer can still refuse it. **Only flip the row to Confirmed on `submitted: true`**; on
  `ok: true, submitted: false` show `detail` and leave the order where it is — it is now waiting on
  the customer, not on you.
- **`customerNotified` means "a message to the customer was queued", and which message depends on
  the branch**: with `submitted: true` it is their confirmation; with `submitted: false` it is the
  counter-offer awaiting their reply. Do not label it "confirmation sent" without checking
  `submitted`.
- **`orderId` comes back canonical.** ERPNext resolves order names case-insensitively, so the id you
  send may differ in case from the real one; the response echoes the document's own name. Use the
  returned value, not the one you sent.

- **`detail` is the agent's own sentence, in the business language, not UI copy.** Show it verbatim
  as a quoted result line. It is exactly what the manager would have been told over WhatsApp, and
  the reason is deliberate: two channels that explain the same outcome in different words end up
  disagreeing. On a refusal it is the only thing that says what to do next — *"está Closed — se
  rechazó antes y ya no reserva stock"* is a different problem from *"no pude comprobar la
  confirmación"*.
- **`ok: false` with HTTP 200 is a real answer, not an error.** The order exists, the caller was
  allowed, and the agent declined — a draft that was already rejected, a state that cannot be
  confirmed. `submitted` is `false` here too. Render `detail`; do not retry. So the three outcomes
  you must render differently are `ok:false` (declined), `ok:true, submitted:false` (the customer
  now has a counter-offer to answer) and `ok:true, submitted:true` (confirmed).
- **403** — the token can read but cannot confirm. Two separate causes, same status: the **shared**
  `DASHBOARD_API_TOKEN` (authenticated but nobody in particular — a decision without a name cannot
  be audited), or a per-person token whose phone is not on `TELEFONOS_EQUIPO`. Do not show a
  sign-in prompt: the session is fine, the permission is not.
- **404** — the order is not in this workspace. Same company check as the reads, and it runs
  **before** anything is decided.
- **405** on `GET .../confirm`. The exception is the *pair* (route, method), not the method.

What the button must say, because the panel is now a place where money moves: confirming is a
**Submit**, and a submit does not come back. The customer is told. Ask once, plainly, and name the
order in the question.

The write does **not** merge the three ERPNext identities. The Submit is still `erpnext.submit_doc`
with the policy credential, which no model tool can reach; a dashboard endpoint is not a tool in
that sense — a **person**, authenticated, invokes the same deterministic Python that
`aprobacion.manejar_boton` invokes when that same person taps a WhatsApp button. All of it goes
through `decisiones.confirmar`, which is now the single door: it checks `router.es_equipo`, takes
its own lock, routes an order with an open counter-offer to the approval path instead of submitting
it at the old price, and closes the human review.

---

# What is not in this brief, and why

**"Today we're closed."** A write, asked for, and still held back.

"Today we're closed" does not exist as a concept anywhere in the codebase, and inventing it is four
decisions (does the agent stop answering, or answer differently? does the sweep pause? what happens
to a delivery already promised for today? who can turn it off?) — a product question, not a UI one.

Build the reads. They are most of the value and none of the risk.
