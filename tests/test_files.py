from __future__ import annotations

import os
from pathlib import Path

import pytest

from yukitools_rime import files as files_module
from yukitools_rime.errors import FileOperationError, ValidationError
from yukitools_rime.files import (
    atomic_write_bytes,
    atomic_write_text,
    ensure_within,
    normalize_text,
    read_text,
    replace_directory_snapshot,
    require_regular_file,
    safe_child,
)


def test_text_round_trip_normalizes_bom_and_newlines(tmp_path: Path) -> None:
    assert normalize_text("\ufeffa\r\nb\rc") == "a\nb\nc"
    path = tmp_path / "nested" / "text"
    atomic_write_text(path, "\ufeff雪\r\n")
    assert path.read_bytes() == "雪\n".encode()
    assert read_text(path) == "雪\n"
    assert not list(path.parent.glob(".text.*.tmp"))


def test_atomic_write_preserves_mode(tmp_path: Path) -> None:
    path = tmp_path / "script"
    path.write_bytes(b"old")
    path.chmod(0o750)
    atomic_write_bytes(path, b"new")
    assert path.read_bytes() == b"new"
    assert path.stat().st_mode & 0o777 == 0o750


def test_read_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "bad"
    path.write_bytes(b"\xff")
    with pytest.raises(FileOperationError, match="UTF-8"):
        read_text(path)


def test_safe_paths_reject_escape(tmp_path: Path) -> None:
    assert safe_child(tmp_path, "main.cpp") == (tmp_path / "main.cpp").resolve()
    with pytest.raises(ValidationError):
        safe_child(tmp_path, "../main.cpp")
    with pytest.raises(ValidationError):
        ensure_within(tmp_path, tmp_path.parent / "outside")


def test_regular_file_rejects_directory_and_symlink(tmp_path: Path) -> None:
    source = tmp_path / "main.cpp"
    source.write_text("x")
    assert require_regular_file(tmp_path, "main.cpp") == source.resolve()
    (tmp_path / "directory").mkdir()
    with pytest.raises(FileOperationError):
        require_regular_file(tmp_path, "directory")
    link = tmp_path / "link.cpp"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(FileOperationError):
        require_regular_file(tmp_path, "link.cpp")
    with pytest.raises(FileOperationError):
        atomic_write_bytes(link, b"unsafe")
    assert source.read_text() == "x"


def test_snapshot_replaces_all_files(tmp_path: Path) -> None:
    target = tmp_path / "cases"
    target.mkdir()
    (target / "old.in").write_bytes(b"old")
    replace_directory_snapshot(target, {"new.in": b"1", "new.diff": b"2"})
    assert {p.name: p.read_bytes() for p in target.iterdir()} == {"new.in": b"1", "new.diff": b"2"}
    with pytest.raises(ValidationError):
        replace_directory_snapshot(target, {"../escape": b"x"})
    assert (target / "new.in").read_bytes() == b"1"


def test_snapshot_rolls_back_failed_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "cases"
    target.mkdir()
    (target / "old").write_bytes(b"old")
    real_replace = os.replace
    calls = 0

    def replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(FileOperationError, match="simulated"):
        replace_directory_snapshot(target, {"new": b"new"})
    assert (target / "old").read_bytes() == b"old"


def test_snapshot_cleans_stage_when_backup_allocation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cases"
    target.mkdir()
    (target / "old").write_bytes(b"old")
    real_mkdtemp = files_module.tempfile.mkdtemp
    created: list[Path] = []
    calls = 0

    def fail_second_mkdtemp(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("backup denied")
        result = real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]
        created.append(Path(result))
        return result

    monkeypatch.setattr(files_module.tempfile, "mkdtemp", fail_second_mkdtemp)
    with pytest.raises(FileOperationError, match="backup denied"):
        replace_directory_snapshot(target, {"new": b"new"})

    assert created and not created[0].exists()
    assert (target / "old").read_bytes() == b"old"
