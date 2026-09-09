# Session opener — English demo slice, #9 and #13

Fresh session in `plus-agent/` of `agent-crm`. Everything needed is in this message.

**First commit:** save this whole message as `docs/prompt-demo-ingles.md` on a branch off `main`, push it.

**Three PRs, in this order.** Do not bundle them — each is small, independently mergeable and independently revertable.

**Context.** There are now two real prospects: one English-speaking, one Spanish. Both want to *see the system work* before anything else. A demo needs far less than a running pilot, and this session builds only the demo-visible part plus two pieces of hygiene.

**Review discipline.** All of this is low-risk — it cannot confirm, submit, cancel or move money. Build it, verify it works, one light review pass per PR, merge. Mutation-check only what the Definition of Done names. No bot round-trips, no critique PRs.

**Two other sessions are running.** One is on `app/` durable markers (`docs/prompt-marcas.md`) and may touch `solicitudes.py` and `limites.py`. One is on `tests/` fixtures. **Rebase onto `main` and re-run the gates before merging each of your PRs** — and see the ordering note in PR 3, which exists to avoid that collision.

---

# PR 1 — #13: CI checks four environments, not one

**Do this first.** Two combination failures reached `main` in one day because CI runs the suite in a single language and a single timezone. The four-environment verification currently happens by hand before each merge. Put it in CI, and every later PR — including PRs 2 and 3 below — gets checked automatically.

Matrix on pull requests, **four combinations, not sixteen**:

| | |
|---|---|
| normal | (defaults) |
| `IDIOMA_DEFAULT=en` | |
| `IDIOMA_GERENCIA=en` | |
| `BUSINESS_TIMEZONE=Asia/Kolkata` | |

**Do not put the 15-wall-clock-moment time-travel sweep in the PR run.** It is slow and it belongs on a schedule or a manual `make` target. If you add the `time-machine` dev dependency, that is fine; just keep it out of the per-PR path.

Touches `.github/workflows/` (and possibly a dev requirements file). Nothing else. Cannot collide with any other session.

**Done when:** the matrix runs on this PR itself and all four cells are green.

# PR 2 — the English demo slice

Four things a prospect *sees* in a ten-minute demo. Nothing else.

**a. The buttons are hardcoded Spanish.** `app/notificar.py` sends titles as literals — `"Confirmar"`, `"Ver detalle"`, `"Confirmar conteo"`. There is no `idioma.t` entry for button labels anywhere, so an owner with `IDIOMA_GERENCIA=en` gets an English alert with Spanish buttons, on the one path where he types nothing at all. Route them through `idioma.t` in `limites.idioma_gerencia()`. `whatsapp.enviar_botones` silently truncates a title at 20 characters, so keep them short: `Confirm`, `View details`, `Confirm count`. These literals are **not** on `tests/idioma_allowlist.py`, whose own rule is *"se traduce lo que LEE UNA PERSONA en WhatsApp"* — treat this as a bug and add the assertion that would have caught it: **no button title may be a literal.**

**b. Money is formatted for Argentina only.** `formato.pesos` swaps separators by hand to produce `$ 1.200,50`. For a US client that reads as one dollar twenty. Replace it with `babel.numbers.format_currency`, driven by a new deployment value `LOCALE` (`es_AR` | `en_US`, set at onboarding, never from WhatsApp) alongside the existing `AUTO_CONFIRM_CURRENCY`.

  - **`babel` is not the stdlib `locale` module.** It takes the locale as an argument per call and holds no process state, so it is thread-safe — which matters because this app runs sweep threads. Do not reject it on the `setlocale` objection.
  - **Pin first, swap second.** Write a test asserting today's exact output for every existing fixture *before* touching the implementation, then swap and make it pass. If Babel's `es_AR` differs by a space or symbol position, one custom pattern for `es_AR` is the only hand-tuning allowed to survive.
  - Keep the name and signature `pesos(...)` as a thin wrapper so its ~40 call sites do not change in this PR.
  - Add `en_US` fixtures: `$1,200.50`.
  - **Out of scope:** phone rules and date-word parsing. Those are running-a-pilot needs, not demo needs.

**c. Day names are Spanish-only.** `app/limites.py::_ORDEN_DIAS` accepts only `lunes, martes, miercoles…`, so `delivery days monday,friday` fails and an English owner cannot configure `ENTREGA_DIAS`, `ENTREGA_EXCEPCION_DIAS` or `RETIRO_LOCAL_DIAS` at all. Accept English names and abbreviations alongside the Spanish. **Keep the stored canonical value Spanish** so nothing already saved migrates, and render it through `idioma` on the way out. Follow `_sin_tildes` rather than adding a second normaliser. While there, confirm `_VERDADEROS`/`_FALSOS` cover the English yes/no forms an owner would type.

