---
name: graph-orchestration
description: Reference for the /graph subagent-orchestration mechanism — how spawning headless subagents (bash_ + microcc-headless), the ambient tracked-subagent status line, checkpoint wakeups (DONE/FAILED/PAUSED/NEEDS_INPUT), and message_session_ coordination between subagents actually work. /graph itself is only ever invoked once, by the user, at the start of an orchestration — re-read THIS skill any time afterward (especially right after a checkpoint wakeup) to reload the full mechanism instead of relying on that one-time turn still being in context.
---

/graph was run once by the user to start orchestrating multiple headless
subagents (microcc-headless) toward a larger goal. There is no manifest file
or JSON graph format — the orchestration is designed turn-by-turn, in your
own reasoning, and adjusted as results come in. This file is the durable
copy of that mechanism: the /graph turn that kicked things off can scroll
out of a long conversation or get compressed away by auto-summarization, but
this skill and GRAPH_PLAN.md (in the project directory — your own specific
plan, not this generic mechanism) always survive that. If you need exact
ground truth instead of this paraphrase (debugging something unexpected),
read_ the file named next to each mechanism below.

## Spawning

Spawn each subagent with bash_: `microcc-headless <project_dir> "<prompt>"` —
no trailing `&`. Spawned from your bash_, it registers the subagent, detaches
it into the background itself, and returns within a second or two with one of:

