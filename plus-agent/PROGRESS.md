
## Stage 2e — DONE on `feat/experiencia-2e`: the manager loop closes

Built on `feat/experiencia` @ eaa4a13 plus the WhatsApp runtime hardening
cherry-picked from `fix/whatsapp-order-flow` (retry classification, dead-letter,
24-hour-window fallbacks, deterministic manager commands, bounded conversation
history, ERPNext v16 payload fixes) and the Qwen migration (`app/modelos.py`).

1. **Confirmed-order notice, exactly once** — `notificar.notificar_confirmacion`
   is called from both confirmation paths (`pedidos._after_create` after the
   locked submit; `aprobacion.confirmar_pedido` after a human tap or command).
   A Redis claim keyed by the Sales Order id makes the second path silent; the
   claim is released only when nobody could be reached, so a retry can still
   notify. Content: order id, customer, items with qty and UOM, total with
   currency, delivery address (from the Address document) and date, source and
   business-time timestamp.
2. **Order-id commands, all deterministic** — `ver`, `confirmar`, `rechazar`,
   `preparar`, `despachar` (`main._STAFF_ACTIONS` → `aprobacion.manejar_boton`
   → `decisiones.*`). Staff phones only; nothing here is an LLM tool.
3. **Dispatch in two human steps** — `decisiones.preparar` creates a DRAFT
   Delivery Note with the policy identity (`erpnext.policy_create_doc`, docstatus
   forced to 0; idempotent per order); `decisiones.despachar` submits exactly one
   prepared draft with `erpnext.submit_doc`. A draft, Closed or Cancelled order
   cannot be prepared; unknown or distant addresses stay drafts and can be
   rejected by id.
4. **18:00 digest** — `app/digest.py`, no model: confirmed orders waiting for
   dispatch, drafts waiting for the manager, stock counts expired or about to
   expire (per product, from `inventario.ultimo_conteo`), failed notifications
   and dead-letter counts. In-process scheduler in `main._lifespan`
   (`DIGEST_HORA`, `DIGEST_ACTIVO`) and `python -m app.digest`; one marker per
   business day in Redis.
5. **Notifications never vanish** — `outbound_status.registrar_aviso_fallido`
   parks any alert nobody received in `wa:{inbound}:dead-notify` (hashed
   recipient, no phone) and opens one ERPNext ToDo per purpose and order per
   day. Customers are told "the team was alerted" only when a ToDo or a WhatsApp
   alert actually exists (`main._alert_technical_failure`).

Review of Stage 2d (read-only, before integrating): all eleven checks hold,
with two notes. (a) Existing customers' NEW addresses were checked against the
wrong address — `direccion_principal` picked the alphabetically first Address,
i.e. the old one — fixed in its own commit (`clientes.direccion_para_pedido`).
(b) Decision for the owner: a postcode inside the zone authorises delivery even
when the stated locality is not in the configured list; the reverse (postcode
outside, locality inside) already fails closed. Making both-present-and-
conflicting fail closed is a policy choice, not a defect, so it was left as is.

## Release candidate RC1 = `084e2c7c` on `feat/experiencia-2e`, plus the release gate

The gate commit (this one) tightens two things and adds the readiness tooling:

- **Delivery rule** (`app/entrega.py`, `evaluar_zona`): with both lists
  configured, postcode AND locality must be present and allowed; with one list,
  that list decides; with none, nothing auto-delivers; a contradiction, a
  missing required value or an unreadable value leaves the order pending. The
  "delivered there before" override is gone: the lists are the rule, and the
  owner extends them. `tests/test_entrega.py` covers every combination of rule
  set × address values.
- **Qwen**: sales stays `qwen3.7-plus-2026-05-26`; the management default is the
  documented `qwen3.8-max`, with `QWEN_MANAGER_MODEL` (and the older
  `LLM_MODEL_*` names) still overriding it — select `qwen3.8-max-0902` after
  `make verificar-modelos` proves it on your endpoint. That script makes one
  tool-calling request per model, refuses to run in CI and never prints the key.
- **Readiness** (`app/readiness.py`, `make check-env`): validates and reports,
  without exposing values, the DashScope key/endpoint/region/models, staff
  phones and country code, zone mode, permanent WhatsApp token and scopes,
  template approval, ERPNext identities and Submit permissions, warehouse,
  stock-trust window and every owner limit. Nothing is fabricated.

Live testing was deliberately NOT run: `.env` still needs the owner's values
(DashScope key, staff phones, zones, templates, three distinct ERPNext users).

