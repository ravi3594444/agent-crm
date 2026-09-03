
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
  `make verificar-qwen` proves it on your endpoint. That script makes one
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
