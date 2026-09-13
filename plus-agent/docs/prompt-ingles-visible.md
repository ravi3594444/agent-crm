# Session opener — the English demo slice

Fresh session in `plus-agent/` of `agent-crm`. Everything you need is in this message.

**First commit:** save this whole message as `docs/prompt-ingles-visible.md` on a branch off `main`, and push it.

## Where things stand — read this, it has changed

`main` is `b887ad6`. Everything is merged; there are no open PRs on this work.
Landed since the earlier `docs/prompt-demo-ingles.md` was written:

- **#28 — the CI matrix.** That was PR 1 of that brief. It is done. Every PR you open
  now runs in four environments: normal, `IDIOMA_DEFAULT=en`, `IDIOMA_GERENCIA=en`,
  `BUSINESS_TIMEZONE=Asia/Kolkata`. **You are picking up at PR 2.**
- **#26 — one business clock** (`app/reloj.py`).
- **#30 — `app/marcas.py`**, a typed registry for the twelve durable markers, with a
  test that fails if a marker's text changes. If anything you write reads or writes a
  durable ERPNext marker, it goes through that registry — not a module-local constant.
- **#27 — `docs/AUDITORIA.md`.**

**Expected, not a bug:** the `BUSINESS_TIMEZONE=Asia/Kolkata` cell prints a
`::warning title=Celda vacua`. `tests/conftest.py` still pins that variable in `_FIJAS`,
so the cell runs the same zone as the default. That is issue **#22** and it is not yours.
Do not unpin it to make the warning go away — 26 tests would fail for the wrong reason.

**Another session is running** on delivery deadlines (`docs/prompt-plazos.md`). It touches
`app/pendientes.py`, adds one row to `LIMITES` in `app/limites.py` with its one
`raise LimiteError`, and edits the owner-alert body in `app/notificar.py`. Your regions in
those two files are different ones — named below. You will both add keys to `app/idioma.py`;
additions at the end of the block rebase cleanly, and whoever merges second rebases.

---

# Why this one, and what "done" means

Dev has two prospects. One of them speaks English, and both want to see the product before
they sign. Right now an English owner gets an English alert with Spanish buttons, a price
that reads as one dollar twenty, a settings command he cannot type, and a catalogue of
*Leche entera sachet 1 L*. This is the slice that makes the demo showable — nothing more.

Four things a prospect **sees** in a ten-minute demo.

## a. The buttons are hardcoded Spanish

`app/notificar.py` sends titles as literals: `"Confirmar"` and `"Ver detalle"` (~116–117),
`"Confirmar conteo"` (~546). There is no `idioma.t` entry for button labels anywhere — so
on the one path where the owner types nothing at all, he gets Spanish. Route them through
`idioma.t` keyed on `limites.idioma_gerencia()`.

`whatsapp.enviar_botones` silently truncates a title at 20 characters, so keep them short:
`Confirm`, `View details`, `Confirm count`.

These literals are **not** on `tests/idioma_allowlist.py`, whose own rule is *"se traduce lo
que LEE UNA PERSONA en WhatsApp"*. Treat that as the bug it is and add the assertion that
would have caught it: **no button title may be a literal.**

## b. Money is formatted for Argentina only

`app/formato.py::pesos` swaps separators by hand to produce `$ 1.200,50`. For a US client
that reads as one dollar twenty. Replace it with `babel.numbers.format_currency`, driven by
a new deployment value `LOCALE` (`es_AR` | `en_US`), set at onboarding alongside the existing
`AUTO_CONFIRM_CURRENCY` — **never** settable from WhatsApp.

- **`babel` is not the stdlib `locale` module.** It takes the locale per call and holds no
  process state, so it is thread-safe. This app runs sweep threads; do not reject it on the
  `setlocale` objection.
- **Pin first, swap second.** Write a test asserting today's exact output for every existing
  fixture *before* touching the implementation, then swap and make it pass. If Babel's
  `es_AR` differs by a space or a symbol position, one custom pattern for `es_AR` is the only
  hand-tuning allowed to survive.
- Keep the name and signature `pesos(...)` as a thin wrapper so its ~40 call sites do not
  change in this PR.
- Add `en_US` fixtures: `$1,200.50`.
- **Out of scope:** phone rules and date-word parsing. Pilot needs, not demo needs.

## c. Day names are Spanish-only

`app/limites.py::_ORDEN_DIAS` (~562) accepts only `lunes, martes, miercoles…`, so
`delivery days monday,friday` fails and an English owner cannot configure `ENTREGA_DIAS`,
`ENTREGA_EXCEPCION_DIAS` or `RETIRO_LOCAL_DIAS` at all. Accept English names and common
abbreviations alongside the Spanish.

**Keep the stored canonical value Spanish** so nothing already saved migrates, and render it
through `idioma` on the way out. Follow the existing `_sin_tildes` rather than adding a
second normaliser. While you are there, confirm `_VERDADEROS`/`_FALSOS` cover the English
yes/no forms an owner would actually type.

## d. The demo data is Spanish

`deploy/seed_dairy.py` seeds *Leche entera sachet 1 L*, *Panadería López*. Add an English
dataset — same shapes, same product count, English names — selected by an argument or env
value, defaulting to the existing Spanish one. Prices are placeholders in both; keep the
script's existing note saying so.

## Explicitly NOT in this PR

English action verbs, English setting aliases, the English prompt persona. A prospect
watching a demo does not type commands, and the pitch script says not to tour settings.
Those are post-signature.

---

# Then, as a second PR: #9

Only after the slice above is merged. The audit comment on issue #9 lists what is left:
`app/tools/operaciones.py`, the **36** `raise LimiteError` in `app/limites.py` (the count has
moved since the issue was written — check it), and five team notices in `app/solicitudes.py`.
Move them into `idioma.t` with English versions.

Leave alone the one `raise LimiteError` that belongs to the plazos session's new limit, and
the one inside `_consultar_marca`.

**Close it properly:** write `closes #9` in **English** in the PR description. Spanish
keywords (`cierra #N`) close nothing — that is why several issues stayed open after their
work was merged.

---

# Constraints

- Nothing here touches `REGLAS QUE NO PODÉS ROMPER` in `app/prompts.py`, the three ERPNext
  identities, or any confirm/submit/cancel path. This PR changes what a person reads.
- Every new string through `idioma.t` in ES and EN.
- `LOCALE` is a deployment value read at startup. It is not an owner limit and never arrives
  by WhatsApp.
- No wholesale reformatting of `limites.py` or `notificar.py` — the plazos session is in both.

# Done when

- `ruff check app demo tests`; `pytest -q` (Redis Stack at `REDIS_URL`, db 0);
  `python -m demo.piloto --modo offline` — 48 turns, no new failures (read the scenario
  count, not the wrapper's exit code).
- Green in all four matrix cells.
- Tests: no button title is a literal; `pesos()` output is byte-identical for every
  pre-existing `es_AR` fixture; `en_US` renders `$1,200.50`; `delivery days monday,friday`
  is accepted and stores the Spanish canonical value.
- **The check that counts:** set `IDIOMA_GERENCIA=en` and `LOCALE=en_US`, seed the English
  dataset, and walk the demo on the live number. An English owner sees English buttons,
  `$1,200.50`, and English product names — with no Spanish anywhere he looks.