## Pilot workflow clarifications (after the release gate)

- Templates are an optional fallback: `make check-env` reports a missing
  template as AVISO; every customer-facing message uses the customer's own
  24-hour window first. The owner keeps their window open by writing to the bot.
- The confirmed-order alert is informational ("no hace falta responder"); if the
  manager does nothing the order stays confirmed. A customer who read
  "confirmado" in the conversation (`outbound_status.marcar_confirmacion(...,
  informado_en_chat=True)` from the automatic path) never receives a second
  confirmation, template or not.
- New human-only command `cancelar <pedido> <motivo>` (`decisiones.cancelar`):
  staff phones only (re-checked), reason required, submitted orders only and
  only within `CANCELACION_HORAS` of a confirmation THIS system recorded, refused
  when any Delivery Note or Sales Invoice (draft or submitted) is linked, no
  cascade, everything re-read under `distributed_lock`, policy identity only
  (`erpnext.policy_cancel_doc`), audited, idempotent, customer told once (free
  text in window / optional template / dead-letter + one ToDo). Not an LLM tool.
  `tests/test_cancelacion.py` has a test per rule.

## Phase A — the three release blockers (before the decision workflow)

Three commits on `feat/decision-workflow`, based on `ccda363`:

1. **The cancellation deadline is durable.** It lived in one Redis string with a
   seven-day TTL, so a flush or a restart silently closed a window the business
   still had. `app/confirmacion.py` writes an append-only ERPNext comment
   (`[confirmado-por-agente] <UTC>`) on both confirmation paths and reads it
   back; Redis is only a cache of it, written after the durable write succeeds.
   The earliest record wins. No durable record means no WhatsApp cancellation,
   which is the safe direction.
2. **The customer's confirmation no longer depends on the model.** It used to be
   the `PEDIDO_CONFIRMADO` token that the prompt asked the model to relay, while
   the "already informed" marker was set whether it did or not. `app/avisos.py`
   is a durable notice queue: the text is built from the document, the entry and
   its (event, order) idempotency key are written in one Lua script, a claimed
   entry is leased rather than removed, transient failures retry with bounded
   backoff, and giving up dead-letters the notice and opens one ERPNext ToDo.
3. **A prepared order can still be cancelled.** `cancelar` used to refuse for any
   linked document, draft included, so one `preparar` stranded the order for
   good. Now a submitted Delivery Note or Sales Invoice blocks it, a draft
   invoice goes to ERPNext, and a draft Delivery Note is undone by the new
   human-only `despreparar <pedido>` — which deletes only a draft the agent
   created and nobody edited, audits it on the order first, and never cascades.

708 tests pass, ruff clean. No model, key or provider change; no live test.

## Phase B — the Sales-to-Management decision workflow

