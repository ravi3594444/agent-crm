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
   The draft is **never deleted and never cancelled**: deleting it would destroy
   the audit trail of something the customer was told about, and ERPNext cannot
   cancel a document that was never submitted. Stage 2a added the missing half —
   it is now marked `status = "Closed"` so it stops holding stock. The manager is
   told whether the customer was actually reached.
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

## Stage 2a — DONE: two drafts can no longer sell the same last units

The gap: in ERPNext a **draft does not touch `Bin.reserved_qty`** — that only
rises on submit. Reading `Bin` alone, two drafts minutes apart both saw the same
last units as free and both auto-confirmed. One of those customers was going to
find out on delivery day.

**The formula**, in `policy._hay_stock`, all in the item's **stock** unit
(`_cantidad_en_stock_uom`, because `Bin` is):

```
disponible = Σ(Bin.actual_qty − Bin.reserved_qty)       submitted orders, once
           − Σ(stock qty promised in other live orders) the new virtual reservation
confirma solo  ⇔  round(disponible, 6) × (1 − STOCK_BUFFER_PCT) ≥ qty
```

**Which orders hold stock** — `_borradores_que_reservan` asks ERPNext for the
ORDERS first (`docstatus = 0`, same `company`, `status not in ("Closed",
"Cancelled", "On Hold")`), then `_comprometido_en_borradores` reads the item rows
for those orders in batches of 50. Both reads use the **policy identity**
(`erpnext.policy_get_list`): the customer agent must not be able to enumerate
other customers' orders. Order matters — asking for rows first and discarding
most of them afterwards let rejected drafts (kept for ever on purpose) fill the
truncation cap, which would have switched auto-confirmation off permanently for
a busy product. Batching matters too: a few hundred order names in one `in`
filter exceed gunicorn's default request-line limit and come back as a 414.

That status list is not a guess: ERPNext's own `get_reserved_qty`
(`erpnext/stock/stock_balance.py`) sums `where docstatus = 1 and status not in
('On Hold', 'Closed')`. `policy.ESTADOS_SIN_RESERVA` is the same set.

**Two exclusions matter as much as the subtraction:**
- the order being evaluated (`excluir=`) — its quantity is the one being checked;
- any order asked for **later** (`desde=` its `creation`) — without it, two
  drafts for the last 8 units each defer to the other and the dairy sells to
  neither. First to ask keeps the claim.

**Rejected drafts stopped holding stock.** `decisiones.rechazar` marks the draft
`Closed` via `erpnext.policy_update_status` — one Select field, policy identity,
never a submit, and **only ever on a draft** (a `no:` tap can arrive after a
`ok:` tap, and stamping Closed on a submitted order would release the stock
ERPNext reserved and drop it out of the delivery queue). Best effort and audited
either way: the comment says `ya no compromete stock` or `sigue comprometiendo
stock`. A Frappe PUT is a *save*, so the doctype can recompute the field and
still answer 200 — the saved value is compared, not trusted.
And `aprobacion.confirmar_pedido` now refuses to submit an order in one of those
states, because ERPNext keeps the status across a submit and then counts no
reservation for it at all.

**Fails closed** (raises → `evaluar` records "no se pudo verificar stock de X" →
draft → existing exception flow, and the real cause is logged as
`[policy] stock no verificable`): lookup error or timeout, more live drafts than
`MAX_BORRADORES`, more rows than `MAX_RENGLONES_POR_PEDIDO` per order, a negative
quantity, a quantity not convertible to the stock unit — and, new in the client,
an ERPNext answer with no list in it (`{"data": null}`, `{}`) which used to
coalesce to `[]` and read as "nothing is promised".

Everything stays inside the existing lock: `_after_create` still re-reads and
re-runs `policy.evaluar` under `policy.auto_submit_lock()`, and the draft lookup
now happens in there with it
(`test_competing_drafts_are_re_read_inside_the_submit_lock`).

Files: `app/policy.py`, `app/erpnext.py` (`_list` + `policy_get_list` +
`policy_update_status`; `get_list` gained `parent=` because Frappe refuses to
list a child doctype without naming its parent), `app/decisiones.py`,
`app/aprobacion.py`, `README.md`, and the three test files.

Result: **315 passed, 0 xfailed** (was 277 + 1 strict xfail), lint clean. Every
guard was mutation-tested: removing any one of them fails at least one test.

### Known ERPNext ambiguity, worth knowing before touching this
1. **`status = "Closed"` on a draft is version-dependent.** Frappe's
   `StatusUpdater.set_status()` keeps it only while `status_map`'s Closed row is
   `eval:self.status=='Closed'`. If a build recomputes it back to Draft, the PUT
   still answers 200 — which is exactly why the saved value is checked and the
   failure is surfaced instead of assumed away.
2. **A submitted order whose status is Closed or On Hold reserves nothing in
   ERPNext either.** Nothing here can see those units: the child rows are read
   with `docstatus = 0`. It is ERPNext's own accounting choice and it predates
   this change; the guard in `aprobacion.confirmar_pedido` closes the route this
   app could have taken into that state.
3. **Listing a child doctype needs `parent=`** on Frappe v14+. Sent always;
   harmless where it is ignored.

**Left open on purpose:** `app/tools/catalogo.py::consultar_stock` — the
DISPONIBLE / POCO / SIN STOCK answer the customer agent gives — still reads `Bin`
alone, so it can say DISPONIBLE for units another draft has promised. It is
orientative by design and cannot confirm anything, but it should get the same
deduction. Same for the `gerencia.py` / `captura.py` readouts.

---

## Stage 2 — NEXT, in this order

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
