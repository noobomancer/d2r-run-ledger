# Run Ledger

A run tracker for **Diablo II: Resurrected** that times your runs automatically
and counts herald kills and drops, shown on a local web page.

It **never touches the game process**. There is no memory reading, no injection
and no hooks into D2R. It reads the Windows TCP connection table, the same data
`netstat` shows. D2R opens one connection to a game server when you join a game
and closes it when you leave, so each of those connections is a run.

## Run it

Needs Windows and Python 3 (standard library only, no `pip install`).

```
start.cmd                 # starts the tracker and opens http://127.0.0.1:8777
py tracker.py --hotkey F10 --port 8777 --host 127.0.0.1
```

Close the tracker window to stop tracking. History is saved to `data/history.json`.

## What it tracks

- **Runs**, detected automatically: live timer, average, median, fastest,
  runs per hour, lobby time between runs.
- **Heralds**: press **F9** in game for +1 (a beep confirms it), or use the
  − / + buttons per run on the page.
- **Drops**, logged on the page: suggestions for every set, unique and quest
  item (from d2db.net), plus one-click buttons for high runes and the uber keys.
  The drop list includes a summary table of repeated drops.
- **Keys**: Terror, Hate and Destruction counts, and keys per hour.
- **Sessions**: *Stop session* freezes the stats, *New session* starts a fresh
  count, and earlier sessions are kept in history.

## How runs are detected

Derived from ~70 logged games on European servers:

| Connection | Meaning |
|---|---|
| `35.228.x`, `34.88.x`, `37.244.50.x` on port 443 | game server, one per game |
| `137.221.x` | Blizzard lobby and matchmaking; reconnects even mid-game (ignored) |
| `66.40.x`, port 1119 | Battle.net services (ignored) |
| `34.117.x`, `3.x` | background service and CDN (ignored) |
| anything under 5 seconds | server ping probes at game launch (ignored) |

A connection that overlaps a run already in progress is ignored. If a
mid-game disconnect ever splits one game into two runs, use **Merge ↑**.
If you play in another region and runs aren't detected, the **Diagnostics**
panel on the page shows which connections the tracker sees.

## Limits

- The global hotkey takes over F9 while the tracker runs, so D2R won't receive
  it. Pick another key with `--hotkey`.
- A run already in progress when the tracker starts is marked `≥` and left out
  of the stats, because its real start time is unknown.