New modules: `app/excepciones.py` (the owner's pre-authorized exceptions) and
`app/solicitudes.py` (the DecisionRequest: durable, event-sourced on ERPNext
comments, with its own expiry sweep). One new customer tool,
`pedir_excepcion_de_entrega`, which can only ask.

- A pre-authorized case is offered from configuration, with no person involved
  and no model inventing terms; everything else opens a request and the
  customer is answered in the same turn.
- Manager actions: `aprobar`, `contraoferta <fecha> <hora> <cargo>`,
  `retiro <fecha> <hora>`, `rechazar <motivo>`, `ver` — only TELEFONOS_EQUIPO,
  exact commands only, prose summarized and sent back for confirmation.
- The customer's `acepto` / `no acepto` is matched deterministically before any
  model sees it; acceptance re-validates stock, quantities, prices, discount,
  delivery and order state under the lock and only then submits.
- The approval timeout is a manager-configurable limit
  (`APROBACION_TIMEOUT_HORAS`), and it is also how long a pending draft may
  hold stock. `app/policy.py` drops a lapsed hold from the draft-stock
  calculation; the sweep closes the draft in ERPNext and only claims the stock
  was freed when a re-read proves it.
- A delivery fee is written only when `ENTREGA_CARGO_CUENTA` is configured;
  otherwise the order waits for a person rather than carrying an invented total.

791 tests pass, ruff clean. No model, key or provider change; no live test.

## Phase B follow-up — an expiry is an answer, not a dead end

The timeout released the hold correctly but ended the conversation with "write
to me again". The customer already wrote; the waiting was our side's failure.
So the sweep now answers them with something concrete.

- The original request dies for good: `vencida` is terminal, its hold is
  released, and no later manager decision reopens it (`texto_superada_equipo`
  says so by name instead of "already decided").
- A **second, separate** request is opened with a new id, a new expiry and its
  own event trail, carrying an offer computed from the owner's configuration
  (`excepciones.evaluar_respaldo`): the next normal delivery day
  (`ENTREGA_DIAS` / `ENTREGA_HORA`, address still inside the zones), otherwise
  a pickup at the shop (`RETIRO_LOCAL_*`), which needs no route and no zone.
  Both carry no fee, so acceptance can never stall on a missing charge
  account, and today is excluded — that request sat unanswered for hours.
- The fallback expiry is capped at the moment it promises, so an offer for
  Tuesday 08:00 is never still acceptable on Tuesday at 09:00.
- It is only an offer: `acepto <pedido>` is still required, and acceptance
  re-opens the draft its predecessor closed and then re-validates stock, draft
  commitments, quantities, total, price, discount, delivery details and order
  state under the lock. Nothing was held while the customer thought about it,
  which is exactly what the revalidation is for.
- A fallback never gets a fallback of its own; a repeated timeout event writes
  nothing twice; requests stay isolated by Sales Order id; and when nothing can
  be computed nothing is offered — the customer hears the plain truth and the
  manager is told what was missing.

824 tests pass, ruff clean. No model, key or provider change; no live test.

## Phase B follow-up 2 — the human review gets a deadline

The failure path had a permanent stock commitment in it. Accepting a fallback
offer re-opens the draft its predecessor closed, then revalidates; when
revalidation failed, `_a_revision` wrote the terminal `revision_humana` and
returned, leaving a LIVE draft with nothing that could ever release it.

The reason it was invisible: "counts against stock" and "waiting for somebody"
are not the same predicate. ERPNext counts every live draft by DEFAULT, and
`app/policy.py` only ever subtracts the holds `solicitudes.vencimientos()`
reports. A state that reports no expiry does not stop holding units — it holds
them for ever.

- New `CON_PLAZO`, deliberately wider than `ABIERTOS`: `revision_humana` now
  carries a live deadline (the index, `vencimientos()`, the sweep) while
  staying out of the decision paths. That is load-bearing — `confirmar
  <pedido>` is the documented way out of a review and works only because the
  state is not "open"; adding it to `ABIERTOS` silently re-routes that word
  from "submit this draft" to "approve the exception again". There is now a
  test that fails if anyone does.
- The deadline is the owner's `REVISION_TIMEOUT_HORAS` (default 24 h, max 168),
  resolved Redis → env → default like every other limit, and `_a_revision`
  writes a FRESH one — it used to inherit the reviewed offer's, which is
  usually minutes away and often already past.
- The sweep dispatches on state: a review is never routed into `_respaldar`,
  so a customer who already accepted is not sent a third machine-picked date.
  It re-reads the order first, so a manager who confirmed or cancelled out of
  band is recognised (`revision_resuelta`) instead of overwritten.
- Two new terminals, because the two endings are different facts:
  `revision_resuelta` (somebody dealt with it) and `revision_vencida` (nobody
  did, the draft was closed, the customer was told).
- `confirmar` / `rechazar` close the review through `resolver_revision`, which
  is a no-op unless the request is actually in review — so duplicates,
  concurrent expiry and late commands are all idempotent.
- A review that ERPNext will not record releases the hold at once: no durable
  record means no deadline, and that is the bug.
- `_avisar_equipo` now takes an explicit event key. It derived one from
  `solicitud.evento`, so the escalation notice collided with the notice already
  sent for that event and was dropped as a duplicate.

23 new tests, including the three-layer proof that a failed fallback acceptance
cannot retain stock (live draft -> deadline reported to policy -> draft closed
by the sweep). 847 pass, ruff clean. No model, key or provider change.

## Phase B follow-up 3 — the delivery rules become the owner's

They were `os.getenv` reads, so the one thing the owner most needs to change
was the one thing he could not: the days he delivers on. Now they resolve like
every other setting — Redis, then the bootstrap environment, then a safe
default — and he sets them from WhatsApp through the SAME two-step confirmation
code and the same append-only audit (Redis plus the durable ERPNext comment,
written first).

- Ten settings: normal round days/time; off-day delivery enablement, days,
  time, fee and order minimum; pickup enablement, days and time.
- Their own registry (`limites.ENTREGA`), not `LIMITES`. `configuracion()`
  validates every entry and raises on the first bad one, and `app/policy.py`
  calls it once per order LINE and again inside the submit lock — so a typo in
  "martes" would have become an outage for every customer. `limites.entrega()`
  reads these on their own and fails SOFT: unreadable means "not
  pre-authorized", which is where app/excepciones.py already fails.
- Read per operation. `app/excepciones.py` no longer touches os.getenv; its
  weekday and time parsing moved into the validators, so one vocabulary
  validates on the way in and reads on the way out.
- Deterministic validators for four kinds: weekday lists in any spelling or
  order ("Miércoles, Sábado" -> "miercoles,sabado"), times ("8", "9:30",
  "18.00", "7 hs" -> "HH:MM"), sí/no, and money. A `-` sentinel makes "borrá
  los días" expressible: an empty string in the store reads as "unset" and
  would silently restore the .env value.
- `definicion()` now refuses an AMBIGUOUS name instead of returning the first
  dict match. "hora" matches six settings; picking one would let a vague word
  from the model move a setting the owner never mentioned.
- The account head (`ENTREGA_CARGO_CUENTA`) is in no registry and stays a
  server setting: a wrong account does not break the bot, it unbalances the
  books, and no model can verify one exists.
- `readiness.chequear_entrega` reports every rule from the owner's store and
  raises an AVISO when neither a round nor a pickup is configured — the expiry
  fallback is then off and an expired request drops the order.

Two money defects fixed on the way, both pre-existing and both about the
owner's own numbers: `validar()` stored at 6 significant digits, so a 1234567
ceiling became "1.23457e+06" and read back as 1234570; and `_numero` read
"1.500" as 1.5 while `solicitudes.parsear_terminos` reads the same keystrokes
as 1500.

48 new tests: authorization, the confirmation code, the audit trail in both
places, resolution order, restart, read-per-operation, malformed values,
ambiguity, the account-head refusal, one-setting-at-a-time, a dead store, and
the readiness AVISO. 905 pass, ruff clean. No model, key or provider change.

## Phase C — the owner writes in criollo, and it reaches ONE existing action

The last dead end on the owner's side. Prose about an order («rechazame el de
la panadería, no llegamos con el reparto») reached the management agent, which
had no tool able to move an order — correct, and a dead end: the answer was the
exact commands, for him to type from a phone with the order number spelled
right.

