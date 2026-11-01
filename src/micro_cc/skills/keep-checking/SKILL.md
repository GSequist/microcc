---
name: keep-checking
description: "Use when the user wants something checked or done repeatedly in the background without asking again — e.g. 'keep checking my emails and draft replies', 'watch my Desktop and upload new files', 'let me know when X happens', 'every morning do Y', 'stay on it while I'm at lunch', 'monitor this'."
---

# Keep checking

Use `monitor_`; it wakes you only when something happens. Never loop or sleep in bash.

## Pick one

| User wants | Use |
|---|---|
| React to **new things** (emails, files, records) | `action="poll"` |
| React to a **live stream** (a log, a running process) | `action="watch"` with `tail -f … \| grep --line-buffered …` |
| Act **at a time** ("every morning at 9", "in 20 min") | `action="schedule"` |

## Poll

```
monitor_(action="poll", spec="5m", description="new emails",
         mcp_tool="search_threads", mcp_args={"query": "is:unread in:inbox"}, key="id",
         prompt="For each new email: read it, draft a reply as a DRAFT. Never send.")

monitor_(action="poll", spec="2m", description="new Desktop files",
         command="ls -1 ~/Desktop",
         prompt="Upload each new file to <API> with curl, then tell the user.")
```

- Load the MCP tool with `search_tools` first and call it once yourself, so you know the args and output work.
- `key` = the field that identifies an item in JSON output (e.g. `id`). Leave it out for line output.
- Whatever is already there counts as seen; only new items wake you.
- `prompt` comes back with every wake. Write it so you can follow it without remembering this chat.
- 5m is a good default. Don't go under 2m unless the user asks.
- If the user says to keep it going after they close microcc, use `durable=True`. Default limit is 7 days (`max_age_days=0` = no limit).

## Safety

- Draft, don't act: never send, delete, pay or post for the user unless they asked for exactly that.
- If one wake brings lots of items, handle them briefly. For heavy work per item, spawn a subagent.

## Tell the user (plain words)

- What you're checking, how often, and what you'll do.
- microcc must stay open. The screen may go dark; the computer stays awake by itself.
  On a laptop: keep it plugged in and the lid open.
- To stop: tell you "stop checking", or press ctrl+b and select it.
- If a wake says it **stopped** (login expired, errors, time limit), tell them why and how to restart.
