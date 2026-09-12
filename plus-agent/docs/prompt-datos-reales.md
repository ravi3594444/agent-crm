# Session opener — the client's real data loads in one command

Fresh session in `plus-agent/` of `agent-crm`. Everything you need is in this message.

**First commit:** save this whole message as `docs/prompt-datos-reales.md` on a branch off `main` (currently `dcd1ffb`), and push it.

## Why this exists

The product cannot go autonomous on fake data. Two things block it today, and both are setup, not features:

* **Opening stock has never loaded.** `deploy/seed_dairy.py`'s stock step fails with HTTP `417` because the ERPNext company has no Default Inventory Account and no Stock Adjustment Account set. Until that is fixed, `STOCK_CONFIABLE` can never honestly be `true`.
* **Every price is a placeholder.** The seed script says so itself. An agent quoting placeholder prices autonomously would be autonomously wrong.

The owner is about to receive a real product list from the client. Right now that would be hours of clicking in the ERPNext UI. It should be one command.

## What to build

Two new files under `deploy/`. **Do not touch `deploy/seed_dairy.py`** — another session owns it this week. Ideally touch nothing under `app/` at all.

### 1. `deploy/cuentas_inventario.py` — kill the 417

Sets the company's Default Inventory Account and Stock Adjustment Account, creating them under the right parent if they do not exist. Idempotent: running it twice is a no-op and it says so. It prints what it found, what it changed, and what it left alone — never a silent success.

Finish by **actually proving it**: run the stock step that used to return `417` and show it returning something else. A script that "should" fix the 417 is not the deliverable.

### 2. `deploy/cargar_catalogo.py` — the real catalogue, from a CSV

Reads a CSV the owner fills in from whatever the client sends, and loads it into ERPNext: Items, Item Prices, and opening stock. Columns, in this order, with a `--ejemplo` flag that writes a template CSV with two filled rows so the owner can see the shape:

```
codigo, nombre, unidad, precio, stock_inicial, grupo
```

Requirements, in priority order:

* **Dry run is the default.** Without `--aplicar` it reads the CSV, validates every row against what is already in ERPNext, and prints exactly what it would create, update and skip. The owner sees the whole plan before anything is written.
* **Validate before writing anything.** A malformed price, a duplicate code, a missing unit, a negative stock — collect them all and refuse the whole run with a numbered list. Never half-load a catalogue.
* **Idempotent.** Re-running with the same CSV changes nothing. Re-running with three new rows adds three items. The owner will run this more than once and must not fear it.
* **`--verificar`** compares ERPNext against the CSV and reports drift in both directions: in the file but not in ERPNext, in ERPNext but not in the file, and prices that differ.
* **Prices go in the same price list and currency the existing seed uses** — read it from the code rather than assuming.

## Credentials, and the rule this must not break

Like `seed_dairy.py`, these are **deploy scripts run by a human with Administrator credentials**, passed per-process:

```
docker compose exec -e ERPNEXT_API_KEY=... -e ERPNEXT_API_SECRET=... agente python /srv/deploy/cargar_catalogo.py --ejemplo
```

They are **not reachable from any tool**, they do not import from `app/tools/`, and they do not touch `ERPNEXT_API_KEY`, `ERPNEXT_MANAGER_API_KEY` or `ERPNEXT_POLICY_API_KEY` as used at runtime. The three-identity separation in `CLAUDE.md` is the product's main selling point — nothing here goes near it. Say in each script's docstring that it is a human-run deploy script, so nobody later "helpfully" wires it to a tool.

## Constraints

* No changes to `app/prompts.py`, the identity split, or any confirm/submit path in `app/`.
* Every message these scripts print is read by the owner: plain, specific, and it names the file and row when something is wrong. English and Spanish both fine here — these are operator tools, not customer messages, so they do not need `idioma.t`.
* Read `CLAUDE.md` at the repo root first if it is there; if it is not yet committed, the rules that matter are: never edit files on the server, never commit `.env`, and a test is not done until one targeted mutation kills it and only it.
* Tests: the validator rejects each bad-row case; a dry run writes nothing; a second identical run is a no-op. Keep it proportionate — these are deploy scripts, not the ordering path.

## Done when

* `ruff check app demo tests deploy`; `pytest -q`; green in all four CI cells.
* `--ejemplo` writes a CSV a non-programmer can fill in.
* A dry run on that example CSV prints a complete, readable plan and writes nothing.
* **The check that counts:** on the live ERPNext, `cuentas_inventario.py` runs and the stock step that returned `417` no longer does. Paste the before and after in the PR.
