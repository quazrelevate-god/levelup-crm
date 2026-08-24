> **Historical.** This described how to hand the empty repository to Claude Code
> at the very beginning, and it names `EXPLAIN-SIMPLE.md`, which has since been
> deleted. The project is now M0–M11 complete.
>
> For setting the project up, read
> [`docs/08-handoff-voice-agent.md`](docs/08-handoff-voice-agent.md).
> For what to build next, read
> [`docs/06-voice-integration-contract.md`](docs/06-voice-integration-contract.md).
> Kept because the reasoning about what to give an agent, and in what order, is
> still worth reading.

---

# Start here

How to hand this to Claude Code and begin building.

---

## 1. What to give Claude Code

**All seven files below. Nothing else.** They are written to be read together and
they cross-reference each other. (This START-HERE is for you, not the agent —
keeping it in the repo is harmless either way.)

```
configurable-crm/                 ← your new empty repo
├── CLAUDE.md                     ← auto-loaded by Claude Code every session
├── PROMPTS.md                    ← for you, not the agent — copy/paste source
├── EXPLAIN-SIMPLE.md             ← plain-English overview for anyone new
├── START-HERE.md                 ← this file
└── docs/
    ├── 00-milestones.md          what to build, in order, with acceptance criteria
    ├── 01-data-model.md          full Postgres schema
    ├── 02-api-contract.md        every endpoint + the permission test matrix
    ├── 03-configuration-model.md what admins can configure — the product spec
    └── 04-feature-coverage.md    TeleCRM feature audit, 80 items
```

`CLAUDE.md` must sit at the repo root — Claude Code loads it automatically on
every request. That is why the non-negotiable rules live there and not in `docs/`.

### What NOT to give it

The five research documents from earlier (`01-functional-spec.md`,
`03-lead-fields.md`, `04-custom-actions.md`, `05-workflows.md`,
`06-mobile-companion.md`) describe **the legacy TeleCRM system and LevelUp's own
data**. They contain hardcoded taxonomy (`FORGE_WRITING`, `INTERVIEW_SCHEDULED`)
and out-of-scope subsystems (workflow engine, Android app).

Keep them for your own reference. **Do not put them in the repo.** An agent that
reads them will start seeding LevelUp's field list into a product meant to have
none.

---

## 2. Setup — five minutes

```bash
mkdir configurable-crm && cd configurable-crm
git init
mkdir docs
# copy CLAUDE.md, PROMPTS.md into ./
# copy the five doc files into ./docs/

git add -A && git commit -m "docs: project specification"
claude
```

---

## 3. First run — the calibration prompt

Before M0, paste the review prompt at the top of `PROMPTS.md`. It asks Claude to
read everything and report contradictions **without writing code**.

Judge the answer on one thing: when asked where it would be tempting to hardcode
a taxonomy, does it name real places — stage enums, dropdown option seeds,
report groupings — and explain how config-as-data prevents it? If the answer is
vague, the docs need work before you spend a milestone finding out.

Re-run this prompt at the start of **every fresh session**. Context does not
survive.

---

## 4. The loop, per milestone

```
1. Paste the M{n} prompt from PROMPTS.md
2. Let it run to completion — don't interrupt to redirect small things
3. Review the diff against the milestone's acceptance criteria in 00-milestones.md
4. Run the checks yourself:
     cd api && uv run ruff check . && uv run mypy app && uv run pytest
     cd web && pnpm lint && pnpm tsc --noEmit
5. Exercise it by hand against the demo workspace
6. Commit
7. Only then move to M{n+1}
```

`PROMPTS.md` ends with recovery prompts for the failure modes you'll actually
hit — hardcoded taxonomy, bypassed permissions, missing changesets, scope creep,
slow endpoints. Use them verbatim; they're phrased to make the agent diagnose
rather than patch.

**One milestone per session where you can.** M2, M5 and M6 are large enough that
context drift is real — if it starts forgetting rules, use the context-drift
recovery prompt rather than pushing on.

---

## 5. Order of work, and what can run in parallel

```
M0 ─ M1 ─ M2 ─ M3 ─ M4 ─ M5 ─ M6 ─ M7 ─ M8 ─ M9 ─ M10 ─ M11
                    │    │
                    │    └─ changesets + transition indexes + templates
                    └────── gates every lead endpoint
```

Strictly sequential. M4 gates all lead work; M5 carries three dependencies that
M6, M7 and M8 assume exist.

**What you can do in parallel, off the keyboard:** six features in the plan were
identified from a documentation table of contents, not from the live product. I
know they exist and roughly what they do; I don't know their exact rules.

| Feature | Needed by | Verify before |
|---|---|---|
| Manage User's Availability | M1 | week 1 |
| Templates (personal / role-specific) | M5 | week 4 |
| Excel Advance Distribution | M7 | week 8 |
| Edit Report & Undo | M7 | week 8 |
| Upcoming Leads Assignment | M8 | week 10 |
| Sales Group | M8 | week 10 |

None blocks M0–M4. Start building now; I can open those doc pages in your
browser and pin down the rules while M0–M2 are underway.

---

## 6. Where the risk actually is

| Milestone | Why it's risky | What to watch |
|---|---|---|
| **M2** — field engine | 13 types, three of them composite | Does a dependent dropdown cascade in the *filter builder* and *import*, not just the form? |
| **M4** — permissions | Can't be retrofitted | Are there exactly two chokepoints, or has projection logic leaked into endpoints? |
| **M5** — leads | Three hidden dependencies | Do three simultaneous edits share one changeset id? |
| **M6** — history filters | New query shape | `EXPLAIN ANALYZE` on `action_not_performed` — that's the one that table-scans |

If a milestone finishes suspiciously fast, check it against the acceptance
criteria before celebrating. Fast usually means something in scope got skipped.

---

## 7. Honest expectations

**71–94 working days, ~14–19 weeks solo** for v1 — the configurable CRM core.
That excludes campaigns, salesform, and the workflow engine.

The estimate assumes one focused developer. It does not compress well against
interruptions: M4 through M8 each carry enough interlocking state that a
half-day-a-week pace will take considerably more than four times as long.

First externally-demoable point is **end of M6** — a working configurable CRM
with leads, timeline and filters. That's roughly week 9. If you need something to
show LevelUp sooner, M5 (week 6) demos the configuration story convincingly even
without the list polish.
