# Progress — read this first

Working branch: **`feat/experiencia`** (based on `main`). Do **not** merge to `main`
or delete any branch yet.

Run everything from `plus-agent/`. Tests need no credentials, no Redis, no
ERPNext, no network:

```bash
cd plus-agent
.venv/bin/python -m pytest -q      # expect: 277 passed, 1 xfailed
.venv/bin/ruff check app tests     # expect: All checks passed!
```

The venv lives at `/workspaces/agent-crm/plus-agent/.venv`.

---

## The three roles (do not blur them)

| Role | Can | Cannot |
|---|---|---|
| **Customer Sales Agent** (LLM) | talk to customers, look up products/prices/stock, create **draft** orders | submit, pay, invoice, adjust stock |
| **AI Management Agent** (LLM) | monitor ERPNext, report, receive daily stock counts, alert the manager | decide anything, submit, override policy |
| **Human Manager** (person) | set automation limits, confirm/reject exceptions | — |

Two independent paths, and they must stay independent:

- **Automatic** — `app/policy.py` (`evaluar`) runs deterministic rules outside the
  LLM; `app/tools/pedidos.py::_after_create` re-reads the order, re-runs every
  rule under `policy.auto_submit_lock()`, then submits with the **policy**
  credential. A routine order confirms with **no human involved**. Enabled while
  `AUTO_CONFIRM_MAX > 0` and stock is trustworthy.
- **Manual (exceptions only)** — `app/decisiones.py`. Reachable only from
  `app/aprobacion.py::manejar_boton` after `router.es_equipo()` authenticates the
  manager on the signed webhook. **Never registered as an LLM tool** — enforced by
  `tests/test_frontera_decisiones.py`.

Credential boundaries (unchanged): agent identity = read + create draft;
manager identity = broad read; **policy identity = the only one that can
submit** (`erpnext.submit_doc`, `erpnext.policy_get_doc`).

---

## Stage 1 — DONE

1. **`app/decisiones.py`** (new) — the manual exception path.
   `confirmar(nombre, por)`, `rechazar(nombre, por, motivo="")`,
   `telefono_del_cliente(nombre_so)`. Each returns
   `{"ok": bool, "aviso_cliente": bool, "detalle": str}`.
2. **Rejecting an order now tells the customer.** It used to tell only the
   manager "no cambié su estado" while the customer — already told the order was
   received — waited indefinitely. Free text first, approved template
   (`WHATSAPP_CUSTOMER_REJECTED_TEMPLATE`) only if Meta closed the 24 h window.
   The draft is **left unconfirmed on purpose**: a generic ERPNext Sales Order has
   no durable "rejected" state, and deleting it would destroy the audit trail of
   something the customer was told about. The manager is told whether the customer
   was actually reached.
3. **The manager is really alerted.** `notificar.alertar_excepcion()` is the single
   funnel; `avisar_falla_tecnica` (a crash — the apology text claims the team was
   told, so now it is) and `avisar_escalamiento` (an ERPNext ToDo is invisible until
   someone opens the system) go through it.
   👉 **This is the hook for the future AI phone call** — add the voice channel
   inside `alertar_excepcion`, where `urgencia` (`URGENCIA_ALTA`) already arrives.
   Do not start it until the stock and limits work below is finished.
4. **Empty LLM reply can no longer become silence.** `main._non_empty()` substitutes
   a bilingual fallback; Meta 400s on an empty body, which previously made the item
   retry forever.
5. **Two strict xfails converted to passing tests**; `confirmar_pedido` was *moved*
   out of `manejar_boton` unchanged (it is already proven against duplicate taps and
   submit timeouts that commit after the client gives up).

Result: **277 passed, 1 xfailed**, lint clean.

---

## Stage 2 — NEXT, in this order

### 2a. Stock: subtract what other drafts already promised
Converts the last strict xfail:
`tests/test_policy_reglas.py::test_quantity_promised_in_other_draft_orders_is_subtracted_from_stock`
(it documents the gap — read its `reason=`). In ERPNext a **draft does not touch
`Bin.reserved_qty`**, so today two drafts can auto-confirm the same last units.
In `policy._hay_stock`, subtract qty from other **draft** `Sales Order Item` rows
(`docstatus = 0`), excluding the order being evaluated. A lookup failure must fail
**closed** (rule fails → human). Remove the xfail marker when it passes.
Reference implementation: `git show origin/claude/architecture-review-testing-j1xc5z:plus-agent/app/policy.py`.

