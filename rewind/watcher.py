from __future__ import annotations

import atexit
import os
import signal
import subprocess
import threading
from pathlib import Path
from threading import Event, Timer

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer
from rich.console import Console

from rewind.checkpoint import CheckpointStore
from rewind.config import RewindConfig, load_config

HIGH_VALUE_FILES = {
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "CLAUDE.md",
    "AGENTS.md",
    ".cursorrules",
    "copilot-instructions.md",   # BUG FIX: match on filename, not path
    "GEMINI.md",
}

# Files whose modification by an agent warrants a warning
SELF_MODIFICATION_FILES = {
    "CLAUDE.md", "AGENTS.md", ".cursorrules", "GEMINI.md",
    "copilot-instructions.md",
}

IGNORE_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll",
    ".log", ".tmp", ".temp", ".swp", ".swo",
}

# Filenames (not extensions) to ignore — covers dotfiles like .DS_Store
IGNORE_NAMES = {
    ".DS_Store", "Thumbs.db", ".localized",
}

IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".tox", "dist", "build", ".eggs",
    ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "target", "vendor",
}


def _is_ignored(path: str) -> bool:
    p = Path(path)
    for part in p.parts:
        if part in IGNORE_DIRS:
            return True
    # Check suffix (e.g. .pyc)
    if p.suffix.lower() in IGNORE_EXTENSIONS:
        return True
    # Check full filename for dotfiles and special names (e.g. .DS_Store)
    if p.name in IGNORE_NAMES:
        return True
    return False


