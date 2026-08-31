"""Persistent file-tool transaction tests."""

from pathlib import Path

import pytest

from tracecoder.transaction import TransactionError, WorkspaceTransaction


def test_rollback_restores_modified_files_and_removes_created_paths(tmp_path: Path) -> None:
    original = b"original\r\nbytes\x00\n"
    existing = tmp_path / "existing.bin"
    existing.write_bytes(original)
    transaction = WorkspaceTransaction(tmp_path, "run-one")

    transaction.prepare_file(existing)
    existing.write_bytes(b"changed")
    created_directory = tmp_path / "generated"
    transaction.prepare_directory(created_directory)
    created_directory.mkdir()
    created = created_directory / "new.txt"
    transaction.prepare_file(created)
    created.write_text("new file\n", encoding="utf-8")

    outcome = transaction.rollback()

    assert outcome["state"] == "rolled_back"
    assert existing.read_bytes() == original
    assert not created.exists()
    assert not created_directory.exists()
    assert WorkspaceTransaction.load(tmp_path, "run-one").state == "rolled_back"


def test_accept_keeps_changes_and_makes_rollback_unavailable(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("before", encoding="utf-8")
    transaction = WorkspaceTransaction(tmp_path, "run-accept")
    transaction.prepare_file(target)
    target.write_text("after", encoding="utf-8")

    first = transaction.accept()
    second = transaction.accept()

    assert first["state"] == second["state"] == "accepted"
    assert target.read_text(encoding="utf-8") == "after"
    assert not transaction.rollback_available
    with pytest.raises(TransactionError, match="accepted"):
        transaction.rollback()


def test_transaction_reports_runtime_derived_unified_diff(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    transaction = WorkspaceTransaction(tmp_path, "run-diff")
    transaction.prepare_file(target)
    target.write_text("value = 2\n", encoding="utf-8")

    changes = transaction.file_changes()

    assert changes == [
        {
            "path": "app.py",
            "kind": "modified",
            "net_changed": True,
            "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n",
        }
    ]


def test_rollback_refuses_partial_cleanup_when_command_left_untracked_artifact(tmp_path: Path) -> None:
    transaction = WorkspaceTransaction(tmp_path, "run-command-artifact")
    generated = tmp_path / "generated"
    transaction.prepare_directory(generated)
    generated.mkdir()
    tracked = generated / "main.py"
    transaction.prepare_file(tracked)
    tracked.write_text("print('ok')\n", encoding="utf-8")
    untracked = generated / "command.cache"
    untracked.write_text("created by a command", encoding="utf-8")

    with pytest.raises(TransactionError, match="untracked command artifact"):
        transaction.rollback()

    assert tracked.is_file()
    assert untracked.is_file()
    assert transaction.state == "pending"


def test_newer_mutating_transaction_auto_accepts_previous_transaction(tmp_path: Path) -> None:
    first_file = tmp_path / "first.txt"
    second_file = tmp_path / "second.txt"
    first_file.write_text("one", encoding="utf-8")
    second_file.write_text("two", encoding="utf-8")
    first = WorkspaceTransaction(tmp_path, "first-run")
    first.prepare_file(first_file)
    first_file.write_text("changed one", encoding="utf-8")

    second = WorkspaceTransaction(tmp_path, "second-run")
    second.prepare_file(second_file)

    assert WorkspaceTransaction.load(tmp_path, "first-run").state == "accepted"
    assert first_file.read_text(encoding="utf-8") == "changed one"
    assert second.rollback_available


@pytest.mark.parametrize("transaction_id", ["../escape", "bad/name", "", "."])
def test_transaction_id_is_confined_to_runtime_directory(tmp_path: Path, transaction_id: str) -> None:
    with pytest.raises(ValueError, match="transaction_id"):
        WorkspaceTransaction(tmp_path, transaction_id)
