# Setting up ERPNext for the agent

Everything ERPNext needs before the agent can take an order end to end, in the
order it has to happen. `docs/PRENDERLO.md` covers the app side — `.env`, the
price list name, MCP, cron. **This file covers the ERPNext side**, which is
where the setup time actually goes.

Every item below was found the hard way, in production, on 16–17/09. Each one
fails **silently or misleadingly**: the agent says "I couldn't create that
order, so I've passed it to the manager", and the reason is four layers down.
That is the whole reason this file exists.

---

## The checklist

Work top to bottom. Each step depends on the ones above it.

| # | What | Script | What breaks without it |
|---|---|---|---|
| 1 | Three users, three roles | manual, once | the identity separation is the product |
| 2 | Read permissions for those roles | `deploy/permisos_erpnext.py` | `403` on order creation **and** on confirmation |
| 3 | Company inventory accounts | `deploy/cuentas_inventario.py` | `417` on every stock count |
| 4 | One submitted opening stock entry | manual, once per system | `417` again, with a different cause |
| 5 | Items with a complete Item Price | `deploy/seed_dairy.py` or yours | agent quotes nothing, auto-confirms nothing, **no error anywhere** |
| 6 | Customers with a delivery address | manual or import | every order is held for review |
| 7 | Verify | `python -m app.readiness` | — |

---

## 1. Three users, three roles

The product's main selling point is that three separate ERPNext identities do
three different things, enforced by ERPNext permissions rather than by prompt:

| Identity | `.env` keys | Can |
|---|---|---|
| customer agent | `ERPNEXT_API_KEY` / `_SECRET` | read + create drafts. **No Submit.** |
| management agent | `ERPNEXT_MANAGER_API_KEY` / `_SECRET` | broader reads + drafts. **No Submit.** |
| policy | `ERPNEXT_POLICY_API_KEY` / `_SECRET` | the **only** Submit. Not reachable from any tool. |

Create one ERPNext User per identity, give each its own role, and generate API
keys per user. On the reference install these are `agente-ia@`, `gerencia-ia@`
and `politica-ia@`, with roles `Agente IA`, `Gerencia IA` and `Politica IA` —
but nothing in the code hardcodes those names.

`app/readiness.py` refuses to pass if any two key pairs are identical. Do not
"simplify" this by sharing one key.

---

## 2. Read permissions

**This is the step that cost a full day.** ERPNext reads the `Account` doctype
while *validating* a Sales Order — the income account, the tax accounts — so an
identity that is allowed to create the document still cannot save it. Three
separate `403`s, each only visible when a real customer ordered something:

```
[erpnext] la creación de Sales Order: rechazado 403 — User agente-ia@… does not
  have doctype access via role permission for document Account
[erpnext] el reporte Accounts Receivable: rechazado 403 — You don't have access
  to Report: Accounts Receivable
[erpnext] la confirmación de Sales Order: rechazado 403 — User politica-ia@…
  does not have doctype access via role permission for document Account
```

Run, from `plus-agent/` on the host:

```
ERPNEXT_URL=https://your-erpnext ERPNEXT_ADMIN_API_KEY=... ERPNEXT_ADMIN_API_SECRET=... \
  .venv/bin/python deploy/permisos_erpnext.py
```

It prints what each identity is, what roles it has, and what's missing. Add
`--aplicar` to grant, then `--probar` to verify by **reading with each
identity's own keys** — not by inspecting the permission table as
Administrator, which would only prove Administrator can read.

It grants read only. Nothing in it writes, creates, deletes or submits.

> `ERPNEXT_URL` must be passed on the command line. The `.env` says
> `http://frontend:8080`, which only resolves *inside* the docker network; from
> the host every script dies with `Temporary failure in name resolution`.
> `app/__init__.py` calls `load_dotenv(override=False)`, so what you pass wins.

> The admin credential uses `ERPNEXT_ADMIN_API_KEY`, deliberately **not**
> `ERPNEXT_API_KEY` — that name is the customer agent's, and reusing it would
> make `--probar` measure Administrator while claiming to measure the agent.

**After granting, clear the cache** or nothing changes and it looks identical to
not having granted at all:

```
docker compose --project-name erpnext -f ~/gitops/erpnext.yml exec backend \
  bench --site <your-site> clear-cache
```

---

## 3. Company inventory accounts

Without a *Default Inventory Account* and a *Stock Adjustment Account* on the
company, every Stock Reconciliation is rejected with `417`.

```
ERPNEXT_URL=https://your-erpnext ERPNEXT_API_KEY=<admin> ERPNEXT_API_SECRET=<admin> \
  .venv/bin/python deploy/cuentas_inventario.py --aplicar
```

It finds the accounts by `account_type`, not by name, because a chart of
accounts is in whatever language it was installed in.

---

## 4. One submitted opening stock entry

**The `417` does not go away after step 3, and the second cause is not
discoverable from the error.** The real message is:

```
OpeningEntryAccountError: Difference Account must be a Asset/Liability type
account, since this Stock Reconciliation is an Opening Entry
```

ERPNext treats *any* Stock Reconciliation as an opening entry while **not one
Stock Ledger Entry exists in the whole system**, and then refuses a P&L
difference account — which is exactly what the everyday stock-adjustment account
is. Chicken and egg: nothing can be counted because nothing was ever counted.

