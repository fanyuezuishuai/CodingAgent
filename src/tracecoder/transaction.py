"""Persistent, workspace-confined rollback for TraceCoder file-tool mutations."""

from __future__ import annotations

import difflib
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from tracecoder.identifiers import validate_runtime_id

MAX_SNAPSHOT_BYTES = 1_000_000


class TransactionError(RuntimeError):
    """A file transaction could not preserve or restore its contract."""


@dataclass(frozen=True, slots=True)
class _FileRecord:
    path: str
    kind: str
    backup: str | None


class WorkspaceTransaction:
    """Journal original file state before mutations and restore it on request.

    Only mutations performed by the bounded file tools are covered. Commands may
    change arbitrary state and are deliberately outside this transaction.
    """

    def __init__(self, workspace: Path, transaction_id: str) -> None:
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be a directory")
        self.id = validate_runtime_id(transaction_id, label="transaction_id")
        self.directory = self.workspace / ".tracecoder" / "transactions" / transaction_id
        self.manifest_path = self.directory / "manifest.json"
        self.latest_path = self.directory.parent / "latest"
        self._state = "not_required"
        self._files: dict[str, _FileRecord] = {}
        self._directories: list[str] = []
        if self.manifest_path.is_file():
            self._load_manifest()

    @classmethod
    def load(cls, workspace: Path, transaction_id: str) -> WorkspaceTransaction:
        """Open an existing transaction, rejecting unknown identifiers."""

        transaction = cls(workspace, transaction_id)
        if not transaction.manifest_path.is_file():
            raise TransactionError(f"transaction does not exist: {transaction_id}")
        return transaction

    @property
    def state(self) -> str:
        return self._state

    @property
    def rollback_available(self) -> bool:
        return self._state == "pending" and bool(self._files or self._directories)

    def prepare_file(self, target: Path) -> None:
        """Persist the original bytes or record that a file did not exist."""

        relative, safe_target = self._safe_target(target)
        if relative in self._files:
            return
        was_pending = self._state == "pending"
        if safe_target.exists() or safe_target.is_symlink():
            if safe_target.is_symlink() or not safe_target.is_file():
                raise TransactionError(f"transaction target must be a regular file: {relative}")
            original = safe_target.read_bytes()
            if len(original) > MAX_SNAPSHOT_BYTES:
                raise TransactionError(
                    f"transaction snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes: {relative}"
                )
            backup_name = f"{len(self._files) + 1:06d}.bin"
            backup_path = self.directory / "backups" / backup_name
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(backup_path, original)
            record = _FileRecord(relative, "modified", backup_name)
        else:
            record = _FileRecord(relative, "created", None)
        self._claim_latest()
        self._require_pending()
        try:
            self._files[relative] = record
            self._save_manifest()
        except OSError:
            self._files.pop(relative, None)
            if not was_pending:
                self._state = "not_required"
                self._clear_latest()
            raise

    def prepare_directory(self, target: Path) -> None:
        """Record one directory that the file tool is about to create."""

        relative, safe_target = self._safe_target(target)
        if safe_target.exists() or safe_target.is_symlink():
            raise TransactionError(f"directory already exists: {relative}")
        if relative in self._directories:
            return
        was_pending = self._state == "pending"
        self._claim_latest()
        self._require_pending()
        try:
            self._directories.append(relative)
            self._save_manifest()
        except OSError:
            self._directories.remove(relative)
            if not was_pending:
                self._state = "not_required"
                self._clear_latest()
            raise

    def file_changes(self) -> list[dict[str, object]]:
        """Return net file changes and deterministic unified diffs."""

        changes: list[dict[str, object]] = []
        for relative, record in self._files.items():
            target = self._manifest_target(relative)
            original = b"" if record.kind == "created" else self._read_backup(record)
            current = target.read_bytes() if target.is_file() and not target.is_symlink() else b""
            change: dict[str, object] = {
                "path": relative,
                "kind": record.kind,
                "net_changed": original != current,
            }
            try:
                before = _normalize_newlines(original.decode("utf-8"))
                after = _normalize_newlines(current.decode("utf-8"))
            except UnicodeDecodeError:
                change["diff"] = None
                change["diff_unavailable_reason"] = "non_utf8_content"
            else:
                from_name = "/dev/null" if record.kind == "created" else f"a/{relative}"
                change["diff"] = "".join(
                    difflib.unified_diff(
                        before.splitlines(keepends=True),
                        after.splitlines(keepends=True),
                        fromfile=from_name,
                        tofile=f"b/{relative}",
                        lineterm="\n",
                    )
                )
            changes.append(change)
        return changes

    def accept(self) -> dict[str, object]:
        """Keep current files and make rollback unavailable."""

        if self._state == "accepted":
            return self._outcome()
        if self._state == "rolled_back":
            raise TransactionError("transaction was already rolled back")
        if self._state == "not_required":
            raise TransactionError("transaction has no file-tool mutations")
        self._state = "accepted"
        self._save_manifest()
        self._clear_latest()
        self._remove_backups()
        return self._outcome()

    def rollback(self) -> dict[str, object]:
        """Restore snapshotted files and remove paths created by file tools."""

        if self._state == "rolled_back":
            return self._outcome()
        if self._state == "accepted":
            raise TransactionError("transaction was already accepted")
        if self._state == "not_required":
            raise TransactionError("transaction has no file-tool mutations")
        latest = self._read_latest()
        if latest is not None and latest != self.id:
            raise TransactionError("only the latest pending transaction can be rolled back")

        # Validate every backup and target parent before making any restoration.
        backups: dict[str, bytes] = {}
        for record in self._files.values():
            target = self._manifest_target(record.path)
            if not target.parent.is_dir():
                raise TransactionError(f"rollback parent is unavailable: {record.path}")
            if record.kind == "modified":
                backups[record.path] = self._read_backup(record)
            if target.exists() and target.is_dir():
                raise TransactionError(f"rollback target became a directory: {record.path}")
        for relative in self._directories:
            self._manifest_target(relative)
        self._validate_created_directory_contents()

        restored: list[str] = []
        removed: list[str] = []
        for record in self._files.values():
            target = self._manifest_target(record.path)
            if record.kind == "modified":
                _atomic_write_bytes(target, backups[record.path])
                restored.append(record.path)
            else:
                target.unlink(missing_ok=True)
                removed.append(record.path)
        for relative in sorted(self._directories, key=lambda value: value.count("/"), reverse=True):
            target = self._manifest_target(relative)
            try:
                target.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise TransactionError(f"created directory is not empty: {relative}") from exc
            else:
                removed.append(relative)

        self._state = "rolled_back"
        self._save_manifest()
        self._clear_latest()
        self._remove_backups()
        return {**self._outcome(), "restored": restored, "removed": removed}

    def _require_pending(self) -> None:
        if self._state in {"accepted", "rolled_back"}:
            raise TransactionError(f"transaction is already {self._state}")
        self._state = "pending"

    def _claim_latest(self) -> None:
        latest = self._read_latest()
        if latest is not None and latest != self.id:
            previous = WorkspaceTransaction.load(self.workspace, latest)
            if previous.state == "pending":
                previous.accept()
        self.latest_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(self.latest_path, (self.id + "\n").encode("ascii"))

    def _read_latest(self) -> str | None:
        try:
            latest = self.latest_path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise TransactionError("latest transaction pointer is unreadable") from exc
        try:
            validate_runtime_id(latest, label="latest transaction pointer")
        except ValueError as exc:
            raise TransactionError("latest transaction pointer is invalid") from exc
        return latest

    def _clear_latest(self) -> None:
        if self._read_latest() == self.id:
            self.latest_path.unlink(missing_ok=True)

    def _safe_target(self, target: Path) -> tuple[str, Path]:
        candidate = target if target.is_absolute() else self.workspace / target
        try:
            relative_path = candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise TransactionError("transaction target is outside the workspace") from exc
        relative = relative_path.as_posix()
        safe_target = self._manifest_target(relative)
        return relative, safe_target

    def _manifest_target(self, relative: str) -> Path:
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts or path.parts[0] == ".tracecoder":
            raise TransactionError(f"unsafe transaction path: {relative}")
        candidate = self.workspace.joinpath(*path.parts)
        try:
            existing_ancestor = candidate.parent
            while not existing_ancestor.exists() and existing_ancestor != self.workspace:
                existing_ancestor = existing_ancestor.parent
            resolved_ancestor = existing_ancestor.resolve(strict=True)
            resolved_ancestor.relative_to(self.workspace)
        except (FileNotFoundError, ValueError) as exc:
            raise TransactionError(f"unsafe transaction parent: {relative}") from exc
        return candidate

    def _read_backup(self, record: _FileRecord) -> bytes:
        if record.backup is None or PurePosixPath(record.backup).name != record.backup:
            raise TransactionError(f"invalid backup reference: {record.path}")
        backup = self.directory / "backups" / record.backup
        try:
            payload = backup.read_bytes()
        except OSError as exc:
            raise TransactionError(f"transaction backup is unavailable: {record.path}") from exc
        if len(payload) > MAX_SNAPSHOT_BYTES:
            raise TransactionError(f"transaction backup is too large: {record.path}")
        return payload

    def _validate_created_directory_contents(self) -> None:
        """Refuse partial rollback when commands left untracked generated artifacts."""

        tracked = set(self._directories)
        tracked.update(record.path for record in self._files.values() if record.kind == "created")
        for relative in self._directories:
            root = self._manifest_target(relative)
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                raise TransactionError(f"created directory changed type: {relative}")
            for directory, directory_names, file_names in os.walk(root, followlinks=False):
                for name in [*directory_names, *file_names]:
                    child = Path(directory) / name
                    child_relative = child.relative_to(self.workspace).as_posix()
                    if child_relative not in tracked:
                        raise TransactionError(
                            "rollback found an untracked command artifact in a created directory: "
                            f"{child_relative}"
                        )

    def _outcome(self) -> dict[str, object]:
        return {
            "transaction_id": self.id,
            "state": self._state,
            "rollback_available": self.rollback_available,
        }

    def _save_manifest(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "transaction_id": self.id,
            "state": self._state,
            "files": [
                {"path": item.path, "kind": item.kind, "backup": item.backup}
                for item in self._files.values()
            ],
            "directories": list(self._directories),
        }
        _atomic_write_bytes(
            self.manifest_path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def _load_manifest(self) -> None:
        try:
            payload: Any = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or payload.get("transaction_id") != self.id:
                raise ValueError
            state = payload["state"]
            if state not in {"pending", "accepted", "rolled_back"}:
                raise ValueError
            files = payload["files"]
            directories = payload["directories"]
            if not isinstance(files, list) or not isinstance(directories, list):
                raise ValueError
            loaded_files: dict[str, _FileRecord] = {}
            for item in files:
                if not isinstance(item, dict):
                    raise ValueError
                backup = item.get("backup")
                if backup is not None and not isinstance(backup, str):
                    raise ValueError
                record = _FileRecord(str(item["path"]), str(item["kind"]), backup)
                if record.kind not in {"created", "modified"}:
                    raise ValueError
                if (record.kind == "created" and record.backup is not None) or (
                    record.kind == "modified" and record.backup is None
                ):
                    raise ValueError
                self._manifest_target(record.path)
                loaded_files[record.path] = record
            loaded_directories = [str(item) for item in directories]
            for relative in loaded_directories:
                self._manifest_target(relative)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TransactionError(f"invalid transaction manifest: {self.id}") from exc
        self._state = str(state)
        self._files = loaded_files
        self._directories = loaded_directories

    def _remove_backups(self) -> None:
        backups = self.directory / "backups"
        for record in self._files.values():
            if record.backup:
                with suppress(OSError):
                    (backups / record.backup).unlink(missing_ok=True)
        with suppress(OSError):
            backups.rmdir()


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")
