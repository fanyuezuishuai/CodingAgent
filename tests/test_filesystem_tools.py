"""Workspace boundary and file-tool tests."""

from pathlib import Path

import pytest

from tracecoder.tools.filesystem import WorkspaceFileTools, WorkspacePathError, WorkspacePolicy
from tracecoder.transaction import WorkspaceTransaction


@pytest.fixture
def tools(tmp_path: Path) -> WorkspaceFileTools:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sample.py").write_text("alpha\nbeta alpha\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TRACECODER_API_KEY=dotenv-secret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("TRACECODER_API_KEY=replace-me\n", encoding="utf-8")
    (tmp_path / ".tracecoder").mkdir()
    (tmp_path / ".tracecoder" / "private.jsonl").write_text("secret", encoding="utf-8")
    return WorkspaceFileTools(WorkspacePolicy(tmp_path))


@pytest.mark.parametrize("path", ["../outside.txt", "/absolute.txt", "C:\\outside.txt", "\\\\host\\share\\x"])
def test_read_rejects_workspace_escape(tools: WorkspaceFileTools, path: str) -> None:
    result = tools.read_file(path)

    assert not result.ok
    assert result.error_code == "path_outside_workspace"


def test_reserved_runtime_directory_is_hidden(tools: WorkspaceFileTools) -> None:
    read_result = tools.read_file(".tracecoder/private.jsonl")
    list_result = tools.list_files(".", recursive=True)

    assert read_result.error_code == "reserved_path"
    assert all(".tracecoder" not in str(item) for item in list_result.data["entries"])


def test_workspace_dotenv_is_reserved_but_example_remains_readable(tools: WorkspaceFileTools) -> None:
    direct_results = [
        tools.read_file(".env"),
        tools.write_file(".env", "TRACECODER_API_KEY=replaced\n"),
        tools.replace_text(".env", "dotenv-secret", "replaced-secret"),
        tools.search_text("dotenv-secret", ".env"),
        tools.list_files(".env"),
    ]
    flat_list = tools.list_files(".")
    recursive_list = tools.list_files(".", recursive=True)
    search_result = tools.search_text("dotenv-secret")

    assert all(result.error_code == "reserved_path" for result in direct_results)
    assert tools.read_file(".env.example").ok
    assert ".env.example" in [entry["path"] for entry in flat_list.data["entries"]]
    assert ".env" not in [entry["path"] for entry in flat_list.data["entries"]]
    assert ".env" not in [entry["path"] for entry in recursive_list.data["entries"]]
    assert search_result.ok
    assert search_result.data["matches"] == []


@pytest.mark.parametrize(
    "path",
    [".env.", ".env ", ".env::$DATA", "src/CON.txt", "src/name?.py", "src/control\x01.py"],
)
def test_windows_unsafe_path_components_are_rejected(tmp_path: Path, path: str) -> None:
    policy = WorkspacePolicy(tmp_path)

    with pytest.raises(WorkspacePathError) as raised:
        policy.resolve_for_write(path)

    assert raised.value.code == "invalid_path"


def test_read_search_write_and_replace(tools: WorkspaceFileTools, tmp_path: Path) -> None:
    read_result = tools.read_file("src/sample.py", start_line=2, max_lines=1)
    search_result = tools.search_text("alpha", "src", pattern="*.py")
    write_result = tools.write_file("src/new.py", "value = 1\n")
    replace_result = tools.replace_text("src/new.py", "1", "2")

    assert read_result.ok and str(read_result.data["content"]).strip() == "beta alpha"
    assert search_result.ok and len(search_result.data["matches"]) == 2
    assert write_result.metadata["changed_file"] == "src/new.py"
    assert replace_result.ok
    assert (tmp_path / "src" / "new.py").read_text(encoding="utf-8") == "value = 2\n"


def test_replace_requires_exact_match_count(tools: WorkspaceFileTools, tmp_path: Path) -> None:
    result = tools.replace_text("src/sample.py", "alpha", "changed")

    assert not result.ok
    assert result.error_code == "replacement_match_count"
    assert (tmp_path / "src" / "sample.py").read_text(encoding="utf-8") == "alpha\nbeta alpha\n"


def test_write_requires_existing_parent(tools: WorkspaceFileTools) -> None:
    result = tools.write_file("missing/new.py", "content")

    assert not result.ok
    assert result.error_code == "path_not_found"


def test_create_directory_then_write_file_with_transaction(tmp_path: Path) -> None:
    transaction = WorkspaceTransaction(tmp_path, "directory-run")
    tools = WorkspaceFileTools(WorkspacePolicy(tmp_path), transaction=transaction)

    directory_result = tools.create_directory("course_project")
    write_result = tools.write_file("course_project/main.py", "print('ok')\n")

    assert directory_result.ok
    assert directory_result.metadata["changed_directory"] == "course_project"
    assert write_result.ok
    transaction.seal()
    assert transaction.rollback_available
    transaction.rollback()
    assert not (tmp_path / "course_project").exists()


def test_create_directory_requires_existing_safe_parent(tmp_path: Path) -> None:
    tools = WorkspaceFileTools(WorkspacePolicy(tmp_path))

    result = tools.create_directory("missing/nested")

    assert not result.ok
    assert result.error_code == "path_not_found"


def test_transaction_rejects_oversized_snapshot_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "large.txt"
    original = "x" * 1_000_001
    target.write_text(original, encoding="utf-8")
    transaction = WorkspaceTransaction(tmp_path, "large-snapshot")
    tools = WorkspaceFileTools(WorkspacePolicy(tmp_path), transaction=transaction)

    result = tools.write_file("large.txt", "replacement", overwrite=True)

    assert not result.ok
    assert result.error_code == "transaction_error"
    assert target.read_text(encoding="utf-8") == original
    assert transaction.state == "not_required"


def test_outside_symlink_is_rejected(tools: WorkspaceFileTools, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "src" / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this platform")

    result = tools.read_file("src/escape.txt")

    assert not result.ok
    assert result.error_code == "path_outside_workspace"
