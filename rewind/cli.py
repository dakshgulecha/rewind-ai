from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.prompt import Confirm

from rewind.checkpoint import CheckpointStore
from rewind.watcher import Watcher, GuardedWatcher

console = Console()


def _find_repo_root() -> Path:
    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            return path
    console.print("[red]Not inside a git repository.[/red]")
    sys.exit(1)


def _get_store() -> CheckpointStore:
    return CheckpointStore(_find_repo_root())


@click.group()
@click.version_option(package_name="rewind-ai")
def main() -> None:
    """Automatic session checkpoints for AI coding agents.

    \b
    Install once, never think about it again:
      pip install rewind-ai && rewind watch &

    \b
    When something goes wrong:
      rewind list          # see all checkpoints
      rewind diff 3        # see what changed at checkpoint 3
      rewind jump 3        # restore to checkpoint 3
      rewind undo          # shortcut: undo the last agent action
      rewind checkout 3 src/auth.py   # restore just one file
    """
    pass


# ── Watcher commands ──────────────────────────────────────────────────────────

@main.command()
@click.option("--quiet", "-q", is_flag=True)
@click.option("--interval", default=1.0, show_default=True)
def watch(quiet: bool, interval: float) -> None:
    """Start the background file watcher. Creates checkpoints automatically."""
    repo_root = _find_repo_root()
    store = CheckpointStore(repo_root)
    running, pid = Watcher.is_running(Path(store.git_dir))
    if running:
        console.print(f"[yellow]rewind is already watching this repo (pid {pid}).[/yellow]")
        return
    w = Watcher(repo_root, quiet=quiet, interval=interval)
    w.start()


@main.command()
@click.argument("test_cmd")
@click.option("--quiet", "-q", is_flag=True)
@click.option("--interval", default=1.0, show_default=True)
def guard(test_cmd: str, quiet: bool, interval: float) -> None:
    """Watch files and auto-rollback if TEST_CMD fails after a burst.

    \b
    Example:
      rewind guard "pytest tests/"
      rewind guard "npm test"
      rewind guard "cargo test"

    After each write burst, rewind runs TEST_CMD. If it exits non-zero,
    the working tree is automatically restored to the last good checkpoint.
    """
    repo_root = _find_repo_root()
    store = CheckpointStore(repo_root)
    running, pid = Watcher.is_running(Path(store.git_dir))
    if running:
        console.print(
            f"[red]rewind is already running (pid {pid}). "
            "Stop it first with [bold]rewind stop[/bold].[/red]"
        )
        return
    console.print(
        f"[dim cyan]rewind[/dim cyan] guard mode — "
        f"test command: [bold]{test_cmd}[/bold]"
    )
    w = GuardedWatcher(repo_root, test_cmd=test_cmd, quiet=quiet, interval=interval)
    w.start()


@main.command()
def stop() -> None:
    """Stop the background file watcher."""
    repo_root = _find_repo_root()
    store = CheckpointStore(repo_root)
    killed = Watcher.stop(Path(store.git_dir))
    if killed:
        console.print("[green]rewind watcher stopped.[/green]")
    else:
        console.print("[yellow]rewind is not running for this repo.[/yellow]")


@main.command()
def status() -> None:
    """Show watcher status and checkpoint summary."""
    from rewind.ui import render_status
    repo_root = _find_repo_root()
    store = CheckpointStore(repo_root)
    running, pid = Watcher.is_running(Path(store.git_dir))
    render_status(repo_root, running, pid, len(store.checkpoints))


# ── Inspection commands ───────────────────────────────────────────────────────

