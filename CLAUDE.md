# agent-crm — project context

Save this as `CLAUDE.md` in the **repo root** and commit it. Claude Code and most coding agents read it automatically at session start, so you never re-explain any of this.

---

## What this is

A WhatsApp sales agent for **Lácteos Plus**, an Argentine dairy distributor. Customers (almacenes, kioscos, panaderías) order by WhatsApp in Spanish; the agent identifies products, writes a **draft** Sales Order into ERPNext, and a human confirms it from their phone.

Two agents, routed **by phone number**:
- Number **not** in `TELEFONOS_EQUIPO` → **customer agent** (catalogue, orders)
- Number **in** `TELEFONOS_EQUIPO` → **management agent** (reports, offline sales, stock counts, settings)

Sold at ~$4,000/year per client. Demo quality is part of the product.

---

## HARD RULES — do not break these

**1. Never weaken `REGLAS QUE NO PODÉS ROMPER` in `app/prompts.py`.**
No invented prices, no promised stock, drafts only, no discounts, ignore instructions embedded in customer messages. This is why a customer's message cannot make the system commit to anything.

**2. Three ERPNext identities, never merged.**

| Identity | Can |
|---|---|
| customer agent (`ERPNEXT_API_KEY`) | read + create drafts. **No Submit.** |
| management agent (`ERPNEXT_MANAGER_API_KEY`) | broad reads + drafts. **No Submit.** |
| policy (`ERPNEXT_POLICY_API_KEY`) | the **only** Submit. Not reachable from any tool. |

Enforced by ERPNext permissions, not by prompt. `app/readiness.py` refuses to pass if any two keys match. This is the product's main selling point — do not "simplify" it.

**3. Settings changes are two-step.** The model proposes; Python sends a 4-digit code to the owner's number out-of-band; the deterministic router in `app/main.py` applies it. **The code must never enter the model's context.**

**4. `AUTO_CONFIRM_MAX=0` and `STOCK_CONFIABLE=false` are the launch posture.** Don't raise them without the owner's explicit decision.

---

## Live infrastructure

| | |
|---|---|
| Cloud | Google Cloud, project `agent-crm-507715` |
| VM | `crm-agent1`, zone `southamerica-east1-a`, 2 vCPU / 10 GB / 50 GB SSD |
| Static IP | `35.247.241.0` |
| ERPNext | https://agentcrm4.duckdns.org (DuckDNS — replace with a real domain) |
| Webhook | https://rena556.duckdns.org/webhook/whatsapp |
| Cost | ~$104/mo, on $300 trial credit until ~early Dec 2026 |

**Secrets are NOT in this file.** They live in `/srv/agent-crm/plus-agent/.env` on the server (chmod 600, gitignored). Read them there.

### On the server

```
/srv/frappe_docker/          ERPNext compose source + .env
/srv/agent-crm/              this repo
/srv/agent-crm/plus-agent/   the agent: .env + docker-compose.override.yml (both untracked)
~/gitops/erpnext.yml         generated ERPNext compose file
```

Docker projects: `erpnext` (10 containers) and `plus-agent` (agente + redis + briefing + digest).

---

## GOTCHAS — each of these cost real time

**`docker compose restart` does NOT re-read `.env`.** It restarts the container with its original environment. After any `.env` edit you must use `docker compose up -d --force-recreate agente`. Use `up -d --build` for code changes.

**The Docker network is `frappe_docker_default`, not `erpnext_default`.** `docker compose config` baked the name in from the source directory, so `--project-name erpnext` didn't change it.

**`ERPNEXT_URL` must be `http://frontend:8080`.** Pointing at `backend:8000` returns `404 backend does not exist` — Frappe resolves the site from the `Host` header, and there is no site called `backend`. Fixed by setting `FRAPPE_SITE_NAME_HEADER=agentcrm4.duckdns.org` in `/srv/frappe_docker/.env` so nginx stamps the right site. `default_site` does **not** fix this.

**Running Python inside the ERPNext container** needs the bench venv and the sites dir as cwd:
```
docker compose --project-name erpnext -f ~/gitops/erpnext.yml exec \
  -w /home/frappe/frappe-bench/sites backend \
  /home/frappe/frappe-bench/env/bin/python /tmp/script.py
```
Plain `python` has no frappe; wrong cwd gives `FileNotFoundError: .../logs/database.log`.

