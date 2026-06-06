"""Tests for the checkpoint module."""
import time
from pathlib import Path
import pytest
import git

from rewind.checkpoint import CheckpointStore


@pytest.fixture
def repo(tmp_path):
    """Create a fresh git repo with an initial commit."""
    r = git.Repo.init(tmp_path)
    r.config_writer().set_value("user", "name", "Test").release()
    r.config_writer().set_value("user", "email", "test@test.com").release()
    (tmp_path / "README.md").write_text("# Test")
    r.index.add(["README.md"])
    r.index.commit("Initial commit")
    return tmp_path


def test_create_checkpoint(repo):
    store = CheckpointStore(repo)
    (repo / "main.py").write_text("print('hello')")
    cp = store.create(trigger="manual", label="Test checkpoint")
    assert cp is not None
    assert cp.index == 0
    assert cp.label == "Test checkpoint"
    assert "main.py" in cp.files_added


def test_deduplication(repo):
    """Creating a checkpoint twice without changes should return None."""
    store = CheckpointStore(repo)
    (repo / "app.py").write_text("x = 1")
    cp1 = store.create(trigger="manual")
    assert cp1 is not None
    # No changes since last checkpoint
    cp2 = store.create(trigger="manual")
    assert cp2 is None


def test_multiple_checkpoints(repo):
    store = CheckpointStore(repo)
    (repo / "a.py").write_text("a = 1")
    store.create(trigger="manual", label="Step 1")
    (repo / "b.py").write_text("b = 2")
    store.create(trigger="manual", label="Step 2")
    (repo / "a.py").write_text("a = 999")
    store.create(trigger="manual", label="Step 3")
    assert len(store.checkpoints) == 3


def test_restore(repo):
    store = CheckpointStore(repo)
    (repo / "app.py").write_text("version = 1")
    store.create(trigger="manual", label="v1")
    (repo / "app.py").write_text("version = 2\nimport bad_package")
    store.create(trigger="manual", label="v2 (bad)")
    # Restore to v1
    ok = store.restore(0)
    assert ok
    content = (repo / "app.py").read_text()
    assert "version = 1" in content
    assert "bad_package" not in content


def test_diff(repo):
    store = CheckpointStore(repo)
    (repo / "main.py").write_text("x = 1")
    store.create(trigger="manual", label="initial")
    (repo / "main.py").write_text("x = 1\ny = 2")
    store.create(trigger="manual", label="added y")
    diff = store.diff(1)
    assert "y = 2" in diff


def test_persistence(repo):
    """Checkpoints persist across store instances (via meta file)."""
    store1 = CheckpointStore(repo)
    (repo / "persist.py").write_text("x = 1")
    store1.create(trigger="manual", label="Persistent")
    # New store instance reads the same meta
    store2 = CheckpointStore(repo)
    assert len(store2.checkpoints) == 1
    assert store2.checkpoints[0].label == "Persistent"


def test_clear_session(repo):
    store = CheckpointStore(repo)
    (repo / "f.py").write_text("f")
    store.create(trigger="manual")
    assert len(store.checkpoints) == 1
    store.clear_session()
    store2 = CheckpointStore(repo)
    assert len(store2.checkpoints) == 0