- `▶ STARTED — <name> running detached as pid N (log: …)` — it's running.
- `✗ REFUSED — <why>` — it did NOT start. Usually the concurrent-subagent cap
  (the user's /subagents setting, default 4) is reached: wait for a checkpoint
  wakeup, or stop one, then spawn it again. Don't retry in a loop.

(Mechanism: start_headless_.py's _spawn_detached. `--foreground` makes it
block instead, which from bash_ means hitting bash_'s own timeout — avoid.)
microcc-headless runs project_dir's conversation through ONE autonomous turn with
dangerous_tools=set() — no approval gate, so scope each subagent's prompt
safely — and automatically appends a status-marker instruction (the model
must end its reply with STATUS: DONE/NEEDS_INPUT/FAILED); that's where the
DONE/FAILED/PAUSED/NEEDS_INPUT checkpoints below come from — you don't add
that instruction yourself (start_headless_.py has the exact mechanism). Give
each subagent its own project_dir — a fresh path is a clean isolated
conversation scope, decided by you turn-by-turn (versus /batch's fixed list
of named tasks predeclared upfront in one manifest file).

You can pick a different --model per subagent if the task calls for it (e.g.
a cheaper/faster model for a simple fan-out step, a stronger one for the
synthesis step) — but what counts as a valid value depends on the currently
logged-in backend, not always models/registry.py's MODEL_OPTIONS:

- Anthropic/Foundry/LiteLLM/OpenAI: --model takes an exact alias from
  MODEL_OPTIONS, not a family name or a guess. Read the live list before
  spawning (from the package base dir resolved in "Finding mechanism
  internals" below):
  `bash_("python3 -c 'from micro_cc.models.registry import MODEL_OPTIONS; print(MODEL_OPTIONS)'")`.
  Do not hardcode or remember a list from an earlier turn — it drifts.
  resolve() passes an unrecognized alias straight through to the API
  unchanged (by design, so a typo fails loudly instead of silently
  downgrading to the default model), so a MODEL_OPTIONS typo will just
  error.
- OpenRouter/Ollama: there is no fixed alias table — the model is whatever
  free-text slug/tag the user entered at /login (e.g.
  "qwen/qwen3-235b-a22b-thinking-2507"), and MODEL_OPTIONS does not contain
  it at all. Passing a MODEL_OPTIONS alias like "sonnet-5" here is wrong —
  resolve() passes it through unchanged and OpenRouter/Ollama will reject it
  as an invalid model. Read the actual configured value instead of
  guessing:
  `bash_("python3 -c 'from micro_cc.utils.settings_store_ import get_setting; print(get_setting(\"model\"))'")`
  (~/.micro-cc/settings.json's "model" key — every /login path writes the
  model actually in use there, including these two free-text backends).

Omitting --model entirely spawns with whatever settings.json's "model" key
currently holds (start_headless_.py reads it as the fallback), i.e. the
same model the live session is actually running — not a hardcoded
Anthropic default. Still worth checking the live value yourself with the
snippet above before choosing a *different* --model per subagent, since
that's the one case where you need to know the current one to justify
deviating from it.

Pass --max-loops <N> when a subagent's task is open-ended enough that it
could keep calling tools indefinitely instead of converging (e.g. "keep
investigating until you find the root cause") — it caps that subagent's own
tool-call rounds and, if hit, the subagent stops itself and reports PAUSED
(same checkpoint you'd get from a real question_asked) instead of running
unattended forever. Relaunch it exactly like any other PAUSED subagent
(`microcc-headless <same project_dir> "<refined or continuing prompt>"`) to
pick its history back up. Omit it for a subagent whose task is naturally
bounded (a fixed set of files to edit, a single lookup) — unlimited is the
default, same as not passing it at all.

## Tracking, the status line, and wakeups

The moment you run a bash_ command containing microcc-headless, that
subagent self-registers into YOUR tracker — no separate registration step.
You'll see an ambient status line under the input showing every tracked
subagent as one glyph + short name (▶ running, ✓ done, ✗ failed, ⏸ paused,
? needs input), and you'll automatically get woken up with a new turn the
instant one of them hits a checkpoint — so you genuinely don't need to sit
and poll. You can still call monitor_(action="check", targets=[project_dirs...])
any time you want an on-demand full-detail snapshot instead of waiting to be
woken (e.g. right after spawning a batch). Calling it repeatedly, including on
an already-finished subagent, is safe — it will not cause a duplicate wakeup
or resurrect something already cleaned up. monitor_ is only in scope on a
turn where there's actually a tracked subagent, a backgrounded bash process,
or an active watch to check on — claude_loop_.py adds it conditionally, so
you won't see it as a tool otherwise.

For anything you'd otherwise have to poll for repeatedly instead of just
waiting for the built-in checkpoint wakeup above — e.g. watching a
subagent's own messages.jsonl for something more specific than DONE/FAILED,
or a bash_-backgrounded process's output for a condition — use
monitor_(action="watch", command=..., description=...) instead: a shell
filter (e.g. `tail -f <path> | grep --line-buffered <pattern>`) over
whatever you're watching, pushed to you as its own turn the moment a line
matches, so you never have to remember to check back in. Stop it early with
monitor_(action="stop", watch_id=...); it auto-stops itself if it ever fires
too fast for a real filter to explain. Only works in the live interactive
session, same reason the ambient status line/wakeup mechanism itself does —
a headless/batch run has no idle loop standing by to receive a push.

Mechanism: utils/subagent_tracker_.py (one record per subagent, one file per
boss, fcntl-locked since a subagent is a separate OS process) and
start_live_tui_.py's _poll_subagents_tick/_wake_for_subagent (the 3s polling
loop that updates the status line and fires wakeups); monitor_'s own
status-reading logic is in tools/monitor_.py, and the streaming watch
mechanism is in tools/monitor_watch_.py.

Each tracked subagent is one record with its own `status` and a `notified`
flag — not membership in a list. A DONE/FAILED subagent stops appearing on
the status line the instant you're woken for it (notified flips to true
right after), but the record itself stays on disk; a separate cleanup pass
reaps it later, fully decoupled from the wakeup itself, so you never get
double-woken for the same completion. PAUSED/NEEDS_INPUT subagents are never
hidden and never get cleaned up — they stay visible on the status line until
you either relaunch them (`microcc-headless <same project_dir> "<answer>"`,
which resets that record to a fresh unnotified state so its next checkpoint
can wake you again) or the user decides to abandon that branch. This is
deliberate: if a real subagent is sitting there paused, you should keep
seeing it, not have it silently time out of view.

## Handling a checkpoint

When woken (or when you check in), decide what to do with what changed —
this is dynamic reevaluation, not a fixed pipeline: PAUSED means the
subagent hit a question it couldn't answer itself and already exited —
decide the answer yourself if you can, or ask the user, then relaunch it
with `microcc-headless <same project_dir> "<answer>"` to continue its exact
same history. FAILED means decide whether to retry, redesign that branch, or
abandon it. DONE means decide what depends on it and whether to kick that
off now. Whatever you decide, update GRAPH_PLAN.md to reflect it — a stale
plan file is worse than no plan file, since a future wakeup (or a future
you, with no memory of this turn) will trust what it says.

## Subagent-to-subagent coordination

Subagents can coordinate directly with each other via
message_session_(target_project_dir, message) from inside their own
prompts — e.g. tell one "when you're done, message_session_ your findings to
<sibling's project_dir>". This works even if the sibling isn't running right
now (durable inbox fallback — picked up next time that project_dir runs), so
you don't have to be the relay for everything. Mechanism:
tools/message_session_.py, backed by utils/session_ipc_.py (live socket
path) and utils/inbox_store_.py (durable fallback path).

## Stopping a subagent

To stop a running subagent — including to kill and relaunch one that looks
stuck — kill its OS pid; that is the entire mechanism. The pid is in the
`▶ STARTED` line its spawn returned, and `monitor_(action="check")` shows it
too, with an activity line: "running bash_ for 3m12s" vs "waiting on the
model for 4m" vs "pid N NOT running". Use it to tell stuck from slow: a tool
batch frozen for minutes is stuck; a long first model call on a big prompt
(litellm especially) is often just slow.

A plain `kill <pid>` (SIGTERM) stops it gracefully: its in-flight and
backgrounded shell commands are killed with it (they'd otherwise keep
burning CPU as orphans), and its transcript ends cleanly with
`STATUS: FAILED — stopped by SIGTERM while running <tool>`, so relaunching
the same project_dir continues without a corrupt turn. A second `kill`
exits immediately. If the pid has scrolled out of context, the tracker
entry is the fallback source: subagents[project_dir]["pid"] in
the tracker json at the deterministic path derived below, set from the
subagent process's own os.getpid() at self-registration
(start_headless_.py's run_headless) - not inferred, not pattern-matched.

Do NOT use `ps`/`pgrep` pattern-matching on command text (e.g. `pgrep -f
"microcc-headless.*<project_dir>"`) to find it. start_headless_.py's own
docstring documents that this exact style of approach - regex/shlex over
`ps aux | grep microcc-headless`-ish output - was tried before, for the
tracker's own registration, and abandoned because it "can't reliably tell a
real microcc-headless invocation apart from the literal string
microcc-headless appearing anywhere" and mistracked garbage project_dirs in
production. Same fragility applies here; use the process-status reminder or
the tracker's pid field instead.

Once you have the pid: `bash_("kill <pid>")`. Do NOT also try to edit, delete, or "clean up" the tracker file yourself
afterward, and do NOT grep/find anywhere just to "locate" or "verify" the
tracker - that's the mistake that reliably times out a turn. start_live_tui_.py's
_poll_subagents_tick already self-heals this on its own next 3s tick: it
notices `os.kill(pid, 0)` now raises ProcessLookupError, marks that entry
FAILED itself with summary "process is no longer running (stopped or killed
before finishing)", and fires the same checkpoint wakeup you'd get from a
natural completion. So the subagent disappears from the status line and you
get notified on your own - zero extra action needed once the pid is dead.

## Finding mechanism internals: never a global filesystem search

Every mechanism above already names its own file path (e.g.
utils/subagent_tracker_.py, tools/monitor_.py, tools/monitor_watch_.py,
start_headless_.py, start_live_tui_.py). Every one of those paths is relative to
wherever the micro_cc PACKAGE is actually installed on this machine — NOT
project_dir, and NOT wherever this skill file itself lives. micro_cc ships
on PyPI, so that install location varies (editable dev checkout vs. a real
pip install) and is never the same directory as project_dir, which is the
user's own project you're working in — a bare relative read_ against
project_dir for one of these paths will just fail. Resolve the real base
once with `bash_("python3 -c 'import micro_cc, os; print(os.path.dirname(micro_cc.__file__))'")`
and prefix every relative path above with that output before reading it.
Skipping this resolution step — trying the bare relative path, having it
fail, then reaching for an unscoped search to compensate — is exactly the
failure mode this section exists to prevent.

If you need to inspect a mechanism file, read_ or grep_ that resolved path
directly — don't reach for `find /` or any other unscoped recursive
search across the whole filesystem to "locate" it. A search rooted at `/`
walks the entire disk (every mounted volume, iCloud-synced folders, node_modules
trees, system directories) and reliably blows past a bash_ call's timeout
before it finds anything, which wastes a turn and produces no result. If you
genuinely don't know a path and the text above doesn't name it, scope the
search instead: the package directory itself (the same one every mechanism
path above is relative to), or `~/.micro-cc/` for any tracker/session/memory
state, or the specific project_dir(s) already in play for this
orchestration. That's a strict superset of anywhere a graph-orchestration
file could actually live.

The tracker file itself never needs finding at all — its path is fully
deterministic from the boss's own project_dir, not something to search for:
`utils/helpers.py`'s `project_hash()` does
`sha256(abspath(project_dir).encode()).hexdigest()[:16]`, and
`utils/msg_store_.py`'s `_get_storage_dir()` combines that with the
project_dir's basename (non `[A-Za-z0-9_-]` chars replaced with `_`) to get
`~/.micro-cc/projects/{safe_basename}_{hash}/`. The tracker lives at
`tracked_subagents.json` inside that directory. e.g. for boss project_dir
`/Users/me/Desktop` that's
`~/.micro-cc/projects/Desktop_<hash>/tracked_subagents.json` —
compute it (or just `ls ~/.micro-cc/projects/` and match the readable
prefix) instead of grepping/finding for it.
