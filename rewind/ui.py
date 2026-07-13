from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from rewind.checkpoint import Checkpoint

console = Console()


def render_checkpoint_list(checkpoints: list[Checkpoint], current_sha: str = "") -> Table:
    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        expand=True,
        border_style="dim",
    )
    table.add_column("#", style="bold white", width=4, justify="right")
    table.add_column("When", style="dim", width=10)
    table.add_column("Label", style="white", ratio=3)
    table.add_column("Changes", style="green", ratio=2)
    table.add_column("Trigger", style="dim", width=10)
    table.add_column("Branch", style="dim cyan", width=12)
    table.add_column("Agent", style="blue dim", width=14)

    for cp in reversed(checkpoints):
        is_current = cp.sha == current_sha
        is_pre_restore = cp.trigger == "pre-restore"
        is_initial = cp.trigger == "initial"

        index_cell = Text(
            f"► {cp.index}" if is_current else str(cp.index),
            style="bold green" if is_current else ("dim" if is_pre_restore else ""),
        )
        label_style = "dim italic" if is_pre_restore else ("dim" if is_initial else "")
        tags = f"  [{', '.join(cp.tags or [])}]" if cp.tags else ""
        session = f"  ({cp.session})" if cp.session else ""
        label_cell = Text(f"{cp.label}{tags}{session}", style="bold" if is_current else label_style)

        changes: list[Text] = []
        if cp.files_changed:
            changes.append(Text(f"~{len(cp.files_changed)}", style="yellow"))
        if cp.files_added:
            changes.append(Text(f"+{len(cp.files_added)}", style="green"))
        if cp.files_deleted:
            changes.append(Text(f"-{len(cp.files_deleted)}", style="red"))
        changes_cell = Text(" ").join(changes) if changes else Text("—", style="dim")

        trigger_style = {
            "auto": "dim",
            "burst": "cyan dim",
            "manual": "bold yellow",
            "initial": "dim",
            "pre-restore": "dim italic",
            "guard": "magenta dim",
            "session-start": "bold green",
            "session-end": "dim green",
        }.get(cp.trigger, "dim")

        branch_text = (cp.branch[:12] if cp.branch else "—")
        branch_style = "cyan" if cp.branch else "dim"

        table.add_row(
            index_cell,
            cp.age_str,
            label_cell,
            changes_cell,
            Text(cp.trigger, style=trigger_style),
            Text(branch_text, style=branch_style),
            Text(cp.agent_hint[:14] if cp.agent_hint else "—", style="blue dim"),
        )

    return table


def render_diff(diff_text: str) -> None:
    if not diff_text.strip():
        console.print("[dim]No changes at this checkpoint.[/dim]")
        return
    console.print(Syntax(diff_text, "diff", theme="monokai", line_numbers=False, word_wrap=False))


def render_status(store_path: Path, running: bool, pid: int, checkpoint_count: int) -> None:
    status = (
        Text("● WATCHING", style="bold green")
        if running
        else Text("○ STOPPED", style="dim red")
    )
    pid_str = f"pid {pid}" if running else "not running"
    panel_content = Text.assemble(
        status, "  ", Text(str(store_path), style="cyan"), "\n",
        Text(
            f"{checkpoint_count} checkpoint(s) this session  ·  {pid_str}",
            style="dim",
        ),
    )
    console.print(
        Panel(panel_content, title="[bold]rewind[/bold]", border_style="dim cyan", width=72)
    )


def render_restore_confirm(cp: Checkpoint) -> None:
    files = cp.files_changed + cp.files_added + cp.files_deleted
    if files:
        file_list = "\n".join(f"  {f}" for f in files[:20])
        if len(files) > 20:
            file_list += f"\n  … and {len(files) - 20} more"
        files_section = f"\n\n[white]Files at this checkpoint:[/white]\n{file_list}"
    else:
        files_section = ""

    console.print(
        Panel(
            f"[bold yellow]Restore to checkpoint #{cp.index}[/bold yellow]\n"
            f"[dim]{cp.age_str}  ·  {cp.change_summary}"
            + (f"  ·  branch: {cp.branch}" if cp.branch else "")
            + f"[/dim]{files_section}",
            border_style="yellow",
            width=72,
        )
    )
