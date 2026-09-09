# Session opener — rigidity audit (read-only, one report)

Fresh session in `plus-agent/` of `agent-crm`. Everything needed is in this message.

**First commit:** save this whole message as `docs/prompt-auditoria.md` on a branch off `main`, push it.

**This is READ-ONLY.** Change no application code. The deliverable is one report and a small set of issues. If you find a bug, file it and move on — do not fix it, do not open a PR for it.

This is not a bug hunt. Bugs have been chased hard already and the returns are falling. This audit has exactly one lens:

> **Where is the same concept written more than once?**

That is what makes a one-line change break ten tests, and what makes every new behaviour cost a whole module. Anything that isn't duplication or rigidity is out of scope — including code smells, style, coverage, and correctness. Note them in a single "not in scope, seen in passing" list at the end and stop there.

## What to look for

Four patterns, in this order of value:

1. **One concept, N implementations.** A known example to calibrate against: durable-schedule-plus-sweep is written four times (`solicitudes.py::tick`, `pendientes.py::tick`, `digest.py::tick`, and plazos would be the fifth), each re-implementing durability, exactly-once, quiet hours, re-read-before-acting and fail-closed. Find the others. Candidates worth checking, not a closed list: reading a durable marker off an ERPNext document; the Redis-cache-rebuilt-from-ERPNext pattern; "is this within Meta's 24h window"; converting a naive ERPNext timestamp; validating an owner-typed value; deciding whether a document may be written.

2. **A module that should be a row or a value.** Code whose only variation across cases is a number, a name or a text key. If adding the next case means copying a file, it belongs in a table.

3. **Hand-rolled where the stdlib or an existing dependency already does it.** Already known and confirmed: `formato.pesos` (separators by hand), `telefono.py` (Argentine phone rules by hand), the Spanish month/day maps in `tools/pedidos.py`, `_ORDEN_DIAS`. Count these once and move on — do not re-derive the analysis. Look for others.

4. **Tests that pin the wrong thing.** Not test bugs — test rigidity: tests asserting exact wording, exact field position, or a hardcoded date compared against a live clock. For each, one line: does it protect a rule, or pin a sentence?

## How to measure, so the ranking isn't opinion

For each finding, give three numbers:

- **copies** — how many places implement it
- **blast radius** — how many test files break if the concept changes (measure it: change the concept in one place and run the suite, or count the tests that touch it)
- **cost to consolidate** — rough, in half-days

Rank by `copies × blast radius ÷ cost`. That ordering is the actual output of this audit; the prose is supporting material.

## The report

`docs/AUDITORIA.md`, and keep it under 150 lines. A 227-line review committed at the repo root already happened once in this project and read better as a PR body — do not repeat it.

Structure:

1. A ranked table: finding, copies, blast radius, cost, recommendation.
2. Top 5 only, one short paragraph each: what is duplicated, where, and what consolidating it would look like. Name files and functions.
3. What you deliberately did not flag, and why — same discipline as the leak allowlist: a reason per line.
4. "Seen in passing, out of scope" — a bare list, no analysis.

Then open one issue per top-5 finding, each with the numbers and a one-paragraph consolidation sketch. Nothing else.

## Bounds — these matter more than completeness

- Read-only. No application code changes. No PRs except the report and the issues.
- **Top 5.** Not top 15. A list nobody acts on is worse than a short list that gets done.
- No consolidation work in this session, **including the "obvious" ones**. Ranking first, building second, so the order is chosen with evidence rather than by which one you happened to read.
- **Do not audit the safety core for duplication.** The three ERPNext identities, `policy.evaluar`'s linear rule list, the durable queue's idempotency scripts, and `confirmacion.py` are deliberately explicit and deliberately repetitive. `policy.evaluar` being one long linear list of independently verified rules is the design — splitting it is how a rule goes missing. Leave all of it alone.
- Time-box it. If the ranking is clear before you have read everything, stop and write it up.

## Done when

- `docs/AUDITORIA.md` exists, under 150 lines, with the ranked table and top 5.
- One issue per top-5 finding, each carrying its three numbers.
- `git status` clean apart from the report — proof nothing was changed.
- The report's first line states the single highest-value consolidation and its estimated cost. That sentence is what the next session acts on.

---

# Amendments agreed before the audit started

These four corrections were made in the same conversation that issued the brief. They are recorded here because the un-amended brief is wrong on one fact, and a later reader would reject the right answer for the wrong reason.

**1. This brief supersedes the `agenda.py` brief.** A brief saying "build `app/agenda.py` now" was issued minutes earlier. Both cannot be right. The audit wins and the agenda brief waits for its ranking — exempting the one finding already decided would undermine the audit's own argument. If the ranking puts something above durable-schedule-plus-sweep, that is the audit working.

**2. `babel` is not `locale`, and the distinction is the whole point.** The objection to consolidating `formato.pesos` onto the stdlib is sound: `locale` is process-global, `setlocale` is not thread-safe, and this app runs sweep threads. That is a real disqualifier — **for `locale`**. `babel` is pure-Python and takes the locale as an argument per call (`format_currency(value, "INR", locale="en_IN")`): no process state, no `setlocale`, thread-safe by construction. Anyone rejecting the library later on the `locale` objection has rejected the right answer for the wrong reason.

**3. `phonenumbers` is defended by the same argument used against it.** "A wrong number means a message to a stranger" is the argument *for* the library: Google's libphonenumber is more correct than any hand-rolled matcher, and `telefono.py`'s existing tests are the acceptance suite that proves equivalence before switching. The underlying principle is adopted regardless: **the report says why something stays hand-rolled, rather than counting it as debt.** A reason is a better output than a number.

**4. Test-side duplication goes in the same ranked table**, not a separate section, and the reason is sharper than lens 4 as written. `epoch()` hardcodes Buenos Aires in one file while `test_inventario.py` builds its own naive stamp from its own `AHORA` — each test file re-derived the code's assumption, so no test could ever disagree with it. That is why the timezone bug was invisible to every test that touched it. Duplicated fixtures don't just make changes expensive: **they make whole classes of bug unfalsifiable.**

**5. A breadth guard against recall bias.** Measuring rather than recalling is the right mitigation, plus one more: **at least two of the top five must come from code not touched in #16–#19.** If all five are concepts the auditor had their hands in last week, that is a recall artifact, not a ranking. Forcing breadth is cheaper than trusting neutrality.

---

**Why an audit and not another fix:** #19 and #16 fixed instances and removed no copies.
