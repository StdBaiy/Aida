from pathlib import Path

import pytest

from coding_agent.errors import CodingAgentError
from coding_agent.workspace.patch import (
    AddOperation,
    ApplyPatchInput,
    DeleteOperation,
    PatchService,
    Replacement,
    UpdateOperation,
    sha256_bytes,
)
from coding_agent.workspace.paths import PathGuard


def test_patch_add_update_delete(tmp_path: Path) -> None:
    service = PatchService(PathGuard(tmp_path))
    service.apply(
        ApplyPatchInput(operations=[AddOperation(op="add", path="a.txt", content="one\n")])
    )
    original = (tmp_path / "a.txt").read_bytes()
    service.apply(
        ApplyPatchInput(
            operations=[
                UpdateOperation(
                    op="update",
                    path="a.txt",
                    expected_sha256=sha256_bytes(original),
                    replacements=[Replacement(old_text="one", new_text="two")],
                )
            ]
        )
    )
    updated = (tmp_path / "a.txt").read_bytes()
    assert updated == b"two\n"

    service.apply(
        ApplyPatchInput(
            operations=[
                DeleteOperation(
                    op="delete",
                    path="a.txt",
                    expected_sha256=sha256_bytes(updated),
                )
            ]
        )
    )
    assert not (tmp_path / "a.txt").exists()


def test_patch_validates_all_operations_before_writing(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("before")
    service = PatchService(PathGuard(tmp_path))

    with pytest.raises(CodingAgentError, match="changed since"):
        service.apply(
            ApplyPatchInput(
                operations=[
                    AddOperation(op="add", path="new.txt", content="new"),
                    UpdateOperation(
                        op="update",
                        path="existing.txt",
                        expected_sha256="stale",
                        replacements=[Replacement(old_text="before", new_text="after")],
                    ),
                ]
            )
        )

    assert not (tmp_path / "new.txt").exists()
    assert existing.read_text() == "before"


def test_patch_rejects_oversized_arguments_before_writing(tmp_path: Path) -> None:
    service = PatchService(PathGuard(tmp_path), max_argument_chars=200)

    with pytest.raises(CodingAgentError) as raised:
        service.apply(
            ApplyPatchInput(
                operations=[
                    AddOperation(op="add", path="large.txt", content="x" * 300),
                ]
            )
        )

    assert raised.value.code == "PATCH_TOO_LARGE"
    assert "Split the same file" in raised.value.user_message
    assert not (tmp_path / "large.txt").exists()


def test_patch_rejects_multiple_new_files_before_writing(tmp_path: Path) -> None:
    service = PatchService(PathGuard(tmp_path))

    with pytest.raises(CodingAgentError) as raised:
        service.apply(
            ApplyPatchInput(
                operations=[
                    AddOperation(op="add", path="first.txt", content="first"),
                    AddOperation(op="add", path="second.txt", content="second"),
                ]
            )
        )

    assert raised.value.code == "PATCH_TOO_MANY_NEW_FILES"
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()
