---
name: system-design-review
description: "Review downstream system effects before and after code changes: concurrency, asyncio task lifetime, cancellation, persistence, process/deployment boundaries, ownership and shared state. Use automatically for these changes, even if the user only asks for a local implementation. Produces an executable impact plan, failure-scenario evidence and a revision-bound review gate."
---

# Design review

Do not substitute remembered best practices for evidence about this system.

This skill is a portable Python 3.12+ CLI plus a review protocol. No model/API
call is made by the CLI. No third-party dependencies. It runs through bash and
file tools in micro-cc or CI. Scripts live beside this file under
`scripts/`; use that actual installed path, not a guessed repository path.

## The obligation

For changes involving task lifetime, concurrency, persistence, cancellation,
process lifecycle, deployment or shared-state ownership:

1. **Before editing:** state the proposed change, the guarantee that matters,
   and the deployment assumptions. Run a plan with `--focus FILE::SYMBOL` for
   code you intend to touch. Read the paths and the consumers they reach.
2. **Inspect both directions:** what does this code depend on, and who depends
   on its ordering, lifetime, identity or state? Open the actual implementations
   at both ends. Include unchanged code; most consequences live there.
3. **Challenge the assumptions:** construct a concrete failure schedule. Don't
   write "consider race conditions." Write "A reads revision 8, B commits 9,
   A persists its shorter snapshot." For each new background task identify an
   owner, descendant policy, exception observer, cancellation policy and bounds.
4. **Make the change**, along with contracts/links/scenarios genuinely required
   by the design. Do not alter desired guarantees merely to agree with code.
5. **Re-plan and verify.** Inspect the proposed commands, then explicitly allow
   them to execute. Add a scenario when an uncovered risk can be exercised.
   Prefer barriers/events, fake clocks, temporary stores and controllable
   failures over timing sleeps. State exactly which parts are mocked.
6. **Record the review**, including remaining unknowns, and run the gate. A
   failing test is not waivable with prose. Report an incomplete/blocked result
   honestly if the user did not ask you to fix existing failures. Do not fix
   unrelated runtime behavior to make the design tool look successful.

The gate checks that this protocol happened against this revision. It does NOT
judge the truth of your reasoning. A user/maintainer still reviews decisions.

## Commands

Replace `$DR` with the absolute path to this skill's `scripts/design_review.py`.
All paths in the model are relative to the reviewed repo, not the skill.

```bash
python "$DR" --repo-root /repo plan --base HEAD --focus src/worker.py::run
# Or audit all declared contracts, including unchanged ones:
python "$DR" --repo-root /repo plan --all

# Read .design/plan.md and the relevant source files before executing commands.
python "$DR" --repo-root /repo verify --allow-exec

# Read .design/review.template.json; fill every obligation in .design/review.json.
python "$DR" --repo-root /repo gate
```

`--focus` adds a hypothetical impact seed; it never hides actual changes.
`--base` accepts a Git commit/ref; it compares that tree against current staged,
unstaged and untracked nonignored files. Use the branch's merge-base for a PR.
Planning writes only `.design/`; it never rewrites declarations or runs tests.
The Git index is never modified. An initialized repo with a commit is required.

Replanning refreshes the template but does not erase an existing review. That
review becomes stale if its plan ID differs. Source changes, declaration changes,
scenario inputs or tool updates invalidate existing evidence. Run the sequence
again rather than copying an old plan ID onto old reasoning.

## Files and their authority

| File | Authority |
|---|---|
| `design/architecture.toml` | Maintainer-declared desired guarantees, assumptions, semantic links and trusted scenario commands. Commit this. |
| Project-specific scenario scripts/tests | Executable observations of the declared failure schedules. Commit these. |
| `.design/plan.json`, `plan.md` | Derived impact hypothesis. Never a source of desired truth. |
| `.design/evidence.json` | Results pinned to a source/model/tool snapshot. |
| `.design/review.template.json` | Required obligations with blank answers. |
| `.design/review.json` | Revision-specific reasoning, counterexamples, evidence references and residual risk. |

Ignore `.design/` in Git. Archive the generated dossier with a PR if useful;
local evidence is not an authenticated CI attestation. Anyone who can rewrite
the repo/tool/artifacts can bypass this gate, just as they can bypass local tests.

## Declaring the first model

Start with 3–6 important boundaries, not every function in the repository.
Use `REFERENCE.md` for the full schema and a minimal example. Discovery questions:

- What state must survive task cancellation, process restart, container replacement?
- Who can write it: one task, multiple loops/threads, processes, hosts?
- Where does ownership transfer, and what exactly does acknowledgement promise?
- What has to finish before a caller may return? What can outlive its creator?
- What does a queue, cache, retry loop or task retain, and what bounds growth?
- Which state invalidates another state (history vs checkpoint, identity vs cursor)?
- Which assumptions differ by deployment profile?

An import/call graph cannot discover all these relationships. Declare semantic
edges where the language has no expression for them. For example:

```toml
[[link]]
from = "src/headless.py::drain"
to = "src/summary.py::compact"
kind = "must-cover-descendants"
why = "Drain must cover children created by compaction after shutdown starts."
```

`A -> B` means **A depends on B**. A change to B propagates to A. Resource nodes
let two otherwise disconnected pieces of code express shared ownership/state.
An observed edge is a static candidate; a declared edge is a maintainer claim.
Never relabel either as a proof.

## Evidence and review

For each obligation, fill:

```json
{
  "decision": "reviewed",
  "reasoning": "Which consumers/assumptions were inspected, and why the design holds under them.",
  "counterexample": "A specific adverse ordering/lifetime/failure schedule that was considered.",
  "evidence": "Scenario ID/test output, or source references for a manual review; never claim tests not run.",
  "residual_risk": "What this evidence does not establish."
}
```

No `accepted`/`waived` switch for failing scenarios. If a contract intentionally
changes, change the declaration and review the **policy-change** obligation.
Deleting an inconvenient scenario is a design-policy change, not fixing code.
Contracts without scenarios stay **manual / untested** even when reviewed.
For new risky symbols, an old nearby passing contract does not remove their
own review obligation. Unmapped changes and non-Python source are flagged too.

## Mechanical enforcement options

- Repo instruction: require this workflow before claiming relevant work complete.
- Commit/review gate: run `python "$DR" --repo-root /repo gate` where the dossier lives.
- Stop hook: see `REFERENCE.md`. It checks the gate; it does not run
  arbitrary repo commands or modify code. Installation is explicit, not automatic.

A Stop hook is a convenience reminder, not a security boundary or an infinite
loop: on a Stop-hook continuation it yields control so the agent can report
blocked work. Do not advertise it as impossible to bypass.

## Don't overclaim

The Python analyzer does not resolve arbitrary dynamic dispatch, callbacks,
monkeypatching, subprocess behavior, SQL transactions or external infrastructure.
Unresolved calls are counted, with impact-relevant sites in plan.json. Add links
or tests; don't interpret a missing edge as independence. Tests execute against
fresh read-only copies of planned inputs; `{repo}` resolves to that snapshot.
Write test output to temporary directories. This is not a security sandbox:
commands retain the user's access. Review them, bound runtimes and isolate state.

For an existing bug, capture the failing scenario and report it. For a local
refactor, make sure the system does not punish mere docstring/format changes.
For every detector you add, write an adversarial test proving it detects a real
semantic mutation, and a control proving it does not flag an irrelevant one.
