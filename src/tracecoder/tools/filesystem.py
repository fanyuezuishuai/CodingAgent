"""Bounded file tools confined to one canonical workspace."""

from __future__ import annotations

import fnmatch
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath

from tracecoder.domain import JSONValue, ToolResult
from tracecoder.transaction import TransactionError, WorkspaceTransaction

MAX_FILE_BYTES = 1_000_000
DEFAULT_MAX_ENTRIES = 200
DEFAULT_MAX_RESULTS = 100
RESERVED_TOP_LEVEL = {".env", ".git", ".tracecoder"}
_WINDOWS_INVALID_NAME_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class WorkspacePathError(ValueError):
    """A path failed a workspace-policy check."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkspacePolicy:
    """Resolve model-supplied paths to canonical, in-workspace paths."""

    def __init__(self, workspace: Path) -> None:
        try:
            root = workspace.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"Workspace does not exist: {workspace}") from exc
        if not root.is_dir():
            raise ValueError(f"Workspace is not a directory: {workspace}")
        self.root = root

    def resolve_existing(self, raw_path: str, *, expected: str | None = None) -> Path:
        """Return an existing canonical path after boundary and type checks."""

        relative = self._validate_relative(raw_path)
        try:
            target = (self.root / relative).resolve(strict=True)
        except FileNotFoundError as exc:
            raise WorkspacePathError("path_not_found", f"Path does not exist: {raw_path}") from exc
        self._require_inside(target)
        if expected == "file" and not target.is_file():
            raise WorkspacePathError("invalid_path_type", f"Expected a file: {raw_path}")
        if expected == "directory" and not target.is_dir():
            raise WorkspacePathError("invalid_path_type", f"Expected a directory: {raw_path}")
        return target

    def resolve_for_write(self, raw_path: str) -> Path:
        """Resolve an existing target or its existing canonical parent for creation."""

        relative = self._validate_relative(raw_path)
        candidate = self.root / relative
        if candidate.exists() or candidate.is_symlink():
            return self.resolve_existing(raw_path, expected="file")
        try:
            parent = candidate.parent.resolve(strict=True)
        except FileNotFoundError as exc:
            raise WorkspacePathError("path_not_found", f"Parent directory does not exist: {raw_path}") from exc
        self._require_inside(parent)
        if not parent.is_dir():
            raise WorkspacePathError("invalid_path_type", f"Parent is not a directory: {raw_path}")
        return parent / candidate.name

    def resolve_for_directory(self, raw_path: str) -> Path:
        """Resolve one absent directory whose canonical parent already exists."""

        relative = self._validate_relative(raw_path)
        candidate = self.root / relative
        if candidate.exists() or candidate.is_symlink():
            raise WorkspacePathError("path_exists", f"Path already exists: {raw_path}")
        try:
            parent = candidate.parent.resolve(strict=True)
        except FileNotFoundError as exc:
            raise WorkspacePathError("path_not_found", f"Parent directory does not exist: {raw_path}") from exc
        self._require_inside(parent)
        if not parent.is_dir():
            raise WorkspacePathError("invalid_path_type", f"Parent is not a directory: {raw_path}")
        return parent / candidate.name

    def relative_display(self, path: Path) -> str:
        """Return a stable POSIX-style path relative to the workspace."""

        return path.relative_to(self.root).as_posix() or "."

    def _validate_relative(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise WorkspacePathError("invalid_arguments", "Path must be a non-empty string")
        normalized = raw_path.replace("\\", "/")
        windows_path = PureWindowsPath(raw_path)
        if normalized.startswith(("/", "//")) or windows_path.is_absolute() or windows_path.drive:
            raise WorkspacePathError("path_outside_workspace", f"Absolute paths are not allowed: {raw_path}")
        parts = [part for part in normalized.split("/") if part not in {"", "."}]
        if ".." in parts:
            raise WorkspacePathError("path_outside_workspace", f"Parent traversal is not allowed: {raw_path}")
        for part in parts:
            _validate_portable_component(part, raw_path)
        if parts and parts[0].casefold() in RESERVED_TOP_LEVEL:
            raise WorkspacePathError("reserved_path", f"Runtime path is reserved: {parts[0]}")
        return Path(*parts) if parts else Path(".")

    def _require_inside(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError("path_outside_workspace", "Resolved path is outside the workspace") from exc


class WorkspaceFileTools:
    """Bounded file operations exposed to the model."""

    def __init__(
        self,
        policy: WorkspacePolicy,
        *,
        transaction: WorkspaceTransaction | None = None,
    ) -> None:
        self.policy = policy
        self.transaction = transaction

    def list_files(self, path: str = ".", recursive: bool = False, max_entries: int = DEFAULT_MAX_ENTRIES) -> ToolResult:
        """List safe workspace entries without following directory symlinks."""

        try:
            root = self.policy.resolve_existing(path, expected="directory")
            entries: list[JSONValue] = []
            pending = [root]
            truncated = False
            while pending:
                directory = pending.pop()
                for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
                    relative = self.policy.relative_display(child)
                    if relative.split("/", 1)[0].casefold() in RESERVED_TOP_LEVEL:
                        continue
                    try:
                        canonical = self.policy.resolve_existing(relative)
                    except WorkspacePathError:
                        continue
                    entries.append({"path": relative, "type": "directory" if canonical.is_dir() else "file"})
                    if len(entries) >= max_entries:
                        truncated = True
                        pending.clear()
                        break
                    if recursive and canonical.is_dir() and not child.is_symlink():
                        pending.append(canonical)
                if not recursive:
                    break
            return ToolResult.success({"entries": entries, "truncated": truncated})
        except WorkspacePathError as exc:
            return ToolResult.failure(exc.code, str(exc))
        except OSError as exc:
            return ToolResult.failure("execution_error", f"Cannot list files: {exc}")

    def search_text(
        self,
        query: str,
        path: str = ".",
        pattern: str = "*",
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> ToolResult:
        """Search literal text in bounded UTF-8 workspace files."""

        if not query:
            return ToolResult.failure("invalid_arguments", "Search query must not be empty")
        try:
            root = self.policy.resolve_existing(path)
            candidates = iter((root,)) if root.is_file() else self._walk_files(root)
            matches: list[JSONValue] = []
            truncated = False
            for candidate in candidates:
                if not fnmatch.fnmatch(candidate.name, pattern):
                    continue
                relative = self.policy.relative_display(candidate)
                try:
                    safe_file = self.policy.resolve_existing(relative, expected="file")
                    raw = _read_bounded(safe_file, MAX_FILE_BYTES)
                except (WorkspacePathError, OSError):
                    continue
                text = raw[0].decode("utf-8", errors="replace")
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if query in line:
                        matches.append({"path": relative, "line": line_number, "text": line[:1000]})
                        if len(matches) >= max_results:
                            truncated = True
                            break
                if truncated:
                    break
            return ToolResult.success({"matches": matches, "truncated": truncated})
        except WorkspacePathError as exc:
            return ToolResult.failure(exc.code, str(exc))

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 400) -> ToolResult:
        """Read a bounded UTF-8 line range."""

        try:
            target = self.policy.resolve_existing(path, expected="file")
            payload, byte_truncated = _read_bounded(target, MAX_FILE_BYTES)
            lines = payload.decode("utf-8", errors="replace").splitlines(keepends=True)
            selected = lines[start_line - 1 : start_line - 1 + max_lines]
            end_line = start_line + len(selected) - 1 if selected else start_line - 1
            truncated = byte_truncated or end_line < len(lines)
            return ToolResult.success(
                {
                    "path": self.policy.relative_display(target),
                    "content": "".join(selected),
                    "start_line": start_line,
                    "end_line": end_line,
                    "truncated": truncated,
                }
            )
        except WorkspacePathError as exc:
            return ToolResult.failure(exc.code, str(exc))
        except OSError as exc:
            return ToolResult.failure("execution_error", f"Cannot read file: {exc}")

    def write_file(self, path: str, content: str, overwrite: bool = True) -> ToolResult:
        """Atomically create or overwrite one UTF-8 file."""

        try:
            target = self.policy.resolve_for_write(path)
            if target.exists() and not overwrite:
                return ToolResult.failure("path_exists", f"File already exists: {path}")
            if self.transaction is not None:
                self.transaction.prepare_file(target)
            _atomic_write(target, content)
            relative = self.policy.relative_display(target)
            return ToolResult.success(
                {"path": relative, "bytes_written": len(content.encode("utf-8"))},
                metadata={"changed_file": relative, "mutation": True},
            )
        except WorkspacePathError as exc:
            return ToolResult.failure(exc.code, str(exc))
        except TransactionError as exc:
            return ToolResult.failure("transaction_error", str(exc))
        except OSError as exc:
            return ToolResult.failure("execution_error", f"Cannot write file: {exc}")

    def create_directory(self, path: str) -> ToolResult:
        """Create one safe workspace directory whose parent already exists."""

        try:
            target = self.policy.resolve_for_directory(path)
            if self.transaction is not None:
                self.transaction.prepare_directory(target)
            target.mkdir()
            relative = self.policy.relative_display(target)
            return ToolResult.success(
                {"path": relative},
                metadata={"changed_directory": relative, "mutation": True},
            )
        except WorkspacePathError as exc:
            return ToolResult.failure(exc.code, str(exc))
        except TransactionError as exc:
            return ToolResult.failure("transaction_error", str(exc))
        except OSError as exc:
            return ToolResult.failure("execution_error", f"Cannot create directory: {exc}")

    def replace_text(
        self,
        path: str,
        old: str,
        new: str,
        expected_replacements: int = 1,
    ) -> ToolResult:
        """Replace text only when the observed count matches the contract."""

        if not old:
            return ToolResult.failure("invalid_arguments", "Replacement source must not be empty")
        try:
            target = self.policy.resolve_existing(path, expected="file")
            payload, truncated = _read_bounded(target, MAX_FILE_BYTES)
            if truncated:
                return ToolResult.failure("file_too_large", f"File exceeds {MAX_FILE_BYTES} bytes: {path}")
            content = payload.decode("utf-8")
            actual = content.count(old)
            if actual != expected_replacements:
                return ToolResult.failure(
                    "replacement_match_count",
                    f"Expected {expected_replacements} match(es), found {actual}",
                    data={"expected": expected_replacements, "actual": actual},
                )
            if self.transaction is not None:
                self.transaction.prepare_file(target)
            _atomic_write(target, content.replace(old, new, expected_replacements))
            relative = self.policy.relative_display(target)
            return ToolResult.success(
                {"path": relative, "replacements": actual},
                metadata={"changed_file": relative, "mutation": True},
            )
        except UnicodeDecodeError:
            return ToolResult.failure("invalid_text_encoding", f"File is not valid UTF-8: {path}")
        except WorkspacePathError as exc:
            return ToolResult.failure(exc.code, str(exc))
        except TransactionError as exc:
            return ToolResult.failure("transaction_error", str(exc))
        except OSError as exc:
            return ToolResult.failure("execution_error", f"Cannot replace text: {exc}")

    def _walk_files(self, root: Path) -> Iterator[Path]:
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_names[:] = [name for name in directory_names if name.casefold() not in RESERVED_TOP_LEVEL]
            directory_names.sort(key=str.casefold)
            for file_name in sorted(file_names, key=str.casefold):
                candidate = Path(directory) / file_name
                relative = self.policy.relative_display(candidate)
                try:
                    yield self.policy.resolve_existing(relative, expected="file")
                except WorkspacePathError:
                    continue


def _read_bounded(path: Path, limit: int) -> tuple[bytes, bool]:
    with path.open("rb") as handle:
        payload = handle.read(limit + 1)
    return payload[:limit], len(payload) > limit


def _validate_portable_component(component: str, raw_path: str) -> None:
    """Reject Win32 aliases and NTFS streams before resolving a model path."""

    if (
        component[-1] in {" ", "."}
        or any(character in _WINDOWS_INVALID_NAME_CHARACTERS or ord(character) < 32 for character in component)
        or component.split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise WorkspacePathError("invalid_path", f"Path contains an unsafe component: {raw_path}")


def _atomic_write(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
