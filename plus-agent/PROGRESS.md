
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
