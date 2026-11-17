"""Test canonical record identity, atomic persistence, locking and path validation."""

from pathlib import Path

import pytest

from dev_agent.errors import DevAgentError
from dev_agent.records import Store, new_id


def test_unique_predictable_ids() -> None:
    """Build distinct path-safe run IDs with visible task and project identity."""
    ids = {new_id("example", "TASK-123") for _ in range(100)}
    assert len(ids) == 100
    assert all("-example-TASK-123-" in run_id for run_id in ids)


def test_record_persistence_and_locking(tmp_path: Path) -> None:
    """Preserve canonical JSON and refuse concurrent acquisition of a run lock."""
    store = Store(tmp_path / "records")
    record = {"schema_version": 1, "run_id": "run-1", "outcome": "pending"}
    directory = store.create(record)
    assert store.load("run-1") == record
    record["outcome"] = "succeeded"
    store.save(record)
    assert store.list() == [record]
    assert not list(directory.glob("*.tmp"))
    with store.lock("run-1"):
        with pytest.raises(DevAgentError, match="active"):
            with store.lock("run-1"):
                pytest.fail("lock acquired twice")
    with store.lock("run-1"):
        pass
    with pytest.raises(FileExistsError):
        store.create(record)


@pytest.mark.parametrize("run_id", ["../bad", "/tmp/run", "bad/name", "..", "-bad", "bad\x00name"])
def test_record_path_traversal_refused(tmp_path: Path, run_id: str) -> None:
    """Prevent unsafe run IDs from resolving beyond canonical storage."""
    with pytest.raises(DevAgentError, match="Invalid run"):
        Store(tmp_path).load(run_id)


def test_record_symlinks_and_corruption_refused(tmp_path: Path) -> None:
    """Refuse symlinked directories or records and incompatible canonical data."""
    store = Store(tmp_path / "records")
    store.root.mkdir()
    target = tmp_path / "external"
    target.mkdir()
    (store.root / "symlink").symlink_to(target, target_is_directory=True)
    with pytest.raises(DevAgentError, match="symlink"):
        store.load("symlink")
    directory = store.create({"schema_version": 1, "run_id": "real"})
    filename = directory / "record.json"
    filename.write_text("broken")
    with pytest.raises(DevAgentError, match="Cannot read"):
        store.load("real")
    filename.unlink()
    filename.symlink_to(tmp_path / "missing")
    with pytest.raises(DevAgentError, match="symlink"):
        store.load("real")
