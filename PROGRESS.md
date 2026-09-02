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
- any order asked for **later** — FIFO on **`(creation, order id)`**, passed as
  `desde=` and `excluir=`. Without the rule, two drafts for the last 8 units
  each defer to the other and the dairy sells to neither. Without the **order
  id** in the key the same deadlock returns whenever two drafts share a
  timestamp, which they can: one queue worker per inbound message, both writing
  to the same ERPNext. The id breaks the tie identically in both evaluations,
  so exactly one claims and it does not depend on which worker runs first. An
  order with no id, or an unreadable timestamp on either side, defers — it
  cannot prove it was first.

**Rejected drafts are asked to stop holding stock — NOT yet proven to.** The
code path is complete and the policy side is tested, but whether ERPNext keeps
`status = "Closed"` on a draft is version-dependent and **has not been verified
on our installation** (see ambiguity 1 below). Until it is, do not tell the
client that rejecting an order releases its stock; the audit comment on each
rejection is the honest record of what actually happened.
`decisiones.rechazar` marks the draft
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
1. **`status = "Closed"` on a draft is version-dependent — UNVERIFIED HERE.**
   Frappe's `StatusUpdater.set_status()` keeps it only while `status_map`'s
   Closed row is `eval:self.status=='Closed'`. If a build recomputes it back to
   Draft, the PUT still answers 200 — which is exactly why the saved value is
   compared and the failure surfaced instead of assumed away. **Nobody has run
   this against our ERPNext yet**, so the claim "a rejected order releases its
   stock" is not established. To settle it, on the live instance: reject a test
   order with the [Rechazar] button, then read the document back —

   ```bash
   curl -s -H "Authorization: token $ERPNEXT_POLICY_API_KEY:$ERPNEXT_POLICY_API_SECRET" \
     "$ERPNEXT_URL/api/resource/Sales%20Order/<NAME>?fields=\\[\"status\",\"docstatus\"\\]"
   ```

   `status: "Closed"`, `docstatus: 0` means it works on this version. Anything
   else means rejected drafts keep holding stock here, and the audit comment
   will already have said so (`sigue comprometiendo stock`).
2. **A submitted order whose status is Closed or On Hold reserves nothing in
   ERPNext either.** Nothing here can see those units: the child rows are read
   with `docstatus = 0`. It is ERPNext's own accounting choice and it predates
   this change; the guard in `aprobacion.confirmar_pedido` closes the route this
   app could have taken into that state.
3. **Listing a child doctype needs `parent=`** on Frappe v14+. Sent always;
   harmless where it is ignored.

## Stage 2a.1 — DONE: the customer answer deducts the same drafts

`app/tools/catalogo.py::consultar_stock` — the DISPONIBLE / POCO / SIN STOCK
level the Sales Agent answers with — read `Bin` alone, so it could say there is
stock for units another draft had already promised, and a customer hears that
as a promise. It now subtracts the same figure through
`policy.comprometido_en_borradores(item_code, warehouse)`, a public wrapper that
counts EVERY live claim: nobody has ordered anything yet, so there is no order
of our own to exclude and no queue position to respect. A failed lookup answers
"No pude verificar cuánto ya está comprometido. No confirmes disponibilidad" —
uncertainty reaches the customer as a refusal to promise, never as availability.

**Still open:** the `gerencia.py` and `captura.py` stock readouts (what the
OWNER sees) are `Bin`-only. That is arguably right — the owner wants the
physical count, not the net — but the briefing should show both, and that
belongs with 2e.

---

## Stage 2 — NEXT, in this order

## Stage 2b — DONE: the owner sets the limits, from WhatsApp

"Manager-configured" cannot mean an env var somebody redeploys. The six numbers
that decide whether an order confirms with nobody watching are now the owner's,
changed from the same WhatsApp thread he already uses, stored outside the
process, and applied from the next order without a restart.

