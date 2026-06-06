from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import git
from rich.console import Console

console = Console()

REWIND_REF_PREFIX = "refs/rewind/"
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_CHECKPOINTS = 200


@dataclass
class Checkpoint:
    index: int
    sha: str
    tree_sha: str
    timestamp: float
    label: str
    files_changed: list[str]
    files_added: list[str]
    files_deleted: list[str]
    trigger: str
    agent_hint: str = ""
    branch: str = ""

    @property
    def age_str(self) -> str:
        delta = time.time() - self.timestamp
        if delta < 60:
            return f"{int(delta)}s ago"
        elif delta < 3600:
            return f"{int(delta / 60)}m ago"
        elif delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"

    @property
    def change_summary(self) -> str:
        parts = []
        if self.files_changed:
            parts.append(f"~{len(self.files_changed)} modified")
        if self.files_added:
            parts.append(f"+{len(self.files_added)} added")
        if self.files_deleted:
            parts.append(f"-{len(self.files_deleted)} deleted")
        return ", ".join(parts) if parts else "no changes"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Checkpoint":
        return cls(
            index=d["index"],
            sha=d["sha"],
            tree_sha=d["tree_sha"],
            timestamp=d["timestamp"],
            label=d["label"],
            files_changed=d["files_changed"],
            files_added=d["files_added"],
            files_deleted=d["files_deleted"],
            trigger=d["trigger"],
            agent_hint=d.get("agent_hint", ""),
            branch=d.get("branch", ""),
        )