New: `app/acciones.py` (the whitelist, the durable proposal, the code and the
revalidation) and `app/tools/gestion.py` (`detalle_de_pedido`, `proponer_accion`).
No new capability: the whitelist IS `main._STAFF_ACTIONS` ∪ `main._ARG_ACTIONS`,
asserted as a set equality, and each action produces the payload the typed
command produces, byte for byte, for the same `aprobacion.manejar_boton`.

- **Read-only runs now.** `ver` is `detalle_de_pedido`, after
  `require_management`. The payload is built inside `app/acciones.py`, so no
  argument exists that turns the read path into a write.
- **Writes are only prepared.** The proposal carries the action, the order id,
  the parameters and the consequence read off the real document — customer,
  total, and what will actually happen, including that an `ok` on an order with
  an open request approves the *exception*, not the order.
- **Six digits, one use, sent by Python to his own number.** The model never
  sees it, so it cannot take both steps. Six and not four because the settings
  code is four: with one of each pending, four digits would be ambiguous and
  one of the two answers cancels an order. No arbitration, no guessing.
- **`main._codigo_de_accion` applies it**, before any model reads the message,
  re-checking in Python: still on the staff list, still whitelisted, the order
  id still parses, the parameters re-parse through the same validators, the
  code matches, not expired, bound to that phone. Then `manejar_boton`, under
  `distributed_lock("accion:<pedido>")`.
- **Incomplete means zero writes.** No action, no order, a malformed order, a
  `cancelar`/`rechazar` with no reason, terms that do not parse, a fee on a
  pickup, an unreadable order, `contraoferta`/`retiro` with no open request, and
  an approval of terms the customer never gave: nothing is stored, nothing is
  read from ERPNext when the request is already incomplete, and it says what is
  missing.
- **The five known ways a two-step confirmation breaks**, each with its test:
  replay (GETDEL — a code works once), a stale code (the expiry is inside the
  proposal, not only a Redis TTL), duplicate delivery (the same action asked
  twice returns the same pending proposal and sends no second code), restart
  (nothing in the process; the module is reloaded in the test and the proposal
  is still there), and two pending at once (one live proposal per phone; a new
  one replaces the old and the old code dies with it).
