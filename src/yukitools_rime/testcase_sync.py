"""Transactional synchronization of remote testcases and Rime output files."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from yukitools_rime.errors import ConflictError, FileOperationError, ValidationError
from yukitools_rime.files import atomic_write_bytes, discard_tree, display_path, read_bytes
from yukitools_rime.layout import (
    TestCaseData,
    inspect_testcases,
    local_testcase_paths,
    read_testcases,
)
from yukitools_rime.models import Which, validate_testcase_name

MAX_UPLOAD_FILES = 100
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MULTIPART_FILE_OVERHEAD = 512

TestcaseSnapshot = Mapping[str, TestCaseData]


class _StagedTestcaseSnapshot(Mapping[str, TestCaseData]):
    """A testcase snapshot whose byte bodies remain on disk until accessed."""

    def __init__(self, root: Path, names: Iterable[str]) -> None:
        self._root = root
        self._names = tuple(sorted(names))
        self._name_set = frozenset(self._names)

    def __getitem__(self, name: str) -> TestCaseData:
        if name not in self._name_set:
            raise KeyError(name)
        input_path, output_path = local_testcase_paths(self._root, name)
        return TestCaseData(name, read_bytes(input_path), read_bytes(output_path))

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


class TestcaseAPI(Protocol):
    """The small YukicoderClient surface needed by testcase synchronization."""

    def list_testcases(self, problem_id: int, which: Which | str) -> list[str]: ...

    def get_testcase(self, problem_id: int, which: Which | str, name: str) -> bytes: ...

    def upload_testcases(
        self,
        problem_id: int,
        which: Which | str,
        files: Mapping[str, bytes],
    ) -> object: ...

    def delete_testcase(self, problem_id: int, which: Which | str, name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SnapshotChanges:
    """Remote-to-local changes, with deterministic names for warnings and diffs."""

    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    @property
    def added_count(self) -> int:
        return len(self.added)

    @property
    def changed_count(self) -> int:
        return len(self.changed)

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed or self.removed)

    @property
    def counts(self) -> tuple[int, int, int]:
        return self.added_count, self.changed_count, self.removed_count


ConfirmReplacement = Callable[[SnapshotChanges], bool]


@dataclass(frozen=True, slots=True)
class PullResult:
    remote_snapshot: TestcaseSnapshot
    changes: SnapshotChanges
    applied: bool


@dataclass(frozen=True, slots=True)
class PushResult:
    remote_snapshot: TestcaseSnapshot
    uploaded_inputs: int
    uploaded_outputs: int
    pruned: int


def _remote_names(names: list[str], *, side: Which) -> tuple[str, ...]:
    validated = tuple(
        validate_testcase_name(name, label=f"remote {side.value} name") for name in names
    )
    if len(validated) != len(set(validated)):
        raise ValidationError(f"remote {side.value} testcase list contains duplicate names")
    if len(validated) != len({name.casefold() for name in validated}):
        raise ValidationError(
            f"remote {side.value} testcase list contains case-insensitive duplicates"
        )
    return tuple(sorted(validated))


def _paired_remote_names(client: TestcaseAPI, problem_id: int) -> tuple[str, ...]:
    inputs = _remote_names(client.list_testcases(problem_id, Which.IN), side=Which.IN)
    outputs = _remote_names(client.list_testcases(problem_id, Which.OUT), side=Which.OUT)
    input_set = set(inputs)
    output_set = set(outputs)
    if input_set != output_set:
        missing_outputs = sorted(input_set - output_set)
        missing_inputs = sorted(output_set - input_set)
        details: list[str] = []
        if missing_outputs:
            details.append("missing remote outputs for " + ", ".join(missing_outputs))
        if missing_inputs:
            details.append("missing remote inputs for " + ", ".join(missing_inputs))
        raise ValidationError("remote testcase names differ: " + "; ".join(details))
    return inputs


def _fetch_remote_case(
    client: TestcaseAPI,
    problem_id: int,
    name: str,
) -> TestCaseData:
    input_data = client.get_testcase(problem_id, Which.IN, name)
    output_data = client.get_testcase(problem_id, Which.OUT, name)
    if not isinstance(input_data, bytes) or not isinstance(output_data, bytes):
        raise ValidationError(f"remote testcase {name!r} did not return raw bytes")
    return TestCaseData(name, input_data, output_data)


def fetch_remote_snapshot(client: TestcaseAPI, problem_id: int) -> TestcaseSnapshot:
    """Fetch a byte-exact remote snapshot into memory."""

    return {
        name: _fetch_remote_case(client, problem_id, name)
        for name in _paired_remote_names(client, problem_id)
    }


def fetch_remote_snapshot_to(
    client: TestcaseAPI,
    problem_id: int,
    directory: str | Path,
) -> TestcaseSnapshot:
    """Fetch a snapshot to an empty staging directory and return a lazy mapping."""

    root = Path(directory)
    try:
        if root.is_symlink():
            raise FileOperationError(f"invalid testcase staging directory: {display_path(root)}")
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise FileOperationError(f"invalid testcase staging directory: {display_path(root)}")
        if any(root.iterdir()):
            raise FileOperationError(
                f"testcase staging directory is not empty: {display_path(root)}"
            )
    except FileOperationError:
        raise
    except OSError as exc:
        raise FileOperationError(
            f"could not prepare testcase staging directory {display_path(root)}: {exc}"
        ) from exc

    names = _paired_remote_names(client, problem_id)
    written: list[Path] = []
    try:
        for name in names:
            case = _fetch_remote_case(client, problem_id, name)
            input_path, output_path = local_testcase_paths(root, name)
            atomic_write_bytes(input_path, case.input, create_parents=False)
            written.append(input_path)
            atomic_write_bytes(output_path, case.output, create_parents=False)
            written.append(output_path)
    except BaseException:
        for path in reversed(written):
            with suppress(OSError):
                path.unlink(missing_ok=True)
        raise
    return _StagedTestcaseSnapshot(root, names)


def compare_snapshots(
    local: Mapping[str, TestCaseData],
    remote: Mapping[str, TestCaseData],
    *,
    incomplete: Iterable[str] = (),
) -> SnapshotChanges:
    """Compare snapshots in the pull direction (remote overwrites local)."""

    local_names = set(local)
    remote_names = set(remote)
    incomplete_names = {
        validate_testcase_name(name, label="incomplete local testcase name") for name in incomplete
    }
    return SnapshotChanges(
        added=tuple(sorted(remote_names - local_names - incomplete_names)),
        changed=tuple(
            sorted(
                {name for name in remote_names & local_names if remote[name] != local[name]}
                | (remote_names & incomplete_names)
            )
        ),
        removed=tuple(sorted((local_names | incomplete_names) - remote_names)),
    )


def _is_case_path(path: Path) -> bool:
    return path.name.endswith(".in") or path.name.endswith(".diff")


def _validated_snapshot_names(snapshot: Mapping[str, TestCaseData]) -> tuple[str, ...]:
    validated: list[str] = []
    spellings: dict[str, str] = {}
    for name, case in snapshot.items():
        validate_testcase_name(name)
        previous = spellings.setdefault(name.casefold(), name)
        if previous != name:
            raise ValidationError(
                f"case-insensitive testcase name collision: {previous!r} and {name!r}"
            )
        if not isinstance(case, TestCaseData):
            raise ValidationError(f"snapshot entry {name!r} is not TestCaseData")
        if case.name != name:
            raise ValidationError(
                f"snapshot key {name!r} does not match testcase name {case.name!r}"
            )
        validated.append(name)
    return tuple(sorted(validated))


def replace_local_snapshot(
    directory: str | Path,
    snapshot: Mapping[str, TestCaseData],
) -> None:
    """Atomically replace direct testcase files while preserving every other artifact."""

    target = Path(directory)
    parent = target.parent
    case_names = _validated_snapshot_names(snapshot)
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise FileOperationError(f"invalid testcase parent: {display_path(parent)}")
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise FileOperationError(f"invalid testcase directory: {display_path(target)}")
    except FileOperationError:
        raise
    except OSError as exc:
        raise FileOperationError(f"could not prepare {display_path(target)}: {exc}") from exc

    stage = Path(tempfile.mkdtemp(dir=parent, prefix=f".{target.name}.stage-"))
    backup = Path(tempfile.mkdtemp(dir=parent, prefix=f".{target.name}.backup-"))
    backup.rmdir()
    try:
        if target.exists():
            stage.rmdir()
            shutil.copytree(target, stage, symlinks=True)
        for entry in stage.iterdir():
            if not _is_case_path(entry):
                continue
            if entry.is_dir() and not entry.is_symlink():
                # Directories are artifacts even when their name has a case suffix.
                continue
            entry.unlink()
        for name in case_names:
            case = snapshot[name]
            input_path, output_path = local_testcase_paths(stage, name)
            atomic_write_bytes(input_path, case.input, create_parents=False)
            atomic_write_bytes(output_path, case.output, create_parents=False)
        if target.exists():
            os.replace(target, backup)
        os.replace(stage, target)
        if backup.exists():
            discard_tree(backup)
    except BaseException as exc:
        # The stage disappears only when the final atomic replace commits.
        if not stage.exists() and target.exists():
            if backup.exists():
                discard_tree(backup)
            return
        rollback_error: OSError | None = None
        if not target.exists() and backup.exists():
            try:
                os.replace(backup, target)
            except OSError as rollback_exc:
                rollback_error = rollback_exc
        if not isinstance(exc, Exception) and rollback_error is None:
            raise
        detail = f"; rollback also failed: {rollback_error}" if rollback_error else ""
        raise FileOperationError(
            f"could not replace testcase snapshot {display_path(target)}: {exc}{detail}"
        ) from exc
    finally:
        committed = not stage.exists() and target.exists()
        if stage.exists():
            discard_tree(stage)
        if backup.exists() and committed:
            discard_tree(backup)


def pull_testcases(
    client: TestcaseAPI,
    problem_id: int,
    directory: str | Path,
    *,
    confirm: ConfirmReplacement | None = None,
) -> PullResult:
    """Fetch and optionally replace local cases; missing output is initialized directly."""

    target = Path(directory)
    existed = target.is_dir() and not target.is_symlink()
    local: TestcaseSnapshot = {}
    incomplete: tuple[str, ...] = ()
    if existed:
        local, missing_outputs, missing_inputs = inspect_testcases(target)
        incomplete = missing_outputs + missing_inputs
    remote = fetch_remote_snapshot(client, problem_id)
    changes = compare_snapshots(local, remote, incomplete=incomplete)
    if not changes.has_changes:
        return PullResult(remote, changes, applied=False)
    if existed:
        if confirm is None:
            raise ConflictError(
                "remote testcases differ from rime-out and replacement needs confirmation"
            )
        if not confirm(changes):
            return PullResult(remote, changes, applied=False)
    replace_local_snapshot(target, remote)
    return PullResult(remote, changes, applied=True)


def estimate_upload_size(name: str, content: bytes) -> int:
    """Conservatively estimate one encoded multipart file's contribution."""

    validate_testcase_name(name)
    if not isinstance(content, bytes):
        raise ValidationError(f"testcase {name!r} content must be bytes")
    return len(name.encode("ascii")) + len(content) + MULTIPART_FILE_OVERHEAD


