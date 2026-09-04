# Progress — read this first

Working branch: **`feat/experiencia`** (based on `main`). Do **not** merge to `main`
or delete any branch yet.

Run everything from `plus-agent/`. Tests need no credentials, no ERPNext and no
network — but they DO need a Redis Stack on `REDIS_URL`, database 0 (see "Tests
and Redis" in `plus-agent/README.md`):

```bash
cd plus-agent
REDIS_URL=redis://localhost:6379/0 .venv/bin/python -m pytest -q   # expect: 1124 passed, 1 xfailed
.venv/bin/ruff check app tests     # expect: All checks passed!
```

## Known gap — fix before production

**What the owner is SHOWN can disagree with what the system will DO, after a
Redis loss.** One `xfail(strict=True)` records it:
`tests/test_limites.py::test_readiness_agrees_with_the_decision_path_after_a_wipe`.

If the delivery-rule store is wiped and ERPNext has `[entrega]` changes on
record, `limites.entrega()` is correct — it offers NOTHING, so nothing is
oversold and no delivery is pre-authorised. But `limites.resumen()` still
resolves those rows from the bootstrap environment and reports
`origen="arranque"`, and two surfaces read `resumen()`:

* `make check-env` (readiness), and
* `app/tools/configuracion.py::ver_reglas_de_entrega` — the tool the **owner**
  uses when he asks which days he delivers on.

So he is told he has a round the system will not offer, and is not told his
rules are gone, which is the one thing he needs to know in order to set them
again. Safe, because the decision path is `entrega()`; wrong, because it hides
a data loss from the person who has to repair it.

**The fix:** `resumen()` has to ask the same durable question `entrega()` asks
(`_hubo_cambios_durables_entrega()`) and report those rows as lost rather than
as bootstrap values. The xfail is strict on purpose — fixing it turns that test
into a FAILURE, so nobody can fix the code and leave this record behind.

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
  **pending**. Nothing moves. The four-digit code does NOT come back here:
  Python sends it to the owner's own number and the agent never sees it.
- there is **no** confirm tool. `app/main.py::_codigo_de_ajuste` applies the
  change when the owner writes those four digits — a signed webhook, a phone
  `es_equipo` authenticated, handled before any model reads the message. Only
  for the phone that proposed it, only with that code, one setting at a time.
  Audited with phone, timestamp, old and new value; pending expires in 10 min.
- `historial_limites` — the last ten changes.

The LLM interprets what the owner wrote and proposes. It never learns the code,
so it cannot take both halves of the confirmation. It cannot move a limit on
its own, and it still decides nothing about any order: `policy.evaluar`
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

## Stage 2c — DONE: trust in the inventory is earned, and it expires

`STOCK_CONFIABLE=true` was a promise written once in the `.env`. The system
kept it for ever — promising stock three weeks after the last count. In a dairy
the system figure drifts from reality in hours.

**The rule** (`app/inventario.py`):

```
un producto es confiable  ⇔  STOCK_CONFIABLE=true  (interruptor maestro)
                             Y alguien contó ESE producto en ESE depósito
                             Y confirmó el ajuste (docstatus 1)
                             hace menos de STOCK_CONFIABLE_HORAS (default 24)
```

**Per `(item_code, warehouse)` pair.** Both halves matter: counting the milk
says nothing about the cheese, and counting the milk in one warehouse says
nothing about the milk in another. A single global flag would have let one
count of one product vouch for every product in every warehouse. `ultimo_conteo` finds the most recent *confirmed* `Stock Reconciliation`
containing that item and warehouse — child rows first, `order_by="modified
desc"`, then the parents for their `posting_date`/`posting_time` in business
time (`erpnext.get_list` gained `order_by` for this).

**A draft count is not a count.** It is somebody's WhatsApp message with a
number in it. `contar_stock` now requires an authenticated manager, creates the
draft, and sends **one button** (`notificar.pedir_confirmacion_conteo` →
`conteo:<name>`). The tap goes to `aprobacion.manejar_boton` → `es_equipo` →
`decisiones.confirmar_conteo`, which submits with **the policy credential**.
Neither agent has Submit permission and `confirmar_conteo` is in no tool list
(asserted in `test_frontera_decisiones.py`). Tapping twice does not post the
adjustment twice; if the button cannot be sent, he is told to confirm in
ERPNext rather than told it is done.

**Fails closed, always.** No count, a draft-only count, a count of another item
or warehouse, an unreadable posting date, a count dated in the future, a
nonsense `STOCK_CONFIABLE_HORAS`, or an ERPNext that cannot be read → not
trustworthy. `confiable()` never raises: the customer agent calls it on the way
to answering somebody, so it degrades into "I am not promising", never into an
exception. Both callers use it: `policy.evaluar` (order pending, reason names
the age — *"el último conteo de LECHE-1L es de hace 40 h (vale 24 h)"*) and
`catalogo.consultar_stock`, which now does not even read `Bin` without a fresh
count.

`STOCK_CONFIABLE=false` still forces "never promise stock", counted or not.
Voice notes stayed out of scope: text only.

Result: **417 passed**, lint clean.

### The mutation harness is now part of the repo
`plus-agent/deploy/mutaciones.py` — it breaks each guard in the
auto-confirmation policy one at a time, runs the suite, and expects a failure.
A mutation nothing catches is a guard with no test.

```
python deploy/mutaciones.py          # all 54
python deploy/mutaciones.py stock    # just the ones about stock
```

All **54** are caught (2a stock reservations and FIFO, 2b limits and audit,
2b.1 discount cap and data loss, 2c earned trust). It restores every file in a
`finally`, so an interrupted run leaves nothing mutated. Use it before touching
`policy.py`, `limites.py` or `inventario.py`.

## Stage 2d — DONE: a stranger can order in one conversation, and the system decides where the truck goes

**The phone is never a parameter.** `crear_cliente(nombre, direccion)` takes
exactly those two things. The phone comes from the webhook Meta signed
(`actor_context(config).actor_phone`) and is normalised in `clientes.crear`. No
tool accepts one, so no message can register — or order for — somebody else
(`test_the_model_cannot_name_a_phone_number`, and the schema-boundary
parametrize in `test_autorizacion.py`).

**Nothing duplicates.** The alta runs under `distributed_lock("alta-cliente:
<phone>")` — the same mechanism that protects order creation — and looks the
phone up again *inside* the lock. Same phone twice → one Customer, `creado=
False`. Same address twice, or the same address spelled differently → one
Address (`clientes.misma_direccion` compares like a person reads). A changed
address → a second Address on the same Customer, which has to earn its own
approval. A `Customer` create that fails on a name collision (or a race) is
resolved by phone before it is reported as a failure. Orders were already
idempotent by `po_no = _message_key(message_id)`.

**The order says where it goes.** `crear_pedido` now resolves the account by
the verified phone when the webhook's config has none yet — the customer
registered a second ago in this same turn — and sets `customer_address` and
`shipping_address_name` from `clientes.direccion_principal`. Without an address
on the order, the policy cannot verify the delivery, and the order waits (good).
An ERPNext that cannot answer the lookup is not "this person has no account":
the tool returns text, because raising breaks that customer's thread for good.

**Where the truck goes is decided in Python** (`app/entrega.py`), in this
order, and the model cannot move it:

1. the address's postal code is in `ZONAS_ENTREGA_CP` → deliver;
2. no postal code, and the locality is in `ZONAS_ENTREGA_LOCALIDADES`
   (accents and case aside) → deliver;
3. there is a **submitted** Sales Order from the same customer to the same
   address → deliver. A person already approved that delivery and the truck
   arrived: stronger evidence than any configured zone, and what keeps
   long-standing customers with no postcode on file from suddenly waiting;
4. anything else — no address, unreadable address, no postcode and unknown
   locality, outside the zone, ERPNext not answering, no zones configured at
   all — → **draft**.

With a postcode present the postcode wins: a locality that "sounds right" does
not rescue a code 200 km away. The rule applies to **every** order, so a known
customer using a new address is reviewed like a stranger. Both lists are read
per call.

**The alert has what a person needs to decide.** Every delivery reason starts
with `entrega a revisar:` and carries the human-readable address and the cause
(*"entrega a revisar: Ruta 9 km 300, Villa Rara (CP X9999) — el código postal
X9999 no está en las zonas de reparto"*). It travels through the existing
`notificar_equipo(nombre, ..., motivos=...)`, so the manager sees the ERP order
id, the address and the reason in one message. He confirms or rejects with the
same `ok:`/`no:` buttons as every other exception — nothing new was added for
the override, on purpose.

**The customer hears "received, delivery under review" — never "confirmed".**
`_after_create` appends `ENTREGA EN REVISIÓN: … NO le digas que está confirmado`
to the tool result whenever a delivery reason is among the motives, and the
prompt repeats it. Asserted in
`test_the_customer_hears_received_and_under_review_never_confirmed`.

**`CLIENTE_NUEVO_HABILITADO = True`**, now that there is a verification behind
it. It is a separate rule from the address check and both must pass — and
stock, discount, per-product quantity, debt and the new-customer ceiling still
each decide on their own
(`test_every_other_limit_still_applies_to_a_verified_new_customer`).

**Needs the client:** the `Agente IA` role in ERPNext must gain Read + Create
on `Address` and Read on `Dynamic Link` (still no Submit on anything), and
`ZONAS_ENTREGA_CP` / `ZONAS_ENTREGA_LOCALIDADES` must be filled — with both
empty, nothing is delivered without a person, by design.

Files: `app/entrega.py` (new), `app/clientes.py`, `app/tools/pedidos.py`,
`app/policy.py`, `app/graph.py`, `app/prompts.py`, `app/tools/configuracion.py`,
`.env.example`, `tests/test_entrega.py` (new), `tests/test_alta_cliente.py`
(new), and the existing suites.

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