Break it by posting **one** opening entry by hand, and **submit it** — a draft
creates no ledger entry, and the ledger entry is the entire point:

- `purpose = "Opening Stock"`
- `expense_account` = an **Equity** account (create one if your chart has no
  `Temporary Opening`; the reference install used `Apertura Temporaria - LP`)
- a real `valuation_rate`

Every count after that works from WhatsApp with the company default, no code
change. Two consequences worth knowing:

- `deploy/cuentas_inventario.py --probar` can **never** pass on a system with
  zero ledger entries — its probe posts the exact document ERPNext refuses.
- An item that has never had stock still needs a `valuation_rate`, and
  `contar_stock` does not send one. So the first-ever count of each product
  needs this treatment, not just the first product.

---

## 5. Items and their prices

**A price without `price_list`, `currency` *and* `uom` can never auto-confirm.**
`policy._precio_autorizado` filters Item Prices on all three and skips any that
misses one — so a catalogue loaded without them produces an agent that quotes
nothing and confirms nothing, **with no error anywhere**. This has surfaced
three times, every time found sideways while fixing something else.

Check any product with:

```
docker compose --project-name erpnext -f ~/gitops/erpnext.yml exec backend \
  bench --site <your-site> execute frappe.client.get_list \
  --kwargs '{"doctype":"Item Price","filters":{"item_code":"QUE-CRE"},"fields":["price_list","currency","uom","price_list_rate"]}'
```

A good row looks like:

```json
{"price_list": "Standard Selling", "currency": "ARS", "uom": "Kg", "price_list_rate": 9800.0}
```

`price_list` must equal `AUTO_CONFIRM_PRICE_LIST` in `.env` **exactly**. Also:
the order line's unit must equal the item's `stock_uom` and the conversion
factor must be 1.0, or `_precio_estandar` rejects it.

If auto-confirm silently does nothing after loading real data, **check this
first.**

---

## 6. Customers and addresses

Customers are matched by phone number. The agent resolves the sender from the
WhatsApp webhook — `crear_cliente` takes no phone argument by design, so no
message can register or act as somebody else.

Each customer needs an **Address** linked to them, with the fields your delivery
zones are configured against:

- `ZONAS_ENTREGA_LOCALIDADES` set → the address needs a `city`
- `ZONAS_ENTREGA_CODIGOS_POSTALES` set → the address needs a `pincode`
- **both set → the address needs both**, and either one missing rejects the
  order

That last rule bites: configuring postcodes "for completeness" on a dataset
whose addresses have `pincode: null` rejects every order, with the reason buried
in a held-for-review card.

> Known gap: the customer agent has **no tool to read or set a delivery
> address**. It uses whatever is on the record and never asks. Addresses must be
> correct in ERPNext before go-live.

---

## 7. Verify

```
docker compose exec agente python -m app.readiness
```

Live preflight of `.env`, Meta, ERPNext, the warehouse, the limits and the
dashboard, without printing any value. It checks the three identities really are
three, and reports a permission it could not *read* as a warning rather than as
a pass.

Then the real test, in this order:

1. From a **staff** number: `contar stock <ITEM>` → answer with a number that
   **differs** from the current figure (ERPNext rejects a reconciliation with no
   change) → **tap the confirm button** (`ultimo_conteo` only counts
   `docstatus = 1`).
2. From a **customer** number: order that product.

A confirmed order says `Source: automatic (policy)`. If it says
`manual (human confirmation)`, a policy gate held it and the card names which.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `403 … for document Account` | step 2 not done, or cache not cleared | `permisos_erpnext.py --aplicar`, then `clear-cache` |
| `403 … access to Report: Accounts Receivable` | same, policy identity | same |
| `403 … permission to get a report on: Sales Invoice` | report needs `report` on its `ref_doctype` too | `add_permission` read, then `update_permission_property` report |
| `417` on a stock count | step 3 or step 4 | see those steps — they are two different causes |
| Agent quotes no price, auto-confirms nothing, no error | step 5 | check `price_list` + `currency` + `uom` |
| Every order held: "no se pudo verificar la deuda vencida" | policy can't read the report | step 2 |
| Every order held: delivery | address missing `city`/`pincode` | step 6 |
| Order held on the amount | `AUTO_CONFIRM_MAX` | see below |

### The ceiling is not in `.env`

`limites._resolver` reads **the owner's saved store first**, then `.env`, then
the code default:

```python
fijado = almacen.get(nombre, "").strip()
if fijado:
    return fijado, ORIGEN_DUENO          # owner's store wins
del_entorno = os.getenv(nombre, "").strip()
if del_entorno:
    return del_entorno, ORIGEN_ARRANQUE  # .env only if the store is empty
```

So editing `.env` does **nothing** for any setting the owner has ever changed by
WhatsApp. That is deliberate — a container restart must not loosen a ceiling the
owner tightened. To see what is actually pinned:

```
docker compose exec redis redis-cli hgetall plus-agent:limites
```

Anything listed there is changed only through the WhatsApp two-step: send the
change from a staff number, then reply with the 4-digit code. English works —
send `change AUTO_CONFIRM_MAX to 0`, since `definicion()` matches the technical
name exactly. Plain-English aliases like "order ceiling" do not resolve, and
"max" is ambiguous across four settings.

**`AUTO_CONFIRM_MAX=0` and `STOCK_CONFIABLE=false` are the launch posture.** If
you raised them to test, put them back.