### 2b. Manager-configured limits (all read per call, no restart)
Existing: `AUTO_CONFIRM_MAX`, `STOCK_BUFFER_PCT`, `AUTO_CONFIRM_MAX_DEBT`,
`AUTO_CONFIRM_MULT`, `AUTO_CONFIRM_MIN_ORDERS`.
Add as policy rules + tests + `.env.example` entries:
- `AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO` — max quantity of one product per auto order
- `AUTO_CONFIRM_MAX_CLIENTE_NUEVO` — order ceiling for a customer with no history
- `AUTO_CONFIRM_DESCUENTOS_APRUEBAN` (default `true`) — when true any document- or
  line-level discount always goes to the manager (today discounts are *always*
  rejected; this makes it configurable without weakening the default)
Every new rule must fail **closed** on unreadable data.

### 2c. Stock reliability becomes earned, not a static flag
Today `STOCK_CONFIABLE` is a static env var. Required: the manager sends today's
counts as **text** to the AI Management Agent → `contar_stock` creates a **draft**
Stock Reconciliation → the manager confirms it → only then is stock treated as
reliable for auto-confirmation. Suggested: record the timestamp of the last
**confirmed** reconciliation and treat stock as reliable for
`STOCK_CONFIABLE_HORAS` (new, default 24). `STOCK_CONFIABLE=false` must still force
"never promise stock". Offline sales already flow through
`registrar_venta_offline`. **Voice notes are out of scope — text only.**

### 2d. New customer can order in the same conversation (was Stage 2 of the old plan)
Today a stranger cannot order: `require_customer` fails closed with no customer
code, so the bot can only create a Lead and promise a callback. Required:
`crear_cliente(nombre, direccion, ...)` (Customer + Address, phone taken
server-side from `actor.actor_phone`, never from the model), `require_customer`
falling back to `clientes.buscar_por_telefono`, then `crear_pedido` in the **same
turn**. A new customer has no history so it can never auto-confirm — the manager
always sees the first order. Prompt rule: ask **one** question for name + address.

### 2e. Owner's two-messages-a-day (design agreed, not built)
`app/digest.py` at 18:00 ("N orders for tomorrow, reply OK to confirm all") —
scheduling is already committed in `docker-compose.yml` + `deploy/crontab`;
management tools `confirmar_pedidos` / `cancelar_pedido` / `reprogramar_pedido`
that call `decisiones.*` **after** `actor_context` proves management scope;
briefing line when stock has not been updated for N days; exception ping with a
`PING_TIMEOUT_MIN` (30) fallback so no customer waits on the manager's phone.

### 2f. Then, and only then
Wire new tools into `app/graph.py`, complete `.env.example`, live run against the
real ERPNext, push to `main`, and archive/delete the old branches
(`merge/best-of-both` and `fix/whatsapp-order-flow` are fully contained in `main`;
tag `claude/architecture-review-testing-j1xc5z` as `archive/claude-review` first —
it still holds the reference implementations for 2a and 2d).

---

## Blockers — need the client, not code

| Blocker | Effect |
|---|---|
| WhatsApp token expired (was a short-lived USER token) | no live send; needs a **System User** token with expiry *Never* |
| `TELEFONOS_EQUIPO` empty | nobody can approve; manager is routed to the customer bot; no alerts land |
| Templates not created | `WHATSAPP_STAFF_DIGEST_TEMPLATE`, `WHATSAPP_CUSTOMER_REJECTED_TEMPLATE`, `WHATSAPP_STAFF_ALERT_TEMPLATE` — needed only outside the 24 h window; free text is used inside it |
| Gemini free tier = 20 requests/day | exhausts during any real test session; billing removes the cap |

None of these block coding or the test suite.

## House rules

- Tests must stay offline and credential-free (`tests/conftest.py` supplies dummies).
- Never invent an order number, a stock figure or a confirmation — report only what
  ERPNext returned.
- Customer-facing text written outside a model turn must be bilingual (ES/EN);
  the model mirrors the customer's language itself.
- Do not rewrite working code to restyle it. One focused commit per stage, with the
  suite and lint green.
