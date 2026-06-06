<div align="center">

# ⏪ Rewind

### An undo button for AI coding agents.

Rewind watches your repository, automatically creates checkpoints whenever an AI agent starts making changes, and lets you instantly roll back to any point in the session. No commits, no branch switching, no lost work—just one command to get back to the last good state.

[![PyPI version](https://img.shields.io/pypi/v/rewind-ai.svg)](https://pypi.org/project/rewind-ai/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

---

# Your AI coding agent just destroyed 14 files.

Get back to the last good state in one command.

AI coding agents are incredible.

Until they aren't.

You ask Claude Code to add authentication.

Ten minutes later:

- 14 files changed
- Half the changes are brilliant
- The other half are a disaster
- You have absolutely no idea where things went wrong

Now what?

```bash
git checkout .
```

Congratulations. You just deleted the 10 minutes of good work too.

Or you can spend the next hour manually undoing files.

Or ask the agent to undo itself and hope it doesn't make things worse.

There should be a better option.

**Rewind creates automatic checkpoints while your AI agent works, so you can jump back to any moment of the session instantly.**

```bash
rewind jump 2
```

Done.

Your git history stays clean.

Your branch stays untouched.

Only the bad changes disappear.

---

## Why Rewind Exists

Git was built for humans.

AI agents don't behave like humans.

Humans:

- Edit one file at a time
- Make deliberate changes
- Commit intentionally

AI agents:

- Touch dozens of files in seconds
- Refactor entire systems at once
- Occasionally decide working code needs "improvement"

When an AI agent goes off the rails, Git gives you two choices:

1. Keep everything
2. Lose everything

Rewind gives you a third option:

**Keep the good work. Remove the bad work.**

---

## See It In Action

You tell Claude Code:

> Add authentication to my application.

A few minutes later:

```bash
rewind list
```

```text
Session checkpoints  (5 total)

#  Time      Description                        Changes          Branch
4  1m ago    Modified middleware, routes         ~4 modified      main
3  4m ago    Modified auth, Added utils          ~2 +1 added      main
2  8m ago    Added auth module                   +3 added         main
1  12m ago   Modified package.json               ~1 modified      main
0  14m ago   Session start                       +1 added         main
```

Something broke after checkpoint 2.

Inspect exactly what changed:

```bash
rewind diff 3
```

Compare any two checkpoints directly:

```bash
rewind diff 2 4
```

Restore the last known good state:

```bash
rewind jump 2
```

Or just undo the last thing the agent did:

```bash
rewind undo
```

Your commits remain untouched.

Your branch remains untouched.

Your work remains intact.

---

## The Agent Wrecked One File. Not All of Them.

Sometimes you don't need to roll back everything.

The agent did brilliant work on four files and completely broke one.

```bash
rewind undo src/auth.py
```

Rewind finds the last checkpoint where `src/auth.py` had different content and restores just that file. The rest of your working tree is untouched.

Or restore any file from any specific checkpoint:

```bash
rewind checkout 2 src/auth.py
```

Five files changed. One file fixed. Done.

---

## Let Your Tests Decide

Don't want to babysit the agent at all?

```bash
rewind guard "pytest tests/"
```

Rewind watches for write bursts, creates a checkpoint, runs your test command, and automatically rolls back if the tests fail.

```text
rewind  checkpoint #3 — +2 added, ~3 modified — running guard...
✗ Guard failed! Rolling back to checkpoint #2...
Working tree restored. Your git history is unchanged.
```

The agent breaks your build. Rewind fixes it. You notice nothing.

Works with any test runner:

```bash
rewind guard "npm test"
rewind guard "cargo test"
rewind guard "make test"
```

---

## Undo the Undo

Changed your mind after a restore?

Every time you run `rewind jump` or `rewind undo`, Rewind automatically captures a pre-restore backup of your working tree before touching anything.

```text
Safety backup saved as checkpoint #7 — run rewind jump 7 to undo this restore.
```

Nothing is ever permanently lost.

---

## Installation

```bash
pip install rewind-ai
```

Start the watcher:

```bash
rewind watch &
```

That's it.

Rewind now silently watches your repository and automatically creates checkpoints whenever an AI agent starts making significant changes.

No setup.

No configuration.

No cloud account.

---

## How It Works

Rewind monitors filesystem activity and detects write bursts—the characteristic pattern of AI coding agents modifying multiple files in rapid succession.

When a burst is detected, Rewind creates a lightweight checkpoint of your working tree.

These checkpoints:

- Don't create commits
- Don't clutter your git history
- Don't touch your branch
- Don't require any action from you

They're simply there when you need them.

**Under the hood:** each checkpoint is a shadow git commit stored under `refs/rewind/TIMESTAMP`—invisible to `git log`, `git status`, and `git push`. Checkpoints are deduplicated by tree hash, so identical states are never stored twice. A typical session of 50 checkpoints uses 2–5 MB.

---

## Works With Every AI Coding Agent

Because Rewind watches filesystem activity rather than integrating with specific tools, it works with virtually anything:

- Claude Code
- Cursor
- GitHub Copilot
- Aider
- OpenHands
- Devin
- Gemini CLI
- Any custom coding agent

If it writes files, Rewind can protect you from it.

---

## Agent Self-Modification Detection

AI agents sometimes modify their own instruction files.

When Rewind detects a write burst that touches `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, or similar files, it immediately creates a checkpoint and prints a warning:

```text
⚠  rewind: CLAUDE.md was modified by an active write burst.
   AI agents modifying their own instruction files is a known risk.
   Checkpoint #7 captured this change.
   Run rewind diff 7 to review.
```

---

## Commands

### Start Watching

```bash
rewind watch
```

### Watch and Auto-Rollback on Test Failure

```bash
rewind guard "pytest tests/"
```

### Stop Watching

```bash
rewind stop
```

### Check Status

```bash
rewind status
```

### Create Manual Checkpoint

```bash
rewind snap
rewind snap -m "before the big refactor"
```

### View Checkpoints

```bash
rewind list                  # checkpoints on the current branch
rewind list --all-branches   # checkpoints from all branches
rewind list -n 50            # show more history
```

### Inspect Changes

```bash
rewind diff 4          # what changed at checkpoint 4
rewind diff 2 5        # diff between checkpoints 2 and 5
```

### Restore Previous State

```bash
rewind jump 4          # restore entire working tree to checkpoint 4
rewind undo            # restore to the checkpoint before the last one
```

### Undo Changes to a Single File

```bash
rewind undo src/auth.py          # restore this file to its previous version
rewind checkout 2 src/auth.py    # restore this file from a specific checkpoint
```

### Create Branch From Checkpoint

```bash
rewind branch 4
```

### Remove Checkpoints

```bash
rewind clear           # delete all checkpoints for this session
rewind prune           # remove checkpoints older than 7 days (keeps last 200)
rewind prune --max-age 3 --max-count 50   # custom limits
```

---

## Why Not Just Commit More Often?

Frequent commits help.

But they don't solve this problem.

- Commits clutter history with half-finished work
- You still can't restore to moments between commits
- You have to remember to create them
- They weren't designed for autonomous agents modifying dozens of files at once

Rewind is automatic.

It works while you're thinking about your code—not your version control strategy.

---

## Privacy

Everything runs locally.

- No telemetry
- No analytics
- No cloud sync
- No network requests

Your code never leaves your machine.

---

## The Future of AI Coding Needs a Better Undo Button

AI coding agents are becoming more powerful every month.

They're writing more code, touching more files, and making bigger changes than ever before.

The better they get, the more important safe rollback becomes.

Rewind isn't a replacement for Git.

It's the missing layer between "everything is fine" and "what just happened to my repository?"

---

## Contributing

```bash
git clone https://github.com/dakshgulecha/rewind-ai
cd rewind-ai
pip install -e ".[dev]"
pytest
```

Issues and pull requests are welcome.

---

## License

MIT

---

<div align="center">

**If Rewind saves you even once, consider giving the project a star.**

⭐ It helps more developers discover it before their AI agent decides to refactor the entire codebase.

</div>