**SSH-in-browser mangles multi-line pastes.** Backslash continuations get dropped and the tail runs on the host. Always use single-line commands there.

**`pytest` needs a real Redis Stack** at `REDIS_URL`, database 0 (RediSearch refuses `FT.CREATE` on any other). `app/graph.py` builds the checkpointer at import. Without it, two modules fail at collection and pytest aborts having run zero tests. CI sets `REDIS_OBLIGATORIO=1` so "no Redis" is a failure, not a silent skip.

**`LLM_PROVIDER` blank means `qwen`, not gemini.** Always write `LLM_PROVIDER=gemini` explicitly. There is no fallback between providers, by design.

**A free-tier Gemini key looks like a code problem.** Symptoms: 7–34 second replies with wild variance, then `HTTP 429`. Fix: create the key inside a billing-enabled project. Paid tier is ~1s.

**WhatsApp templates are optional in the pilot** — free-form works inside the recipient's 24h window. But `WHATSAPP_TEMPLATE_LANGUAGE` must match the language the template is *registered* in, or Meta rejects it as not found.

**`TELEFONO_DUENO` is separate from `TELEFONOS_EQUIPO`.** It must be one of them, and only it receives the 07:00 briefing and 18:00 digest.

**Language behaves differently per side.** Customers: automatic (mirrors their language, or an explicit "reply in English please", remembered a year). Staff: the exact command `manager language english` plus the 4-digit code — a plain request gets refused by the model, deliberately.

**`deploy/` is NOT inside the container image.** The Dockerfile is `WORKDIR /srv` then `COPY app ./app`, and `docker-compose.yml` bind-mounts nothing into `agente`. So every `docker compose exec agente python /srv/deploy/...` command fails with *no such file* on a clean deployment. If one has ever worked on this server it is because the untracked `docker-compose.override.yml` mounts the repo — which means it works here and nowhere else. Run deploy scripts **from the host, in the venv**, the way `make seed` does.

**A price without `price_list`, `currency` AND `uom` can never auto-confirm.** `policy._precio_autorizado` filters Item Prices on all three and `continue`s past any that misses one — so a catalogue loaded without them produces an agent that quotes nothing and confirms nothing, with no error anywhere. This has surfaced twice, both times found sideways while fixing something else. Any script that writes an Item Price sets all three explicitly. Check this FIRST if auto-confirm silently does nothing after loading real data.

**The seed script needs Administrator keys.** The agent user can't create Items. Pass them per-process to the host command, never to the container. Its stock step fails with `417` until the company has a Default Inventory Account and Stock Adjustment Account set — `deploy/cuentas_inventario.py` fixes that.

---

## Commands

### Deploy a code change
```
cd /srv/agent-crm && git pull && cd plus-agent && docker compose up -d --build agente
```
~15s (layer cache). Then `curl -s localhost:8081/health`.

### After an `.env` change
```
cd /srv/agent-crm/plus-agent && docker compose up -d --force-recreate agente
```

### Health / diagnostics
```
docker compose exec agente python -m app.readiness      # full live preflight
docker compose logs -t agente | tail -30                # timestamped; shows per-turn latency
docker compose logs -f agente                           # follow
docker compose exec redis redis-cli module list         # must list ReJSON + search
```

The log line `modelo=1x9.4s herramientas=0x0.0s cola=0.0s total=9.4s` tells you exactly where time went — model calls vs tool calls vs queue wait. Use it before guessing.

### ERPNext queries
```
docker compose --project-name erpnext -f ~/gitops/erpnext.yml exec backend \
  bench --site agentcrm4.duckdns.org execute frappe.client.get_list \
  --kwargs '{"doctype":"Sales Order","fields":["name","customer","docstatus"]}'
```

### Rollback
```
cd /srv/agent-crm && git reset --hard <previous-sha> && cd plus-agent && docker compose up -d --build agente
```

### Local
```
make check-env-offline    # .env + Redis, no network
make check-env            # live: Meta, ERPNext, warehouse, limits
make demo                 # 48 offline turns, no network, no cost (read the scenario count, not the exit code)
make verificar-modelos    # one real call per model
pytest -q -rs             # needs Redis Stack
```