@main.command(name="list")
@click.option("--limit", "-n", default=20, show_default=True)
@click.option(
    "--branch", "-b", default=None,
    help="Filter to checkpoints from a specific branch (default: current branch).",
)
@click.option(
    "--all-branches", "-a", "all_branches", is_flag=True,
    help="Show checkpoints from all branches.",
)
def list_checkpoints(limit: int, branch: str | None, all_branches: bool) -> None:
    """List checkpoints for the current session."""
    from rewind.ui import render_checkpoint_list
    store = _get_store()
    cps = store.checkpoints

    if not all_branches:
        # Default: filter to current branch
        try:
            import subprocess as sp
            current_branch = sp.run(
                ["git", "-C", str(_find_repo_root()), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True,
            ).stdout.strip()
            filter_branch = branch or current_branch
        except Exception:
            filter_branch = branch

        if filter_branch:
            branch_cps = [cp for cp in cps if cp.branch == filter_branch or cp.branch == ""]
            if branch_cps:
                cps = branch_cps
            # If filtering would show nothing, fall back to all
            elif not branch:
                pass  # show all if filtering by current branch gives nothing

    cps = cps[-limit:]

    if not cps:
        console.print(
            "[dim]No checkpoints yet.\n\n"
            "Start the watcher with [bold]rewind watch[/bold] and let it run in the background.\n"
            "Checkpoints are created automatically as your AI agent writes code.[/dim]"
        )
        return

    table = render_checkpoint_list(cps)
    console.print(f"\n[bold]Session checkpoints[/bold]  [dim]({len(store.checkpoints)} total)[/dim]\n")
    console.print(table)
    console.print(
        "\n[dim]Restore: [bold]rewind jump N[/bold]   "
        "Diff: [bold]rewind diff N[/bold]   "
        "File: [bold]rewind checkout N path[/bold]   "
        "Undo: [bold]rewind undo[/bold][/dim]"
    )


@main.command()
@click.argument("indices", nargs=-1, type=int)
def diff(indices: tuple[int, ...]) -> None:
    """Show a diff at or between checkpoints.

    \b
    rewind diff        # diff at the most recent checkpoint
    rewind diff 3      # diff at checkpoint 3 vs checkpoint 2
    rewind diff 2 5    # diff between checkpoints 2 and 5
    """
    from rewind.ui import render_diff
    store = _get_store()
    cps = store.checkpoints
    if not cps:
        console.print("[dim]No checkpoints yet.[/dim]")
        return

    if len(indices) == 0:
        idx = cps[-1].index
        diff_text = store.diff(idx)
        cp = cps[-1]
        console.print(
            f"\n[bold]Checkpoint #{cp.index}[/bold]  "
            f"[dim]{cp.age_str}  ·  {cp.change_summary}[/dim]\n"
        )
        render_diff(diff_text)

    elif len(indices) == 1:
        idx = indices[0]
        if idx < 0 or idx >= len(cps):
            console.print(f"[red]No checkpoint #{idx}.[/red]")
            return
        diff_text = store.diff(idx)
        cp = cps[idx]
        console.print(
            f"\n[bold]Checkpoint #{cp.index}[/bold]  "
            f"[dim]{cp.age_str}  ·  {cp.change_summary}[/dim]\n"
        )
        render_diff(diff_text)

    elif len(indices) == 2:
        from_idx, to_idx = indices
        if from_idx < 0 or from_idx >= len(cps):
            console.print(f"[red]No checkpoint #{from_idx}.[/red]")
            return
        if to_idx < 0 or to_idx >= len(cps):
            console.print(f"[red]No checkpoint #{to_idx}.[/red]")
            return
        diff_text = store.diff_between(from_idx, to_idx)
        console.print(
            f"\n[bold]Diff: checkpoint #{from_idx} → #{to_idx}[/bold]  "
            f"[dim]{cps[from_idx].age_str} → {cps[to_idx].age_str}[/dim]\n"
        )
        render_diff(diff_text)

    else:
        console.print("[red]Usage: rewind diff [N] [M][/red]")


# ── Restore commands ──────────────────────────────────────────────────────────

@main.command()
@click.argument("index", type=int)
@click.option("--yes", "-y", is_flag=True)
def jump(index: int, yes: bool) -> None:
    """Restore working tree to checkpoint INDEX.

    \b
    Your git history is not modified. The working tree is reset to the
    state at the chosen checkpoint. A pre-restore backup is created
    automatically so you can undo this with: rewind jump <backup_index>
    """
    from rewind.ui import render_restore_confirm
    store = _get_store()
    cps = store.checkpoints
    if index < 0 or index >= len(cps):
        console.print(
            f"[red]No checkpoint #{index}. "
            "Run [bold]rewind list[/bold] to see available checkpoints.[/red]"
        )
        return
    render_restore_confirm(cps[index])
    if not yes:
        ok = Confirm.ask("[bold yellow]Restore to this checkpoint?[/bold yellow]", default=False)
        if not ok:
            console.print("[dim]Aborted.[/dim]")
            return
    store.restore(index, branch=False)


@main.command()
@click.argument("file_path", required=False, default=None)
@click.option("--yes", "-y", is_flag=True)
def undo(file_path: str | None, yes: bool) -> None:
    """Undo the last agent action — for the whole tree or a single file.

    \b
    rewind undo                   # restore entire working tree one checkpoint back
    rewind undo src/auth.py       # undo just this file to its previous version
    rewind undo src/auth.py -y    # skip confirmation prompt

    \b
    The file-specific form walks backwards through checkpoints, finds the most
    recent snapshot where the file had different content, and restores that
    version. Everything else in the working tree is untouched.

    A safety backup is created automatically before any full-tree undo.
    """
    from rewind.ui import render_restore_confirm
    store = _get_store()
    cps = store.checkpoints

    # ── File-specific undo ──────────────────────────────────────────────────
    if file_path:
        target_idx = store.find_undo_checkpoint_for_file(file_path)
        if target_idx is None:
            console.print(
                f"[yellow]No earlier version of [bold]{file_path}[/bold] "
                "found in checkpoints.[/yellow]"
            )
            return
        target_cp = cps[target_idx]
        console.print(
            f"[bold]Undo file:[/bold] restoring [bold]{file_path}[/bold] "
            f"from checkpoint #{target_idx}  [dim]({target_cp.age_str}  ·  {target_cp.label})[/dim]"
        )
        if not yes:
            ok = Confirm.ask(
                f"[bold yellow]Restore {file_path} from checkpoint #{target_idx}?[/bold yellow]",
                default=False,
            )
            if not ok:
                console.print("[dim]Aborted.[/dim]")
                return
        store.restore_file(target_idx, file_path)
        return

    # ── Full-tree undo ──────────────────────────────────────────────────────
    real_cps = [cp for cp in cps if cp.trigger != "pre-restore"]
    if len(real_cps) < 2:
        console.print(
            "[yellow]Nothing to undo — need at least 2 checkpoints.[/yellow]\n"
            "[dim]Use [bold]rewind snap[/bold] to create a checkpoint before running your agent.[/dim]"
        )
        return

    target = real_cps[-2]
    console.print(f"[bold]Undo: restoring to checkpoint #{target.index}[/bold]")
    render_restore_confirm(target)
    if not yes:
        ok = Confirm.ask("[bold yellow]Undo last agent action?[/bold yellow]", default=False)
        if not ok:
            console.print("[dim]Aborted.[/dim]")
            return
    store.restore(target.index, branch=False)


@main.command(name="checkout")
@click.argument("index", type=int)
@click.argument("file_path")
def checkout_file(index: int, file_path: str) -> None:
    """Restore a single FILE_PATH from checkpoint INDEX.

    \b
    If an agent touched 5 files and only 1 went wrong:
      rewind checkout 2 src/auth.py

    Everything else stays as-is. Only the specified file is restored.
    """
    store = _get_store()
    store.restore_file(index, file_path)


@main.command()
@click.argument("index", type=int)
def branch(index: int) -> None:
    """Create a new git branch at checkpoint INDEX without touching HEAD."""
    store = _get_store()
    store.restore(index, branch=True)


# ── Snapshot commands ─────────────────────────────────────────────────────────

@main.command()
@click.option("--label", "-m", default="")
def snap(label: str) -> None:
    """Manually create a checkpoint right now."""
    store = _get_store()
    cp = store.create(trigger="manual", label=label)
    if cp:
        console.print(
            f"[green]Checkpoint #{cp.index} created:[/green] "
            f"{cp.label}  [dim]({cp.change_summary})[/dim]"
        )
    else:
        console.print("[dim]Nothing changed since the last checkpoint.[/dim]")


@main.command()
@click.option("--yes", "-y", is_flag=True)
def clear(yes: bool) -> None:
    """Delete all checkpoints for the current session."""
    store = _get_store()
    if not store.checkpoints:
        console.print("[dim]No checkpoints to clear.[/dim]")
        return
    if not yes:
        ok = Confirm.ask(f"Delete all {len(store.checkpoints)} checkpoint(s)?", default=False)
        if not ok:
            console.print("[dim]Aborted.[/dim]")
            return
    store.clear_session()
    console.print("[green]All checkpoints cleared.[/green]")


@main.command()
@click.option("--max-age", default=7.0, show_default=True, help="Max age in days.")
@click.option("--max-count", default=200, show_default=True, help="Max number to keep.")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without removing it.")
def prune(max_age: float, max_count: int, dry_run: bool) -> None:
    """Remove old checkpoints to reclaim disk space.

    \b
    By default keeps the last 200 checkpoints and anything from the past 7 days.
    Run automatically by the watcher on startup.
    """
    store = _get_store()
    if dry_run:
        import time
        cutoff = time.time() - max_age * 86400
        to_remove = [
            cp for cp in store.checkpoints
            if cp.timestamp < cutoff
        ]
        kept = store.checkpoints[-max_count:]
        to_remove_ids = {id(cp) for cp in to_remove} | (
            {id(cp) for cp in store.checkpoints if cp not in kept}
        )
        would_remove = len(to_remove_ids)
        console.print(
            f"[dim]Would remove {would_remove} checkpoint(s) "
            f"(keeping {len(store.checkpoints) - would_remove}).[/dim]"
        )
        return
    removed = store.prune(max_age_days=max_age, max_count=max_count)
    if removed:
        console.print(f"[green]Pruned {removed} checkpoint(s).[/green]")
    else:
        console.print("[dim]Nothing to prune.[/dim]")


# ── Install ───────────────────────────────────────────────────────────────────

GLOBAL_HOOK_SCRIPT = """\
#!/bin/sh
if command -v rewind >/dev/null 2>&1; then
  GIT_DIR=$(git rev-parse --git-dir 2>/dev/null)
  if [ -n "$GIT_DIR" ]; then
    PID_FILE="$GIT_DIR/rewind.pid"
    if [ ! -f "$PID_FILE" ]; then
      rewind watch --quiet &
    fi
  fi
fi
"""


@main.command()
@click.option("--global", "global_", is_flag=True)
def install(global_: bool) -> None:
    """Install rewind as a git hook so it starts automatically.

    \b
    Local (current repo):  rewind install
    Global (all repos):    rewind install --global
    """
    if global_:
        hooks_dir = Path(
            subprocess.run(
                ["git", "config", "--global", "core.hooksPath"],
                capture_output=True, text=True,
            ).stdout.strip() or Path.home() / ".git-hooks"
        )
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path = hooks_dir / "post-checkout"
        hook_path.write_text(GLOBAL_HOOK_SCRIPT)
        hook_path.chmod(0o755)
        subprocess.run(["git", "config", "--global", "core.hooksPath", str(hooks_dir)])
        console.print(f"[green]Installed global hook at [bold]{hook_path}[/bold][/green]")
        console.print("[dim]rewind will auto-start whenever you enter a git repo.[/dim]")
    else:
        repo_root = _find_repo_root()
        hooks_dir = repo_root / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        hook_path = hooks_dir / "post-checkout"
        hook_path.write_text(GLOBAL_HOOK_SCRIPT)
        hook_path.chmod(0o755)
        console.print(f"[green]Installed hook at [bold]{hook_path}[/bold][/green]")
