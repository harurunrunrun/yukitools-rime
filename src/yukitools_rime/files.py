"""Safe, normalized local-file operations."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from yukitools_rime.errors import FileOperationError, ValidationError
from yukitools_rime.models import validate_basename


def display_path(path: Path) -> str:
    """Return a stable path spelling suitable for user-facing errors."""

    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path)


def discard_tree(path: Path) -> None:
    """Best-effort cleanup that cannot invalidate an already committed swap."""

    # Ctrl-C during cleanup must not look like a failed atomic operation.
    with suppress(BaseException):
        shutil.rmtree(path, ignore_errors=True)


def _discard_empty_directories(paths: list[Path]) -> None:
    """Best-effort removal of directories created for an aborted operation."""

    for path in paths:
        with suppress(BaseException):
            path.rmdir()


def _prepare_directory_swap(
    parent: Path,
    name: str,
    *,
    label: str,
) -> tuple[Path, Path]:
    """Allocate a sibling stage and an absent backup path with safe cleanup."""

    stage: Path | None = None
    backup: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(dir=parent, prefix=f".{name}.stage-"))
        backup = Path(tempfile.mkdtemp(dir=parent, prefix=f".{name}.backup-"))
        backup.rmdir()
    except BaseException as exc:
        if stage is not None and stage.exists():
            discard_tree(stage)
        if backup is not None and backup.exists():
            discard_tree(backup)
        if isinstance(exc, OSError):
            raise FileOperationError(
                f"could not prepare {label} in {display_path(parent)}: {exc}"
            ) from exc
        raise
    assert stage is not None
    assert backup is not None
    return stage, backup


def normalize_text(text: str) -> str:
    """Remove a leading UTF-8 BOM marker and normalize newlines to LF."""

    if not isinstance(text, str):
        raise TypeError("normalize_text() expects str")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_bytes(path: Path) -> bytes:
    """Read a regular non-symlink file, adding path context to failures."""

    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise FileOperationError(f"not a regular file: {display_path(path)}")
        return path.read_bytes()
    except FileOperationError:
        raise
    except OSError as exc:
        raise FileOperationError(f"could not read {display_path(path)}: {exc}") from exc


def read_text(path: Path) -> str:
    """Read strict UTF-8 and return BOM-free, LF-normalized text."""

    data = read_bytes(path)
    try:
        return normalize_text(data.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise FileOperationError(f"{display_path(path)} is not valid UTF-8: {exc}") from exc


def read_text_verbatim(path: Path) -> str:
    """Read strict UTF-8 without changing a BOM or newline spelling."""

    data = read_bytes(path)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileOperationError(f"{display_path(path)} is not valid UTF-8: {exc}") from exc


def _prepare_target(path: Path, *, create_parents: bool) -> tuple[Path, int | None]:
    parent = path.parent
    try:
        if create_parents:
            parent.mkdir(parents=True, exist_ok=True)
        if not parent.is_dir() or parent.is_symlink():
            raise FileOperationError(f"parent is not a regular directory: {display_path(parent)}")
        if path.is_symlink():
            raise FileOperationError(f"refusing to replace symlink: {display_path(path)}")
        if path.exists() and not path.is_file():
            raise FileOperationError(f"not a regular file: {display_path(path)}")
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    except FileOperationError:
        raise
    except OSError as exc:
        raise FileOperationError(f"could not prepare {display_path(path)}: {exc}") from exc
    return parent, mode


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    create_parents: bool = True,
) -> None:
    """Write bytes through a sibling temporary file and atomically replace target."""

    if not isinstance(data, bytes):
        raise TypeError("atomic_write_bytes() expects bytes")
    path = Path(path)
    parent, mode = _prepare_target(path, create_parents=create_parents)
    fd = -1
    temporary: Path | None = None
    try:
        fd, raw_temporary = tempfile.mkstemp(dir=parent, prefix=f".{path.name}.", suffix=".tmp")
        temporary = Path(raw_temporary)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise FileOperationError(f"could not update {display_path(path)}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    create_parents: bool = True,
) -> None:
    """Normalize text and atomically write it as UTF-8 without a BOM."""

    atomic_write_bytes(
        path,
        normalize_text(text).encode("utf-8"),
        create_parents=create_parents,
    )


# Short aliases used by command and layout modules. Writes remain atomic.
write_bytes = atomic_write_bytes
write_text = atomic_write_text


def ensure_within(root: Path, candidate: Path, *, label: str = "path") -> Path:
    """Resolve candidate and reject paths outside root, including symlink escapes."""

    root = Path(root).resolve(strict=False)
    candidate = Path(candidate)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError(
            f"{label} escapes {display_path(root)}: {display_path(candidate)}"
        ) from exc
    return resolved


def safe_child(root: Path, name: str, *, label: str = "file name") -> Path:
    """Return a resolved direct child of root after basename and containment checks."""

    name = validate_basename(name, label=label)
    root = Path(root).resolve(strict=False)
    return ensure_within(root, root / name, label=label)


def require_regular_file(root: Path, name: str, *, label: str = "source") -> Path:
    """Resolve a same-directory source and require a regular, non-symlink file."""

    name = validate_basename(name, label=label)
    lexical_path = Path(root).resolve(strict=False) / name
    if lexical_path.is_symlink():
        raise FileOperationError(f"{label} is not a regular file: {display_path(lexical_path)}")
    path = ensure_within(root, lexical_path, label=label)
    if not path.is_file():
        raise FileOperationError(f"{label} is not a regular file: {display_path(path)}")
    return path


def remove_file(path: Path, *, missing_ok: bool = True) -> None:
    """Remove one non-symlink regular file."""

    path = Path(path)
    try:
        if not path.exists() and not path.is_symlink():
            if missing_ok:
                return
            raise FileOperationError(f"file does not exist: {display_path(path)}")
        if path.is_symlink() or not path.is_file():
            raise FileOperationError(f"not a regular file: {display_path(path)}")
        path.unlink()
    except FileOperationError:
        raise
    except OSError as exc:
        raise FileOperationError(f"could not remove {display_path(path)}: {exc}") from exc


def replace_directory_snapshot(
    directory: Path,
    files: Mapping[str, bytes],
) -> None:
    """Replace a flat directory snapshot, rolling back if the final swap fails."""

    directory = Path(directory)
    parent = directory.parent
    created_parents: list[Path] = []
    candidate = parent
    while not candidate.exists() and not candidate.is_symlink():
        created_parents.append(candidate)
        candidate = candidate.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise FileOperationError(f"parent is not a regular directory: {display_path(parent)}")
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise FileOperationError(
                f"snapshot target is not a regular directory: {display_path(directory)}"
            )
        validated: dict[str, bytes] = {}
        for name, content in files.items():
            validate_basename(name, label="snapshot file name")
            if not isinstance(content, bytes):
                raise ValidationError(f"snapshot content for {name!r} must be bytes")
            validated[name] = content
    except (FileOperationError, ValidationError):
        _discard_empty_directories(created_parents)
        raise
    except OSError as exc:
        _discard_empty_directories(created_parents)
        raise FileOperationError(
            f"could not prepare snapshot {display_path(directory)}: {exc}"
        ) from exc

    try:
        stage, backup = _prepare_directory_swap(
            parent, directory.name, label=f"snapshot for {display_path(directory)}"
        )
    except BaseException:
        _discard_empty_directories(created_parents)
        raise
    try:
        for name, content in validated.items():
            atomic_write_bytes(stage / name, content, create_parents=False)
        if directory.exists():
            os.replace(directory, backup)
        os.replace(stage, directory)
        if backup.exists():
            discard_tree(backup)
    except BaseException as exc:
        # A missing stage plus an installed target proves the final replace
        # committed, even if Ctrl-C arrived before the next Python bytecode.
        if not stage.exists() and directory.exists():
            if backup.exists():
                discard_tree(backup)
            return
        rollback_error: OSError | None = None
        if not directory.exists() and backup.exists():
            try:
                os.replace(backup, directory)
            except OSError as rollback_exc:
                rollback_error = rollback_exc
        if not isinstance(exc, Exception) and rollback_error is None:
            raise
        detail = f": rollback also failed: {rollback_error}" if rollback_error else ""
        raise FileOperationError(
            f"could not replace snapshot {display_path(directory)}: {exc}{detail}"
        ) from exc
    finally:
        committed = not stage.exists() and directory.exists()
        if stage.exists():
            discard_tree(stage)
        if backup.exists() and committed:
            discard_tree(backup)
        _discard_empty_directories(created_parents)


__all__ = [
    "atomic_write_bytes",
    "atomic_write_text",
    "display_path",
    "ensure_within",
    "normalize_text",
    "read_bytes",
    "read_text",
    "read_text_verbatim",
    "remove_file",
    "replace_directory_snapshot",
    "require_regular_file",
    "safe_child",
    "write_bytes",
    "write_text",
]
