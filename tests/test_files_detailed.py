from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest

from yukitools_rime import files
from yukitools_rime.errors import FileOperationError, ValidationError


class _UnresolvablePath:
    def resolve(self, *, strict: bool) -> Path:
        raise OSError("resolve denied")

    def __str__(self) -> str:
        return "fallback-path"


def test_display_path_falls_back_and_cleanup_suppresses_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert files.display_path(_UnresolvablePath()) == "fallback-path"  # type: ignore[arg-type]

    def interrupt(*_: object, **__: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(shutil, "rmtree", interrupt)
    files.discard_tree(tmp_path / "anything")


def test_normalize_and_verbatim_read_validation(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="expects str"):
        files.normalize_text(b"bytes")  # type: ignore[arg-type]

    invalid = tmp_path / "invalid"
    invalid.write_bytes(b"\xff")
    with pytest.raises(FileOperationError, match="UTF-8"):
        files.read_text_verbatim(invalid)


def test_read_bytes_rejects_nonfile_and_wraps_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(FileOperationError, match="regular file"):
        files.read_bytes(tmp_path)

    path = tmp_path / "data"
    path.write_bytes(b"value")
    real_read_bytes = Path.read_bytes

    def denied(candidate: Path) -> bytes:
        if candidate == path:
            raise PermissionError("read denied")
        return real_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(FileOperationError, match="read denied"):
        files.read_bytes(path)


def test_prepare_target_rejects_missing_parent_directory_and_target(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileOperationError, match="parent"):
        files.atomic_write_bytes(
            tmp_path / "missing" / "value",
            b"value",
            create_parents=False,
        )

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(FileOperationError, match="regular file"):
        files.atomic_write_bytes(directory, b"value")


def test_prepare_target_wraps_mkdir_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "denied" / "value"
    real_mkdir = Path.mkdir

    def denied(
        candidate: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if candidate == target.parent:
            raise PermissionError("mkdir denied")
        real_mkdir(candidate, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(FileOperationError, match="mkdir denied"):
        files.atomic_write_bytes(target, b"value")


def test_atomic_write_rejects_nonbytes_before_touching_disk(tmp_path: Path) -> None:
    target = tmp_path / "value"
    with pytest.raises(TypeError, match="expects bytes"):
        files.atomic_write_bytes(target, bytearray(b"value"))  # type: ignore[arg-type]
    assert not target.exists()


def test_atomic_write_wraps_mkstemp_and_fdopen_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "value"

    def denied_mkstemp(*_: object, **__: object) -> tuple[int, str]:
        raise PermissionError("mkstemp denied")

    monkeypatch.setattr(tempfile, "mkstemp", denied_mkstemp)
    with pytest.raises(FileOperationError, match="mkstemp denied"):
        files.atomic_write_bytes(target, b"value")

    monkeypatch.undo()
    created: list[Path] = []
    real_mkstemp: Any = tempfile.mkstemp

    def tracked_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, raw = real_mkstemp(*args, **kwargs)
        created.append(Path(raw))
        return fd, raw

    def denied_fdopen(*_: object, **__: object) -> Any:
        raise OSError("fdopen denied")

    monkeypatch.setattr(tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(os, "fdopen", denied_fdopen)
    with pytest.raises(FileOperationError, match="fdopen denied"):
        files.atomic_write_bytes(target, b"value")

    assert created and not created[0].exists()


class _FailingStream:
    def __init__(self, stream: Any, failure: BaseException | None) -> None:
        self._stream = stream
        self._failure = failure

    def __enter__(self) -> _FailingStream:
        return self

    def __exit__(self, *args: object) -> None:
        self._stream.close()

    def write(self, data: bytes) -> int:
        if self._failure is not None:
            raise self._failure
        return cast(int, self._stream.write(data))

    def flush(self) -> None:
        self._stream.flush()

    def fileno(self) -> int:
        return cast(int, self._stream.fileno())


@pytest.mark.parametrize("failure_point", ["write", "fsync", "chmod", "replace"])
@pytest.mark.parametrize(
    ("failure", "expected"),
    [(OSError("operation failed"), FileOperationError), (KeyboardInterrupt(), KeyboardInterrupt)],
)
def test_atomic_write_failure_positions_preserve_original_and_clean_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    failure: BaseException,
    expected: type[BaseException],
) -> None:
    target = tmp_path / "value"
    target.write_bytes(b"original")
    target.chmod(0o640)
    real_fdopen = os.fdopen
    real_chmod = Path.chmod

    if failure_point == "write":

        def failing_fdopen(fd: int, mode: str) -> _FailingStream:
            return _FailingStream(real_fdopen(fd, mode), failure)

        monkeypatch.setattr(os, "fdopen", failing_fdopen)
    elif failure_point == "fsync":

        def failing_fsync(_: int) -> None:
            raise failure

        monkeypatch.setattr(os, "fsync", failing_fsync)
    elif failure_point == "chmod":

        def failing_chmod(candidate: Path, mode: int) -> None:
            if candidate.name.endswith(".tmp"):
                raise failure
            real_chmod(candidate, mode)

        monkeypatch.setattr(Path, "chmod", failing_chmod)
    else:

        def failing_replace(_: object, __: object) -> None:
            raise failure

        monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(expected):
        files.atomic_write_bytes(target, b"replacement")

    assert target.read_bytes() == b"original"
    assert target.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".value.*.tmp"))


def test_atomic_write_cleanup_error_does_not_hide_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "value"
    created: list[Path] = []
    real_mkstemp: Any = tempfile.mkstemp
    real_unlink = Path.unlink

    def tracked_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, raw = real_mkstemp(*args, **kwargs)
        created.append(Path(raw))
        return fd, raw

    def failing_fsync(_: int) -> None:
        raise OSError("fsync failed")

    def failing_unlink(
        candidate: Path,
        missing_ok: bool = False,
    ) -> None:
        if candidate in created:
            raise OSError("cleanup failed")
        real_unlink(candidate, missing_ok=missing_ok)

    monkeypatch.setattr(tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(FileOperationError, match="fsync failed"):
        files.atomic_write_bytes(target, b"value")

    assert created[0].exists()
    os.unlink(created[0])


def test_relative_containment_and_remove_file_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert files.ensure_within(tmp_path, Path("child")) == (tmp_path / "child").resolve()

    missing = tmp_path / "missing"
    with pytest.raises(FileOperationError, match="does not exist"):
        files.remove_file(missing, missing_ok=False)
    files.remove_file(missing)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(FileOperationError, match="regular file"):
        files.remove_file(directory)

    target = tmp_path / "target"
    target.write_bytes(b"value")
    real_unlink = Path.unlink

    def denied(candidate: Path, missing_ok: bool = False) -> None:
        if candidate == target:
            raise PermissionError("unlink denied")
        real_unlink(candidate, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(FileOperationError, match="unlink denied"):
        files.remove_file(target)


def test_snapshot_preflight_rejects_unsafe_paths_and_content(
    tmp_path: Path,
) -> None:
    parent_file = tmp_path / "parent-file"
    parent_file.write_bytes(b"value")
    with pytest.raises(FileOperationError, match="prepare snapshot"):
        files.replace_directory_snapshot(parent_file / "cases", {})

    target_file = tmp_path / "target-file"
    target_file.write_bytes(b"value")
    with pytest.raises(FileOperationError, match="snapshot target"):
        files.replace_directory_snapshot(target_file, {})

    with pytest.raises(ValidationError, match="must be bytes"):
        files.replace_directory_snapshot(
            tmp_path / "cases",
            {"case": "not bytes"},  # type: ignore[dict-item]
        )


def test_snapshot_replaces_absent_target(tmp_path: Path) -> None:
    target = tmp_path / "cases"
    files.replace_directory_snapshot(target, {"new": b"value"})
    assert (target / "new").read_bytes() == b"value"


def test_prepare_swap_keyboard_interrupt_cleans_both_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_rmdir = Path.rmdir
    created: list[Path] = []
    real_mkdtemp: Any = tempfile.mkdtemp

    def tracked_mkdtemp(*args: object, **kwargs: object) -> str:
        raw = cast(str, real_mkdtemp(*args, **kwargs))
        created.append(Path(raw))
        return raw

    def interrupt_backup(candidate: Path) -> None:
        if ".backup-" in candidate.name:
            raise KeyboardInterrupt
        real_rmdir(candidate)

    monkeypatch.setattr(tempfile, "mkdtemp", tracked_mkdtemp)
    monkeypatch.setattr(Path, "rmdir", interrupt_backup)

    with pytest.raises(KeyboardInterrupt):
        files._prepare_directory_swap(tmp_path, "cases", label="test")

    assert len(created) == 2
    assert all(not path.exists() for path in created)


def test_prepare_swap_first_allocation_failure_has_no_cleanup_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*_: object, **__: object) -> str:
        raise PermissionError("allocation denied")

    monkeypatch.setattr(tempfile, "mkdtemp", denied)
    with pytest.raises(FileOperationError, match="allocation denied"):
        files._prepare_directory_swap(tmp_path, "cases", label="test")

    assert list(tmp_path.iterdir()) == []


def test_snapshot_rejects_symlink_parent(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(FileOperationError, match="parent"):
        files.replace_directory_snapshot(linked / "cases", {})

    assert list(actual.iterdir()) == []


def test_snapshot_stage_write_failure_cleans_stage_and_new_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "new-parent" / "cases"

    def fail_write(*_: object, **__: object) -> None:
        raise FileOperationError("stage write failed")

    monkeypatch.setattr(files, "atomic_write_bytes", fail_write)
    with pytest.raises(FileOperationError, match="stage write"):
        files.replace_directory_snapshot(target, {"new": b"value"})

    assert not target.exists()
    assert not target.parent.exists()
    assert not list(tmp_path.rglob(".*.stage-*"))
    assert not list(tmp_path.rglob(".*.backup-*"))


def test_snapshot_commit_then_keyboard_interrupt_is_treated_as_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cases"
    target.mkdir()
    (target / "old").write_bytes(b"old")
    real_replace = os.replace

    def commit_then_interrupt(source: object, destination: object) -> None:
        source_path = Path(cast(Any, source))
        destination_path = Path(cast(Any, destination))
        real_replace(source_path, destination_path)
        if destination_path == target and ".stage-" in source_path.name:
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", commit_then_interrupt)
    files.replace_directory_snapshot(target, {"new": b"new"})

    assert not (target / "old").exists()
    assert (target / "new").read_bytes() == b"new"
    assert not list(tmp_path.glob(".cases.backup-*"))


def test_new_snapshot_commit_then_keyboard_interrupt_is_treated_as_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cases"
    real_replace = os.replace

    def commit_then_interrupt(source: object, destination: object) -> None:
        source_path = Path(cast(Any, source))
        destination_path = Path(cast(Any, destination))
        real_replace(source_path, destination_path)
        if destination_path == target and ".stage-" in source_path.name:
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", commit_then_interrupt)
    files.replace_directory_snapshot(target, {"new": b"new"})

    assert (target / "new").read_bytes() == b"new"


def test_snapshot_keyboard_before_backup_move_propagates_and_cleans_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cases"
    target.mkdir()
    (target / "old").write_bytes(b"old")
    real_replace = os.replace

    def interrupt_first_swap(source: object, destination: object) -> None:
        if Path(cast(Any, source)) == target:
            raise KeyboardInterrupt
        real_replace(cast(Any, source), cast(Any, destination))

    monkeypatch.setattr(os, "replace", interrupt_first_swap)
    with pytest.raises(KeyboardInterrupt):
        files.replace_directory_snapshot(target, {"new": b"new"})

    assert (target / "old").read_bytes() == b"old"
    assert not list(tmp_path.glob(".cases.stage-*"))


def test_snapshot_reports_rollback_failure_and_keeps_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cases"
    target.mkdir()
    (target / "old").write_bytes(b"old")
    real_replace = os.replace

    def fail_publish_and_rollback(source: object, destination: object) -> None:
        source_path = Path(cast(Any, source))
        destination_path = Path(cast(Any, destination))
        if source_path == target:
            real_replace(source_path, destination_path)
            return
        if destination_path == target and source_path.is_dir():
            raise OSError(f"cannot move {source_path.name}")

        real_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", fail_publish_and_rollback)
    with pytest.raises(FileOperationError, match="rollback also failed"):
        files.replace_directory_snapshot(target, {"new": b"new"})

    backups = list(tmp_path.glob(".cases.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "old").read_bytes() == b"old"
    assert not target.exists()