class BurstDetector(FileSystemEventHandler):
    BURST_WINDOW = 3.0

    def __init__(
        self, store: CheckpointStore, quiet: bool = False,
        burst_window: float = BURST_WINDOW, ignored_paths: set[str] | None = None,
    ):
        super().__init__()
        self.store = store
        self.quiet = quiet
        self.burst_window = burst_window
        self.ignored_paths = {path.strip("/") for path in (ignored_paths or set())}
        self.suspended = Event()
        self._pending_files: set[str] = set()
        self._timer: Timer | None = None
        self._lock = threading.Lock()

    def _reset_timer(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = Timer(self.burst_window, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        from rich.console import Console
        cons = Console(stderr=True)

        with self._lock:
            pending = set(self._pending_files)
            self._pending_files.clear()
            self._timer = None

        cp = self.store.create(trigger="burst")
        if cp is None:
            return

        # Self-modification warning
        if not self.quiet:
            modified_instruction_files = [
                Path(f).name for f in pending
                if Path(f).name in SELF_MODIFICATION_FILES
            ]
            for fname in modified_instruction_files:
                cons.print(
                    f"\n[bold yellow]⚠  rewind:[/bold yellow] [bold]{fname}[/bold] "
                    f"was modified by an active write burst.\n"
                    f"   [yellow]AI agents modifying their own instruction files is a known risk.[/yellow]\n"
                    f"   Checkpoint #{cp.index} captured this change.\n"
                    f"   Run [bold]rewind diff {cp.index}[/bold] to review.\n"
                )

            branch_tag = f" [dim]({cp.branch})[/dim]" if cp.branch else ""
            cons.print(
                f"[dim cyan]rewind[/dim cyan] checkpoint #{cp.index}"
                f"{branch_tag} [dim]— {cp.change_summary}[/dim]"
            )

    def _record_event(self, path: str, high_value: bool = False) -> None:
        normalized = path.replace("\\", "/")
        if self.suspended.is_set() or _is_ignored(path) or any(
            normalized.endswith(f"/{ignored}") or f"/{ignored}/" in normalized
            for ignored in self.ignored_paths
        ):
            return
        with self._lock:
            self._pending_files.add(path)
        if high_value:
            with self._lock:
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
            self._flush()
        else:
            self._reset_timer()

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        filename = Path(event.src_path).name
        self._record_event(event.src_path, high_value=filename in HIGH_VALUE_FILES)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        filename = Path(event.src_path).name
        self._record_event(event.src_path, high_value=filename in HIGH_VALUE_FILES)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._record_event(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._record_event(event.dest_path)


class GuardedBurstDetector(BurstDetector):
    """
    BurstDetector that runs a test command after each burst checkpoint.
    Auto-rolls back to the last known-good checkpoint if the test fails.
    """

    def __init__(
        self, store: CheckpointStore, test_cmd: str, quiet: bool = False,
        burst_window: float = BurstDetector.BURST_WINDOW,
        ignored_paths: set[str] | None = None, timeout: float = 120.0,
    ):
        super().__init__(store, quiet, burst_window, ignored_paths)
        self.test_cmd = test_cmd
        self._last_good_index: int = -1
        self.timeout = timeout
        self._baseline_ready = False
        self._pending_checkpoint = None
        self._guard_event = Event()
        self._guard_thread = threading.Thread(target=self._guard_worker, daemon=True)
        self._guard_thread.start()

    def _flush(self) -> None:
        from rich.console import Console
        cons = Console(stderr=True)

        with self._lock:
            pending = set(self._pending_files)
            self._pending_files.clear()
            self._timer = None

        cp = self.store.create(trigger="burst")
        if cp is None:
            return

        if not self.quiet:
            cons.print(
                f"[dim cyan]rewind[/dim cyan] checkpoint #{cp.index} "
                f"[dim]— {cp.change_summary}[/dim] — running guard..."
            )
        self._pending_checkpoint = cp
        self._guard_event.set()

    def establish_baseline(self, checkpoint) -> None:
        """A guard never rolls back until the starting state has passed."""
        if self._run_guard(checkpoint):
            self._last_good_index = checkpoint.index
            self._baseline_ready = True
            if not self.quiet:
                Console(stderr=True).print("[green]✓ Guard baseline passed[/green]")
        else:
            Console(stderr=True).print(
                "[bold yellow]⚠ Guard baseline failed; automatic rollback is disabled "
                "until the starting state passes.[/bold yellow]"
            )

    def _run_guard(self, cp) -> bool:
        try:
            result = subprocess.run(
                self.test_cmd, shell=True, capture_output=self.quiet, text=True,
                timeout=self.timeout,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            Console(stderr=True).print(
                f"[bold red]✗ Guard timed out after {self.timeout:g}s[/bold red]"
            )
            return False

    def _guard_worker(self) -> None:
        from rich.console import Console
        cons = Console(stderr=True)
        while True:
            self._guard_event.wait()
            if self._pending_checkpoint is None:
                break
            cp = self._pending_checkpoint
            self._pending_checkpoint = None
            self._guard_event.clear()
            passed = self._run_guard(cp)

            # A new burst arrived while tests ran: discard this stale result and
            # immediately test the newest checkpoint instead.
            if self._guard_event.is_set():
                continue

            if passed:
                self._last_good_index = cp.index
                self._baseline_ready = True
                if not self.quiet:
                    cons.print(f"[green]✓ Guard passed[/green] after checkpoint #{cp.index}")
            elif self._baseline_ready and self._last_good_index >= 0:
                rollback_to = self._last_good_index
                cons.print(
                    f"[bold red]✗ Guard failed![/bold red] "
                    f"Rolling back to checkpoint #{rollback_to}..."
                )
                self.suspended.set()
                try:
                    self.store.restore(rollback_to)
                finally:
                    self.suspended.clear()
            else:
                cons.print("[yellow]Guard failed; no known-good baseline to restore.[/yellow]")

    def stop(self) -> None:
        self._pending_checkpoint = None
        self._guard_event.set()
        self._guard_thread.join(timeout=5)


class Watcher:
    def __init__(self, repo_path: Path, quiet: bool = False, interval: float = 1.0):
        self.repo_path = repo_path
        self.quiet = quiet
        self.interval = interval
        self.config: RewindConfig = load_config(repo_path)
        self._store = CheckpointStore(repo_path)
        self._pid_file = Path(self._store.git_dir) / "rewind.pid"

    def _write_pid(self) -> None:
        self._pid_file.write_text(str(os.getpid()))

    def _clear_pid(self) -> None:
        if self._pid_file.exists():
            self._pid_file.unlink()

    def _make_detector(self) -> BurstDetector:
        return BurstDetector(
            self._store, quiet=self.quiet,
            burst_window=self.config.burst_window_seconds,
            ignored_paths=self.config.ignore,
        )

    def start(self) -> None:
        from rich.console import Console
        cons = Console(stderr=True)

        self._write_pid()
        atexit.register(self._clear_pid)

        # Prune stale checkpoints on session start
        pruned = self._store.prune(
            max_age_days=self.config.max_age_days, max_count=self.config.max_count
        )
        if pruned and not self.quiet:
            cons.print(f"[dim cyan]rewind[/dim cyan] pruned {pruned} old checkpoint(s)")

        # Always mark the beginning of a watch session, including clean trees.
        initial_cp = self._store.create(
            trigger="initial", label="Initial Snap", force=True
        )
        if initial_cp and not self.quiet:
            cons.print(
                f"[dim cyan]rewind[/dim cyan] initial snapshot #{initial_cp.index} "
                f"[dim]— {initial_cp.change_summary}[/dim]"
            )

        handler = self._make_detector()
        if isinstance(handler, GuardedBurstDetector) and initial_cp:
            handler.establish_baseline(initial_cp)
        observer = Observer(timeout=self.interval)
        observer.schedule(handler, str(self.repo_path), recursive=True)
        observer.start()

        if not self.quiet:
            cons.print(
                f"[dim cyan]rewind[/dim cyan] watching [bold]{self.repo_path}[/bold] "
                f"[dim](pid {os.getpid()})[/dim]"
            )

        stop_event = Event()

        def _handle_signal(sig: int, frame: object) -> None:
            stop_event.set()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        try:
            stop_event.wait()
        finally:
            observer.stop()
            observer.join()
            if isinstance(handler, GuardedBurstDetector):
                handler.stop()
            self._clear_pid()

    @staticmethod
    def is_running(git_dir: Path) -> tuple[bool, int]:
        pid_file = git_dir / "rewind.pid"
        if not pid_file.exists():
            return False, 0
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return True, pid
        except (ValueError, ProcessLookupError, PermissionError):
            pid_file.unlink(missing_ok=True)
            return False, 0

    @staticmethod
    def stop(git_dir: Path) -> bool:
        running, pid = Watcher.is_running(git_dir)
        if not running:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except ProcessLookupError:
            return False


class GuardedWatcher(Watcher):
    def __init__(
        self,
        repo_path: Path,
        test_cmd: str,
        quiet: bool = False,
        interval: float = 1.0,
    ):
        super().__init__(repo_path, quiet, interval)
        self.test_cmd = test_cmd

    def _make_detector(self) -> BurstDetector:
        return GuardedBurstDetector(
            self._store, self.test_cmd, quiet=self.quiet,
            burst_window=self.config.burst_window_seconds,
            ignored_paths=self.config.ignore,
            timeout=self.config.guard_timeout_seconds,
        )