| Setting | Meaning | Unit | Default | Stored | Missing / invalid / unreadable |
|---|---|---|---|---|---|
| `AUTO_CONFIRM_MAX` | biggest order that may confirm unseen | $ | `0` → off | Redis ← env ← code | `0` blocks; invalid or unreadable → **pending**, reason names it |
| `AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO` | most of one product per auto order | **stock UOM** | `0` → blocks | same | same |
| `STOCK_BUFFER_PCT` | margin held back for unloaded sales | % | `20` | same | must be `[0, 95]`; outside → stock rule pending |
| `AUTO_CONFIRM_MAX_CLIENTE_NUEVO` | ceiling for a customer below the history threshold | $ | `0` → they wait | same | same as the ceiling |
| `AUTO_CONFIRM_MAX_DEBT` | overdue balance tolerated | $ | `0` | same | same |
| `AUTO_CONFIRM_DESCUENTOS_APRUEBAN` | any discount goes to a person | sí/no | `true` | same | anything not clearly sí/no → **pending**, never read as `false` |
| `AUTO_CONFIRM_MAX_DESCUENTO_PCT` | most discount that may pass when the rule above is off — line **and** document combined | % | `5` | same | max accepted `50`; `0` = no discount confirms; unmeasurable → **pending** |

**Every default reproduces the behaviour before this stage.** With nothing
configured, 2b changes which orders auto-confirm: not at all. Only an explicit,
confirmed change from the owner loosens anything.

**Where they live** — `app/limites.py`, a Redis hash, resolved
`Redis → env → code default`. Redis is the same connection the submit lock uses
(`locks.conexion`), so a limits read and a lock failure fail closed together. A
Redis outage does **not** fall back to the environment: that would quietly undo
a limit the owner had tightened. It raises, and the order waits for a person.

**How he changes one** — `app/tools/configuracion.py`, management scope only,
re-checking `router.es_equipo` itself (`runtime_context.require_management`):
- `ver_limites` — all six, with the value and where it came from.
- `proponer_limite(limite, valor)` — validates and stores the change as
  **pending**. Nothing moves. Returns a four-digit code.
- `confirmar_limite(codigo)` — applies it, only for the phone that proposed it,
  only with that code, and only one setting at a time. Audited with phone,
  timestamp, old and new value; the pending change expires in 10 minutes.
- `historial_limites` — the last ten changes.

The LLM interprets what the owner wrote and relays the code. It cannot move a
limit on its own, and it still decides nothing about any order: `policy.evaluar`
reads the numbers and decides, in Python, including in the locked revalidation
(`test_the_locked_revalidation_uses_the_limits_in_force_at_that_moment` —
lowering the ceiling while the lock is held stops that order).

**Two rules were ambiguous, and the client chose:**
1. *New customer* = fewer than `AUTO_CONFIRM_MIN_ORDERS` **submitted** orders
   (not "has no Customer record" — anyone can be given one in a second). For
   them the new-customer ceiling **replaces** both history rules, since a
   customer with no history has no average to be compared against.
2. *Discounts off* relaxes **both** the document-level and the line-level
   check, up to `AUTO_CONFIRM_MAX_DESCUENTO_PCT` — corrected in 2b.1 below,
   where this was first built as "anything at or below list price".

Result: **369 passed**, lint clean. 35 mutations (18 from 2a, 17 from 2b) all
caught — including the dangerous ones: a Redis outage falling back to the
environment, an unchecked confirmation code, one manager confirming another's
change, and the configuration tools appearing in the customer tool list.

## Stage 2b.1 — DONE: three corrections to 2b

### 1. The discount rule was built wrong
2b implemented "any discount at or below the list price", which would have let
**90% off** auto-confirm: 2 is less than 20. The rule is now a manager-set
ceiling, `AUTO_CONFIRM_MAX_DESCUENTO_PCT`, default **5%**.

