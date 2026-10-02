# Model, evidence and integrations

## Minimal portable project

Copy this entire skill directory into `~/.micro-cc/skills/system-design-review/` for
all micro-cc projects, or a project's `skills/system-design-review/` for micro-cc project-local use. The built-in micro-cc skill
already ships with its scripts. Do not maintain two independent script copies in
one repo. Invoke the script in the skill directly.

Create `design/architecture.toml`:

```toml
version = 1
# These are import roots: use "src", not "src/my_package" for a src-layout package.
source_roots = ["src"]

[[contract]]
id = "worker-shutdown"
statement = "Required worker descendants finish or are reported incomplete before shutdown returns."
anchors = ["src/app.py::shutdown"]
assumptions = ["A single process owns this run; returned success means required work completed."]
questions = ["Walk a child spawned after shutdown starts, a failed child and a stuck child."]
scenarios = ["late-child"]

[[scenario]]
id = "late-child"
command = ["{python}", "tests/test_late_child.py", "--repo-root", "{repo}"]
timeout = 10
inputs = ["tests/test_late_child.py"]
scope = "Actual shutdown implementation with an event-controlled fake worker; not production I/O."
```

Every list/string shown is required except `contract.scenarios`, which defaults
to empty. Empty contract lists, unknown fields, missing anchors, duplicate IDs,
missing scenario inputs, parse failures and broken declared links fail closed.
Scenario input paths must stay inside the repo. List any test fixtures/scripts
that might otherwise be ignored by Git; they are included in the snapshot.
All tracked and nonignored untracked files are also included in the snapshot.
Changing a declared scenario input selects its contracts even without `--all`.
Interpreter path/version/platform are pinned too; environment variables, installed
third-party packages, external services and machine state are not fully captured.
For evidence dependent on those, use a reproducible environment and add explicit
configuration/fixture inputs. A local code digest is not a deployment attestation.
Ignoring `.design/` is automatic for hashing, to avoid self-invalidation.

Each reference is `repo/relative/file.py::qualified.symbol`, not a line number.
Methods/nested functions use dots; properties' setters use `name@setter`.
`file.py::<module>` denotes module-level executable statements/globals/imports.
Resource references use `resource:ID`.

Optional resource and semantic link:

```toml
[[resource]]
id = "job-state"
kind = "durable-state"
lifetime = "survives container replacement"
description = "Accepted jobs remain recoverable until their effects commit."

[[link]]
from = "resource:job-state"
to = "src/queue.py::ack"
kind = "acknowledged-by"
why = "Moving acknowledgement changes the durability boundary."
```

These are reviewed declarations, not automatically discovered runtime facts.
`kind` is a descriptive relationship, not a built-in correctness theorem.

## What each step does

**Plan** parses the before/after Python trees, compares semantic AST hashes,
resolves direct local/imported call candidates, task-spawn candidates and
conservative references to local/imported module data, combines
old/new call edges with declared semantic dependencies, then walks reverse edges
from changed or focused symbols. It also walks changed callers' dependencies to
known contract/link boundaries: adding a second caller of an unchanged store is
a topology change. These paths are labelled `Changed usage`, not an implementation
change. Recognized code/configuration files outside source roots create explicit
outside-analysis obligations. Old call edges matter when a call is removed.
Module-level changes conservatively seed every symbol in the file. Comments and
docstrings are ignored for impact but still invalidate evidence hashes.

Risk probes identify calls associated with task lifetime, locks, persistent writes,
processes and environment access. Changed await sites also prompt cancellation
review. These triggers create questions, not automatic bug verdicts. New risky
scopes get review obligations even when an existing contract is affected.
Unmapped changed scopes and non-Python files get coverage-gap obligations.
The tool does not infer complete data flow, lifetime/ownership, bounded growth,
thread topology or all filesystem writes.

**Verify** executes only selected scenario argument arrays, without a shell, in
a fresh read-only snapshot of the planned files for each scenario. `{python}` is
the current interpreter; `{repo}` is this evidence snapshot, NOT the live checkout.
Ignored secrets/runtime files and the Git database are not copied. Listed scenario
inputs are copied even if ignored. Write test output/caches/state to temporary
directories, not the snapshot. Python bytecode writing is disabled. User-immutable
file flags are applied where supported in addition to read-only permissions.
A scenario must use ordinary exit codes: 0 property held for the exercised case,
1 assertion/command failure, other code setup/execution error. Python unhandled
exceptions also exit 1, so inspect the captured output, not just the status label. Each command
has a timeout; on POSIX its process group is killed on timeout and cleaned up on
completion. Children that deliberately detach can escape that group: this is
resource cleanup, not a sandbox. Output is retained up to 64KB per scenario.
Commands can access the user environment and machine; trust/review them first.
Snapshot bytes and the live source are rechecked after each execution. Ordinary
write-and-restore tests are blocked by read-only/immutable snapshot protection,
not merely by final hashes. A hostile same-user test can chmod/clear user flags,
read external paths, forge success, or mutate the live checkout by an explicit
absolute path. Root/admin execution can bypass file permissions too. This is
isolation against accidental test/source coupling, not unforgeable execution
attestation. Validate tests and use a true sandbox for an adversarial trust model.

**Gate** requires a current plan, current passing evidence for every selected
scenario, no model/parse diagnostics and completed entries for every review
obligation. Missing and failed evidence cannot be overridden in review.json.
It validates review fields exist, not whether their reasoning is correct.
Results are **REVIEWED**, never **PROVEN**. Manual-only contracts remain untested.

Exit codes: plan 0 generated, 1 diagnostics; verify 0 selected cases passed,
1 failed/error/timeout; gate 0 reviewed, 1 blocked; 2 input/setup/staleness error.

## Stop hook (optional)

Any agent runtime with a Stop hook can call the `hook` subcommand. The runtime
supplies the project directory and a JSON event on stdin; adjust the Python
executable and script path for your installation.

```bash
python3 "$PROJECT_DIR/src/micro_cc/skills/system-design-review/scripts/design_review.py" --repo-root "$PROJECT_DIR" hook
```

The hook prints a JSON `decision: block` on a missing/stale/failed review, with
instructions to inspect the plan. It does not execute scenarios. It respects
`stop_hook_active` to avoid recursive blocking. It has a protocol test in the
repo, but validate it against your runtime before treating it as a workflow
dependency. It is not installed automatically.

## CI and review authority

The model and tests are committed; local .design artifacts are not. In CI, run
plan/verify on the exact reviewed tree. To gate the manual review too, supply
its matching review.json as a reviewed PR artifact and rerun gate there.
The source/model/tool digest must match; moving or editing the reviewed tree
requires a new plan. No signatures, tamper resistance, remote attestations or
branch-protection rules are provided by this first version.

## Broadening the system without building a pretend prover

1. Add domain scenarios: cancellation schedules, overlapping writers, interrupted
   handoffs, rewind during compaction, queue backpressure, repeated start/stop.
2. Add semantic links when a concrete incident demonstrates a missing dependency.
3. Add language adapters or runtime traces as **observations**, not replacements
   for desired guarantees.
4. Evaluate each new detector against known failures and counterexamples; track
   missed failures and irrelevant alarms, not the number of green checks.
5. Use state-machine/model checking for a bounded protocol when schedule space
   justifies it. Connect its properties back to implementation tests; a correct
   model with a divergent implementation is not a correct system.