**d. The demo data is Spanish.** `deploy/seed_dairy.py` seeds *Leche entera sachet 1L*, *Panadería López*. Add an English dataset — same shapes, same product count, English names — selected by an argument or env value, defaulting to the existing Spanish one. The prices are placeholders in both; keep the script's existing note saying so.

**Explicitly not in this PR:** English action verbs, English setting aliases, the English prompt persona. A prospect watching a demo does not type commands, and the pitch script says not to tour settings. Those are post-signature.

# PR 3 — #9: the remaining Spanish strings

The audit comment on issue #9 lists what is left: `app/tools/operaciones.py`, the 33 `raise LimiteError` in `app/limites.py`, and five team notices in `app/solicitudes.py`. Move them into `idioma.t` with English versions.

**Ordering, and the reason:** do this **last**, and rebase onto `main` first. The `marcas` session may be editing `solicitudes.py` and `limites.py`; letting it merge first means you resolve nothing. If it has not merged by the time you get here, rebase, re-run all four environments, and only then merge.

**Close it properly:** write `closes #9` in **English** in the PR description. Spanish keywords (`cierra #N`) close nothing — that is why several issues stayed open after their work was merged.

# Constraints, all three PRs

- **Additive only.** A pilot runs in Spanish: no Spanish button, day name, alias or string stops working.
- Every new customer- or staff-facing string goes through `idioma.t` in **both** languages; the new limit values get English aliases.
- `LOCALE` is a deployment value set at onboarding — not an owner limit, not changeable from WhatsApp. Changing a running business's currency would corrupt every existing price.
- Ambiguity stays fail-closed: ask, never guess.
- No change to the 4-digit/6-digit code flows, to `REGLAS QUE NO PODÉS ROMPER`, to any tool signature, or to the three ERPNext identities.
- Customer-side accept/reject regexes are already bilingual — read them, do not touch them.

# Done when

- Per PR: `ruff check app demo tests`; `pytest -q` (Redis Stack at `REDIS_URL`, db 0); `python -m demo.piloto --modo offline` — read the scenario count, not the wrapper's exit code.
- After PR 1, every subsequent PR is green in **all four** matrix cells. That is the point of doing it first.
- PR 2 tests: no button title is a literal; `pesos()` output is byte-identical for every pre-existing `es_AR` fixture; `en_US` renders `$1,200.50`; `delivery days monday,friday` is accepted and stores the Spanish canonical value.
- PR 3: `tests/test_idioma_cobertura.py` passes with additions only; with `IDIOMA_GERENCIA=en` no staff-facing string is Spanish.
- **The check that counts:** with English set on the live number, the order alert's buttons read `Confirm` / `View details`, tapping one works, and an amount renders as `$1,200.50` under `LOCALE=en_US`.

---

# Nota agregada al arrancar: la cuarta celda es VACUA hoy

La celda `BUSINESS_TIMEZONE=Asia/Kolkata` **no puede fallar** en este momento, y hay que saberlo antes de apoyar el orden de los PRs en ella.

`tests/conftest.py` tiene `BUSINESS_TIMEZONE` en `_FIJAS`, y esa lista se aplica con `os.environ[_k] = _v` —asignación incondicional, no `setdefault`— al importarse el conftest. O sea que una variable puesta por CI se **sobrescribe** antes de que corra un solo test. Medido: `BUSINESS_TIMEZONE=Asia/Kolkata pytest -q` da exactamente el mismo resultado que la corrida normal.

Y el pin es **correcto**: `tests/test_pendientes.py::epoch()` tiene Buenos Aires escrito a mano, así que el reloj del negocio tiene que ser el mismo o se caen 26 tests por la razón equivocada. Ahí fijar **es** el arreglo, al contrario del idioma.

Así que la celda entra —cuesta una fila y es correcta tenerla— pero se declara vacua en el propio workflow en vez de dejarla como un verde que no significa nada, que es la falla que esta semana se arregló tres veces. Se vuelve real cuando aterrice el issue #22 (la sesión de `tests/`), que hace que cada test declare su zona igual que #17 hizo con el idioma. El workflow nombra ese issue.

**Consecuencia para el orden:** los PRs 2 y 3 quedan cubiertos automáticamente en las **tres** celdas de idioma, que son las que atraparon las dos fallas de combinación de esta semana. La de zona todavía no cubre nada.