- Approval **on** (the default): every discount waits for a person, however
  small, and a generous cap cannot override that.
- Approval **off**: only up to the cap. It is measured on the **combined**
  line + document discount, because they stack — 5% off a line with 5% off the
  document is 9.75% off the list, not 5% — and on the **worst line**, because
  an average lets one heavily discounted product hide behind the rest of the
  order. A document `discount_amount` is measured against the sum of the line
  amounts; if that base is missing, or a line has no list price, the discount
  cannot be measured and the order waits (`_descuento_efectivo` raises).
- The cap itself is bounded at 50: more than half off is not a decision an
  unattended system should be making. Raise it in `limites.LIMITES` if the
  client disagrees — deliberately, not by typing a bigger number into WhatsApp.

Tested exactly at the cap (rate 19 against a list of 20), a hair over (18.98 →
5.10%), stacked as a percentage and as an amount, 90% off, an unmeasurable
discount, a cap of zero, and the cap being re-read in the locked revalidation.

### 2. New-customer auto-confirmation is off until 2d
`policy.CLIENTE_NUEVO_HABILITADO = False`. Promising an automatic delivery to
an address nobody has checked — or one outside the delivery area — is worse
than making the customer wait. So a customer below the history threshold always
stays a draft **and the manager is told** (the existing `notificar_equipo`
path, reason: "cliente sin historial suficiente: falta verificar dirección y
zona de entrega").

Deliberately **not** a knob the owner can turn on from WhatsApp and **not** an
environment variable: it is switched on in 2d, together with the verification
that justifies it. He can still set the ceiling — it is stored and audited —
and `ver_limites` tells him plainly that it decides nothing yet.

### 3. Persistence is proven, not assumed
A second connection proves two processes see the same value, not that the value
is on disk. `deploy/verificar_persistencia_limites.sh` proves it properly, on a
throwaway container with the production configuration, and it is re-runnable:

```
== 1. Redis con AOF + volumen, y un límite fijado por el código real ==
   valor|auditoría = 31337|1
== 2. docker restart (el proceso muere y vuelve) ==
   valor|auditoría = 31337|1
== 3. docker rm -f y docker run con el MISMO volumen ==
   valor|auditoría = 31337|1
   AOF en el volumen: yes
== 4. y si el volumen SÍ se pierde, no se vuelve a un default más flojo ==
   falla cerrada, como debe: los límites que configuró el dueño no están en el
   almacén, y ERPNext tiene cambios registrados: hay que restaurarlos antes de
   que algo se confirme solo
```

Step 3 is the one that matters: the container is destroyed, so a value that
comes back was on the volume and not in the container's filesystem.

**Step 4 is the requirement "a restart must never silently restore a looser
environment default".** Redis cannot answer "was I wiped?" — an empty store is
identical to a new install. So every applied change now also writes a durable
comment on the ERPNext **Company** document (`limites.MARCA_DURABLE`), and the
change is **not applied** if that record cannot be written. An empty store
*plus* changes on record means data loss, not a fresh install, and everything
stays pending until the limits are restored. That comment is also the owner's
audit trail in the system where his accounting lives.

**Infrastructure:** `docker-compose.yml` already had AOF and a named volume;
it now also pins `--appendfsync everysec` and says that the owner's limits live
there. `start.sh` created its container with **neither**, and now creates it
with both — and warns loudly if it finds an old one without a volume.
The Codespace's `agent-redis` was one of those: migrated with
`deploy/migrar_redis_a_volumen.sh` (SAVE, copy `/data` into the volume, recreate,
roll back if the new container does not answer). All 68 keys preserved, AOF on,
RedisJSON and RediSearch still loaded, and it survives a restart. The old
container is kept as `agent-redis-sin-volumen` until someone deletes it.

Result: **387 passed**, lint clean. 44 mutations (18 from 2a, 17 from 2b, 9
from 2b.1) all caught.

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