class CheckpointStore:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        try:
            self.repo = git.Repo(repo_path, search_parent_directories=True)
        except git.InvalidGitRepositoryError:
            raise ValueError(f"{repo_path} is not inside a git repository")
        self.git_dir = Path(self.repo.git_dir)
        self.meta_path = self.git_dir / "rewind_meta.json"
        self._checkpoints: list[Checkpoint] = []
        self._load_meta()

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _load_meta(self) -> None:
        if self.meta_path.exists():
            try:
                data = json.loads(self.meta_path.read_text())
                self._checkpoints = [Checkpoint.from_dict(c) for c in data.get("checkpoints", [])]
                return
            except Exception:
                pass
        # JSON missing or corrupt: rebuild from git refs
        self._rebuild_from_refs()

    def _save_meta(self) -> None:
        """Atomic write to prevent corruption from concurrent processes."""
        data = {"checkpoints": [c.to_dict() for c in self._checkpoints]}
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.meta_path)  # atomic on POSIX

    def _rebuild_from_refs(self) -> None:
        """Reconstruct checkpoint metadata from git refs after JSON loss."""
        try:
            refs_output = self._git(
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "--sort=refname",
                REWIND_REF_PREFIX,
            )
        except RuntimeError:
            return

        checkpoints: list[Checkpoint] = []
        for line in refs_output.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            ref_name, commit_sha = parts[0], parts[1]
            try:
                msg = self._git("log", "-1", "--format=%B", commit_sha)
                lines = msg.splitlines()
                if not lines or not lines[0].startswith("[rewind]"):
                    continue
                label = lines[0].removeprefix("[rewind]").strip()
                body = "\n".join(lines[1:])

                def _val(key: str, default: str = "") -> str:
                    m = re.search(rf"^{key}=(.*)$", body, re.MULTILINE)
                    return m.group(1).strip() if m else default

                timestamp = float(_val("timestamp") or time.time())
                agent_hint = _val("agent")
                trigger = _val("trigger", "burst")
                branch = _val("branch")
                tree_sha = self._git("log", "-1", "--format=%T", commit_sha)

                prev_tree = (
                    checkpoints[-1].tree_sha
                    if checkpoints
                    else self._head_tree()
                )
                modified, added, deleted = self._tree_diff_stats(prev_tree, tree_sha)

                cp = Checkpoint(
                    index=len(checkpoints),
                    sha=commit_sha,
                    tree_sha=tree_sha,
                    timestamp=timestamp,
                    label=label,
                    files_changed=modified,
                    files_added=added,
                    files_deleted=deleted,
                    trigger=trigger,
                    agent_hint=agent_hint,
                    branch=branch,
                )
                checkpoints.append(cp)
            except (RuntimeError, ValueError):
                continue

        if checkpoints:
            self._checkpoints = checkpoints
            self._save_meta()

    # ── Git helpers ───────────────────────────────────────────────────────────

    def _git(self, *args: str) -> str:
        """Run a git command with exponential backoff on index.lock contention."""
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "rewind")
        env.setdefault("GIT_AUTHOR_EMAIL", "rewind@local")
        env.setdefault("GIT_COMMITTER_NAME", "rewind")
        env.setdefault("GIT_COMMITTER_EMAIL", "rewind@local")
        delays = [0.05, 0.10, 0.25]
        for attempt, delay in enumerate(delays + [None]):
            result = subprocess.run(
                ["git", "-C", str(self.repo.working_dir), *args],
                capture_output=True, text=True, env=env,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            err = result.stderr.strip() or result.stdout.strip()
            if "index.lock" in err and delay is not None:
                time.sleep(delay)
                continue
            raise RuntimeError(err)
        raise RuntimeError("git command failed after retries")

    def _git_unstage(self) -> None:
        """Unstage everything. Works on both empty repos and normal repos."""
        try:
            self._git("reset")
        except RuntimeError:
            pass  # nothing staged, harmless

    def _head_tree(self) -> str:
        """Return current HEAD's tree SHA, or the empty tree SHA if no commits."""
        try:
            return self._git("rev-parse", "HEAD^{tree}")
        except RuntimeError:
            return EMPTY_TREE_SHA

    def _tree_diff_stats(
        self, from_tree: str, to_tree: str
    ) -> tuple[list[str], list[str], list[str]]:
        """
        Compare two tree SHAs and return (modified, added, deleted) file lists.
        Uses git diff-tree so it captures shadow-only file changes that
        git diff --cached would miss.
        """
        try:
            diff_output = self._git(
                "diff-tree", "-r", "--name-status", "--no-commit-id",
                from_tree, to_tree,
            )
        except RuntimeError:
            return [], [], []

        modified, added, deleted = [], [], []
        for line in diff_output.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) < 2:
                continue
            status = parts[0][0]
            path_part = parts[1]
            if status == "M":
                modified.append(path_part)
            elif status == "A":
                added.append(path_part)
            elif status == "D":
                deleted.append(path_part)
            elif status == "R":
                # Rename: "old_name\tnew_name" — track new name as added
                new_path = path_part.rsplit("\t", 1)[-1]
                added.append(new_path)
        return modified, added, deleted

    # ── Agent detection ───────────────────────────────────────────────────────

    def _detect_agent(self) -> str:
        if os.getenv("CLAUDE_CODE_ENTRYPOINT"):
            return "Claude Code"
        work_dir = self.repo.working_dir or ""
        if os.path.exists(os.path.join(work_dir, ".cursor")):
            return "Cursor"
        if os.getenv("GITHUB_COPILOT_TOKEN") or os.getenv("COPILOT_WORKSPACE"):
            return "Copilot"
        try:
            import psutil
            procs = {p.name().lower() for p in psutil.process_iter(["name"])}
            if "cursor" in procs:
                return "Cursor"
            if any("copilot" in n for n in procs):
                return "Copilot"
        except Exception:
            pass
        return "Unknown agent"

    # ── Label generation ──────────────────────────────────────────────────────

    def _make_label(
        self,
        files_changed: list[str],
        files_added: list[str],
        files_deleted: list[str] | None = None,
    ) -> str:
        files_deleted = files_deleted or []
        all_files = files_changed + files_added + files_deleted
        if not all_files:
            return "No changes"

        def _top_dirs(files: list[str], n: int = 2) -> list[str]:
            dirs: dict[str, int] = {}
            for f in files:
                key = f.split("/")[0] if "/" in f else f
                dirs[key] = dirs.get(key, 0) + 1
            return [k for k, _ in sorted(dirs.items(), key=lambda x: -x[1])[:n]]

        parts = []
        if files_added:
            tops = _top_dirs(files_added)
            parts.append(f"Added {', '.join(tops)}")
        if files_changed:
            tops = _top_dirs(files_changed)
            parts.append(f"Modified {', '.join(tops)}")
        if files_deleted:
            tops = _top_dirs(files_deleted)
            parts.append(f"Deleted {', '.join(tops)}")

        label = "; ".join(parts[:2])
        remaining = len(all_files) - len(files_added[:2]) - len(files_changed[:2])
        if len(all_files) > 2:
            label += f" +{len(all_files) - 2} more" if len(all_files) > 2 else ""
        return label if label else "No changes"

    # ── Working tree state ────────────────────────────────────────────────────

    def _working_tree_files(self) -> set[str]:
        tracked = set(self._git("ls-files").splitlines())
        try:
            untracked = set(
                self._git("ls-files", "--others", "--exclude-standard").splitlines()
            )
        except RuntimeError:
            untracked = set()
        return tracked | untracked

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def checkpoints(self) -> list[Checkpoint]:
        return list(self._checkpoints)

    def create(self, trigger: str = "auto", label: str = "") -> Optional[Checkpoint]:
        """Snapshot the working tree. Returns None if nothing changed."""
        try:
            self._git("add", "-A")
            tree_sha = self._git("write-tree")

            # Dedup: skip if tree is identical to the last checkpoint
            if self._checkpoints and self._checkpoints[-1].tree_sha == tree_sha:
                self._git_unstage()
                return None

            # Determine the baseline tree for change detection
            prev_tree = (
                self._checkpoints[-1].tree_sha
                if self._checkpoints
                else self._head_tree()
            )

            # Use tree-to-tree diff: catches shadow-only changes that
            # git diff --cached misses (e.g. deleting an untracked file)
            modified, added, deleted = self._tree_diff_stats(prev_tree, tree_sha)
            if not (modified or added or deleted):
                self._git_unstage()
                return None

            timestamp = time.time()
            ref_name = f"{REWIND_REF_PREFIX}{int(timestamp * 1000)}"

            try:
                parent_sha = self._git("rev-parse", "HEAD")
                parent_args = ["-p", parent_sha]
            except RuntimeError:
                parent_args = []  # empty repo — no parent

            try:
                current_branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
            except RuntimeError:
                current_branch = ""

            agent = self._detect_agent()
            commit_label = label or self._make_label(modified, added, deleted)

            commit_sha = self._git(
                "commit-tree", tree_sha,
                *parent_args,
                "-m",
                f"[rewind] {commit_label}\n\n"
                f"timestamp={timestamp}\n"
                f"agent={agent}\n"
                f"trigger={trigger}\n"
                f"branch={current_branch}",
            )

            self._git("update-ref", ref_name, commit_sha)
            self._git_unstage()

        except RuntimeError as e:
            self._git_unstage()
            console.print(f"[red]rewind: checkpoint failed: {e}[/red]")
            return None

        cp = Checkpoint(
            index=len(self._checkpoints),
            sha=commit_sha,
            tree_sha=tree_sha,
            timestamp=timestamp,
            label=commit_label,
            files_changed=modified,
            files_added=added,
            files_deleted=deleted,
            trigger=trigger,
            agent_hint=agent,
            branch=current_branch,
        )
        self._checkpoints.append(cp)
        self._save_meta()
        return cp

    def restore(self, index: int, branch: bool = False) -> bool:
        """Restore working tree to checkpoint N. Saves a pre-restore backup first."""
        if index < 0 or index >= len(self._checkpoints):
            console.print(f"[red]No checkpoint #{index}[/red]")
            return False

        cp = self._checkpoints[index]

        if branch:
            # FIX: use git branch (no checkout) — non-destructive, works with untracked files
            branch_name = f"rewind/restore-{index}-{int(time.time())}"
            try:
                self._git("branch", branch_name, cp.sha)
                console.print(
                    f"[green]Created branch [bold]{branch_name}[/bold] "
                    f"at checkpoint #{index}  [dim](HEAD unchanged)[/dim][/green]"
                )
                return True
            except RuntimeError as e:
                console.print(f"[red]Failed to create branch: {e}[/red]")
                return False

        # Pre-restore safety backup: capture current state so the user can undo the undo
        safety_cp = self.create(
            trigger="pre-restore",
            label=f"[Pre-Restore Backup] before jump to #{index}",
        )

        try:
            checkpoint_files = set(
                self._git("ls-tree", "-r", "--name-only", cp.sha).splitlines()
            )
            current_files = self._working_tree_files()
            work_dir = Path(self.repo.working_dir)

            # Delete files that exist now but weren't in the checkpoint
            extra_files = current_files - checkpoint_files
            for fname in extra_files:
                fpath = work_dir / fname
                try:
                    fpath.unlink(missing_ok=True)
                except OSError:
                    pass

            # Clean up empty directories left behind
            for fname in sorted(extra_files, reverse=True):
                parent = (work_dir / fname).parent
                if parent == work_dir:
                    continue
                try:
                    if not any(parent.iterdir()):
                        parent.rmdir()
                except OSError:
                    pass

            # Restore all files from the checkpoint tree
            self._git("checkout", cp.sha, "--", ".")

            # Write agent self-correction context
            self._write_rollback_json(cp, safety_cp)

            console.print(
                f"[green]Restored to checkpoint #{index}: "
                f"[bold]{cp.label}[/bold][/green]\n"
                "[dim]Working tree restored. Your git history is unchanged.[/dim]"
            )
            if safety_cp:
                console.print(
                    f"[dim]Safety backup saved as checkpoint #{safety_cp.index} — "
                    f"run [bold]rewind jump {safety_cp.index}[/bold] to undo this restore.[/dim]"
                )
            return True

        except RuntimeError as e:
            console.print(f"[red]Failed to restore: {e}[/red]")
            return False

    def restore_file(self, index: int, file_path: str) -> bool:
        """Cherry-pick a single file from checkpoint N without touching the rest.

        If the file didn't exist at that checkpoint it is deleted, matching
        the snapshot exactly.
        """
        if index < 0 or index >= len(self._checkpoints):
            console.print(f"[red]No checkpoint #{index}[/red]")
            return False
        cp = self._checkpoints[index]
        try:
            ls = self._git("ls-tree", "-r", cp.sha, "--", file_path)
            if ls.strip():
                self._git("checkout", cp.sha, "--", file_path)
                console.print(
                    f"[green]Restored [bold]{file_path}[/bold] "
                    f"from checkpoint #{index}[/green]"
                )
            else:
                fpath = Path(self.repo.working_dir) / file_path
                fpath.unlink(missing_ok=True)
                console.print(
                    f"[green]Deleted [bold]{file_path}[/bold] "
                    f"(it did not exist at checkpoint #{index})[/green]"
                )
            return True
        except RuntimeError as e:
            console.print(f"[red]Failed to restore {file_path}: {e}[/red]")
            return False

    def find_undo_checkpoint_for_file(self, file_path: str) -> Optional[int]:
        """Return the index of the most recent checkpoint where *file_path*
        had different content from the current working-tree version.

        Used by ``rewind undo <file>`` to locate the right snapshot.
        Returns None when no earlier version exists.
        """
        try:
            current_blob = self._git(
                "ls-files", "--format=%(objectname)", "--", file_path
            ).strip()
            if not current_blob:
                full = Path(self.repo.working_dir) / file_path
                if full.exists():
                    current_blob = self._git("hash-object", str(full))
        except RuntimeError:
            current_blob = ""

        for cp in reversed(self._checkpoints):
            if cp.trigger == "pre-restore":
                continue
            try:
                ls = self._git("ls-tree", "-r", cp.sha, "--", file_path)
                cp_blob = ls.split()[2] if ls.strip() else ""
            except (RuntimeError, IndexError):
                cp_blob = ""

            if cp_blob != current_blob:
                return cp.index

        return None

    def _write_rollback_json(
        self, restored_to: Checkpoint, pre_restore: Optional[Checkpoint]
    ) -> None:
        """Write rollback context so AI agents can detect and learn from rejections."""
        try:
            data = {
                "rollback_at": time.time(),
                "restored_to_index": restored_to.index,
                "restored_to_label": restored_to.label,
                "reverted_files": {
                    "changed": pre_restore.files_changed if pre_restore else [],
                    "added": pre_restore.files_added if pre_restore else [],
                    "deleted": pre_restore.files_deleted if pre_restore else [],
                },
                "message": (
                    "A human reviewer rolled back your changes. "
                    "Review reverted_files and adjust your approach before retrying."
                ),
            }
            (self.git_dir / "rewind_latest_rollback.json").write_text(
                json.dumps(data, indent=2)
            )
        except Exception:
            pass

    def diff(self, index: int) -> str:
        """Diff at checkpoint N vs the one before it (or the empty tree for cp 0)."""
        if index < 0 or index >= len(self._checkpoints):
            return ""
        cp = self._checkpoints[index]
        try:
            prev_sha = (
                self._checkpoints[index - 1].sha if index > 0 else EMPTY_TREE_SHA
            )
            return self._git("diff", prev_sha, cp.sha)
        except RuntimeError:
            return ""

    def diff_between(self, from_idx: int, to_idx: int) -> str:
        """Diff between any two checkpoint indices."""
        cps = self._checkpoints
        if not (0 <= from_idx < len(cps) and 0 <= to_idx < len(cps)):
            return ""
        try:
            return self._git("diff", cps[from_idx].sha, cps[to_idx].sha)
        except RuntimeError:
            return ""

    def prune(self, max_age_days: float = 7.0, max_count: int = MAX_CHECKPOINTS) -> int:
        """
        Remove checkpoints that are older than max_age_days or exceed max_count.
        Returns the number of checkpoints removed.
        """
        cutoff = time.time() - max_age_days * 86400
        kept = [cp for cp in self._checkpoints if cp.timestamp >= cutoff]
        if len(kept) > max_count:
            kept = kept[-max_count:]

        kept_shas = {cp.sha for cp in kept}
        to_remove = [cp for cp in self._checkpoints if cp.sha not in kept_shas]
        if not to_remove:
            return 0

        for cp in to_remove:
            try:
                # Find the exact ref by SHA to avoid timestamp rounding issues
                refs = self._git(
                    "for-each-ref",
                    "--format=%(refname)",
                    f"--points-at={cp.sha}",
                    REWIND_REF_PREFIX,
                ).splitlines()
                for ref in refs:
                    self._git("update-ref", "-d", ref)
            except RuntimeError:
                pass

        self._checkpoints = kept
        for i, cp in enumerate(self._checkpoints):
            cp.index = i
        self._save_meta()
        return len(to_remove)

    def clear_session(self) -> None:
        try:
            refs = self._git(
                "for-each-ref", "--format=%(refname)", REWIND_REF_PREFIX
            ).splitlines()
            for ref in refs:
                self._git("update-ref", "-d", ref)
        except RuntimeError:
            pass
        self._checkpoints = []
        if self.meta_path.exists():
            self.meta_path.unlink()
        rollback = self.git_dir / "rewind_latest_rollback.json"
        if rollback.exists():
            rollback.unlink()