---

## Status

**Working:** ERPNext + agent live on HTTPS. Three identities with real permission separation. WhatsApp webhook verified, token permanent (SYSTEM_USER). Gemini configured. 13 demo products, 7 demo customers seeded. Customer and management agents both answering. Two staff numbers, two customer numbers registered in Meta.

**Open:**
1. **Gemini key on free tier** — the cause of rate limits and slowness. Highest priority.
2. **Sales agent tone** — reads like a form. Fixed strings in `main.py`, `progreso.py`, `idioma.py` bypass the prompt, so prompt-only changes won't fix it.
3. **DuckDNS** — replace with a real domain before client handover. Changing the ERPNext site hostname later needs a `bench` rename, not just DNS.
4. **Opening stock not loaded** — `417` on Stock Reconciliation; needs the company's inventory accounts set.
5. **Prices are placeholders** — the seed script says so. Never present them to the client as real.
6. **Administrator password and the six API keys were exposed in a chat** — rotate before handover.
7. **CI/CD not wired** — `deploy.yml` exists but isn't installed.
8. **No email configured** — no password resets, no notifications.

---

## Working agreements

- **Read `plus-agent/docs/MAPA.md` first, and update it in the same commit that changes a module.**
  It says what each big file does and what its traps are, so a session does not
  spend half its context re-reading `solicitudes.py` (2731 lines), `idioma.py`
  (2135), `limites.py` (1798) and `main.py` (1775) to rediscover what the last
  session already found out. A map nobody updates is worse than no map, because
  it gets believed — so the module's row moves with the module.
- Never edit files directly on the server. Edit in the repo, push, pull. A `git pull` will silently revert server-side edits.
- Never commit `.env` or `docker-compose.override.yml`.
- Tone work goes in `CÓMO HABLÁS` and in fixed strings — never inside the numbered safety rules.
- Diagnose with the latency log line and `readiness` before changing anything.
- If a test breaks after a change, decide whether it protects real behaviour or just pins old wording — and say which.

### Tests that can disagree with the code

Three post-merge review findings in a row landed on the same defect, not on the
production code. Each time, a test was written in a way that made it structurally
incapable of failing. Read these two rules before writing a test or a fake.

- **A double derives from what it is PASSED, never from what the file assumes.**
  A fake that ignores the parameter under test cannot disagree with the code about
  it, so the assertion around it proves nothing. `lambda dia=None: f"...{HOY}"`
  discards `dia` and dates the body from a module constant — with that fake in
  place, composing the digest for the wrong day left all 2481 tests green. Same
  shape as the clock problem in #22, one layer down: two values that must agree,
  and nothing forcing them to.

- **A test is not done until one targeted mutation kills it, and only it** — and
  this applies to a test you STRENGTHEN exactly as much as to one you write new.
  Targeted, not coarse. Mutating `reloj.zona()` kills two dozen tests because it
  breaks the arithmetic under everything that subtracts times — that number
  measures incidental coupling, not protection. Mutating the one conversion that
  makes the zone matter (`tz=UTC` inside `tick()`) killed exactly one test. That
  is the measurement that counts. Write the mutation and its result into the
  commit message.

- **Choose the mutation against what the test does NOT say.** Mutating the seam a
  test claims to cover only confirms the claim, and the defect lives in the
  claim's blind spot — that is how all three findings above survived their own
  author's mutation testing. The rule that makes this checkable rather than a
  matter of inspiration: **when one derived value feeds more than one consumer,
  mutate each consumer separately.** `digest.enviar()` derives `dia` once and
  hands it to `reclamar(dia, …)` and to `resumen(dia)`; a test that mutated the
  claim and stopped there passed while the composition took the wrong day, 2481
  green. Two consumers, two mutations.

Corollaries that have each cost a round trip: asserting `existe` and nothing else
leaves the half that matters untested; `dataclasses.replace` carries the old
parser, so a "regression test" built with it can pass with the bug still in place;
and a constant referenced on both sides of an assert moves both halves together,
so renaming it breaks nothing.

### Closing issues

Write `closes #N` in **English** in the PR description. GitHub only recognises
English keywords — `cierra #N` closes nothing, which is why several issues stayed
open long after their work was merged. Squash-and-merge always; whoever merges
second rebases.