- **The existing safety net stays in front.** With an OPEN decision request on
  the order, `main._resumen_de_solicitud` still answers before any model reads
  the message — the summary and the exact commands — so `contraoferta` and
  `retiro` on such an order keep needing the typed command when the message
  names it in the uppercase form `_ORDER_IN_TEXT` recognises. Pinned by a test
  so nobody removes it by accident. The prose path covers everything else.
- **The authorization is durable.** `[accion] … autorizado por … con código de
  un solo uso` is written on the Sales Order BEFORE the handler runs, and a
  comment that cannot be written means the action is not executed — the same
  rule `app/limites.py` uses, for the same reason.
- **A customer's words stay data.** The quote stripper moved to
  `app/formato.py::sin_citas` so the prose path and the typed command share ONE
  copy of that rule; `app/main.py::_sin_citas` is now that function.
- Prompt: a new section telling the model to prepare and never to claim
  something is done, to ask for what is missing instead of inventing an order
  number or a date, and that quoted customer text is data.
- Bench: scenario `gerencia_accion_en_prosa` (prose → proposal → code →
  rejected, with the middle step asserting the order is still a draft and that
  nothing said "rechazado"). `demo/falso_modelo.py` now resolves the order id
  from what the owner wrote when there is no tool result to read it from — the
  management thread starts empty — and `demo/piloto.py` reads both code lengths.

1714 pass, ruff clean. No model, provider, key or live-configuration change.

**Not verified here:** Docker was unavailable in this container (a recovery
image with no `dockerd` and no `CAP_SYS_ADMIN`), so `make demo`, `docker build`
and the container no-egress check did NOT run. The suite was run instead with a
socket-level egress blocker: 1714 pass with every non-loopback connection
refused, and zero attempts. That also closed a pre-existing leak —
`tests/test_autorizacion_gerencia.py` reached graph.facebook.com through
`proponer_limite`; both propose tools now have the sender mocked.

---

## Adaptive response UX — DONE: one reply per message, a progress notice only when a tool is really running

**The fault, seen live.** Every text got *"Recibido, dame un momento mientras lo
verifico"* before anybody had read it — from the webhook's background task AND
again from the worker (`_acknowledge_once`, deduplicated between the two with a
16 s wait loop). A `hi` produced two messages; a four-digit code, answered by
Python in a second, got the same fake preamble; and when Gemini failed with a
rate limit the person had been told "I'm checking" about nothing. The durable
checkpoints prove the `hi` turns never touched a tool or ERPNext: they were one
Gemini call each (16.6 s, 15.2 s, 54 s with the client's own 429 retries, and one
that failed after 29.5 s), with under a second of queue delay.

**The fix is the turn lifecycle, not a keyword.** `app/progreso.py` is a
LangChain `BaseCallbackHandler` passed in the run config from
`graph.responder_*` — the documented observation hook, no monkey-patching. The
MODEL decides whether it needs a tool; the handler only reacts: on the first
real `on_tool_start` of the turn it arms ONE timer, and if the turn is still
open after `WHATSAPP_PROGRESS_DELAY_SECONDS` (default 3) the person gets
*"Estoy consultando el sistema, dame un momento."* in their language. A fast
tool, several tools (serial or parallel), a direct answer of any length, a
code, a button or a typed command never produce it. `_generate_response` calls
`terminar()` before returning — it cancels the timer and takes the same lock an
in-flight notice holds — so a notice after the final is impossible by
construction. The notice is claimed once per inbound in Redis
(`wa:{inbound}:progress:*`), recorded as `agent_progress` and never as the
inbound's accepted final; any failure to send it is logged and ignored.

**Latency is reported, not hidden.** One log line per turn:
`[agent] turno … camino=modelo modelo=1x16.6s herramientas=0x0.0s progreso=no total=16.7s cola=0.9s`
(the enqueue timestamp now travels in the queue item). A 20 s direct answer is
one message and one honest log line.

**Also:** the team's failure alert is now in the team's language and quotes the
person's words as data (`notificar.texto_falla_tecnica`, `solicitudes.citar`),
labelled "De:/From:" since the sender may be the manager themself.

Tests: `tests/test_progreso.py` drives the real path — signed webhook → durable
queue → worker → `_generate_response` → the real `graph.responder_*` on a
compiled agent with a SCRIPTED model and the real ToolNode/callback plumbing —
for the direct answer (every ERPNext/policy function wired to raise), fast and
slow tools, both race orderings, serial and parallel tools, tool and model
failures, duplicate deliveries, both languages for manager and customer
independently, and the model-free paths. 1954 pass, none skipped, ruff clean,
no non-loopback socket. No model, provider, key, prompt or business-rule change.