def batch_upload_files(
    files: Mapping[str, bytes],
    *,
    max_files: int = MAX_UPLOAD_FILES,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> tuple[dict[str, bytes], ...]:
    """Split deterministic multipart batches by both file count and estimated bytes."""

    if max_files < 1 or max_bytes < 1:
        raise ValueError("batch limits must be positive")
    batches: list[dict[str, bytes]] = []
    current: dict[str, bytes] = {}
    current_size = 0
    for name in sorted(files):
        content = files[name]
        size = estimate_upload_size(name, content)
        if current and (len(current) >= max_files or current_size + size > max_bytes):
            batches.append(current)
            current = {}
            current_size = 0
        current[name] = content
        current_size += size
    if current:
        batches.append(current)
    return tuple(batches)


def push_testcases(
    client: TestcaseAPI,
    problem_id: int,
    directory: str | Path,
    *,
    prune: bool = False,
) -> PushResult:
    """Upload changed pairs, optionally prune stale remote pairs, then refresh local bytes."""

    target = Path(directory)
    local = read_testcases(target, require_nonempty=True)
    remote = fetch_remote_snapshot(client, problem_id)
    input_uploads = {
        name: case.input
        for name, case in local.items()
        if name not in remote or remote[name].input != case.input
    }
    output_uploads = {
        name: case.output
        for name, case in local.items()
        if name not in remote or remote[name].output != case.output
    }
    for batch in batch_upload_files(input_uploads):
        client.upload_testcases(problem_id, Which.IN, batch)
    for batch in batch_upload_files(output_uploads):
        client.upload_testcases(problem_id, Which.OUT, batch)

    removed = tuple(sorted(set(remote) - set(local))) if prune else ()
    for name in removed:
        client.delete_testcase(problem_id, Which.IN, name)
        client.delete_testcase(problem_id, Which.OUT, name)

    normalized = fetch_remote_snapshot(client, problem_id)
    uploaded_input_names = set(input_uploads)
    uploaded_output_names = set(output_uploads)
    uploaded_names = uploaded_input_names | uploaded_output_names
    missing = uploaded_names - set(normalized)
    if missing:
        raise ValidationError(
            "server normalization response omitted uploaded testcases: "
            + ", ".join(sorted(missing))
        )
    refreshed = dict(local)
    for name in uploaded_names:
        local_case = local[name]
        normalized_case = normalized[name]
        refreshed[name] = TestCaseData(
            name,
            normalized_case.input if name in uploaded_input_names else local_case.input,
            (normalized_case.output if name in uploaded_output_names else local_case.output),
        )
    replace_local_snapshot(target, refreshed)
    return PushResult(
        normalized,
        uploaded_inputs=len(input_uploads),
        uploaded_outputs=len(output_uploads),
        pruned=len(removed),
    )


__all__ = [
    "MAX_UPLOAD_BYTES",
    "MAX_UPLOAD_FILES",
    "MULTIPART_FILE_OVERHEAD",
    "ConfirmReplacement",
    "PullResult",
    "PushResult",
    "SnapshotChanges",
    "TestcaseAPI",
    "TestcaseSnapshot",
    "batch_upload_files",
    "compare_snapshots",
    "estimate_upload_size",
    "fetch_remote_snapshot",
    "fetch_remote_snapshot_to",
    "pull_testcases",
    "push_testcases",
    "replace_local_snapshot",
]
