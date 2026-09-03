"""Transactional synchronization of remote testcases and Rime output files."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from yukitools_rime.errors import ConflictError, FileOperationError, ValidationError
from yukitools_rime.files import atomic_write_bytes, display_path
from yukitools_rime.layout import TestCaseData, local_testcase_paths, read_testcases
from yukitools_rime.models import Which, validate_testcase_name

MAX_UPLOAD_FILES = 100
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MULTIPART_FILE_OVERHEAD = 512

TestcaseSnapshot = dict[str, TestCaseData]


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
    return tuple(sorted(validated))


def fetch_remote_snapshot(client: TestcaseAPI, problem_id: int) -> TestcaseSnapshot:
    """List both sides, require an exact name match, then fetch byte-exact bodies."""

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
    snapshot: TestcaseSnapshot = {}
    for name in inputs:
        input_data = client.get_testcase(problem_id, Which.IN, name)
        output_data = client.get_testcase(problem_id, Which.OUT, name)
        if not isinstance(input_data, bytes) or not isinstance(output_data, bytes):
            raise ValidationError(f"remote testcase {name!r} did not return raw bytes")
        snapshot[name] = TestCaseData(name, input_data, output_data)
    return snapshot


def compare_snapshots(
    local: Mapping[str, TestCaseData],
    remote: Mapping[str, TestCaseData],
) -> SnapshotChanges:
    """Compare snapshots in the pull direction (remote overwrites local)."""

    local_names = set(local)
    remote_names = set(remote)
    return SnapshotChanges(
        added=tuple(sorted(remote_names - local_names)),
        changed=tuple(
            sorted(name for name in remote_names & local_names if remote[name] != local[name])
        ),
        removed=tuple(sorted(local_names - remote_names)),
    )


def _is_case_path(path: Path) -> bool:
    return path.name.endswith(".in") or path.name.endswith(".diff")


def _validated_snapshot(snapshot: Mapping[str, TestCaseData]) -> TestcaseSnapshot:
    validated: TestcaseSnapshot = {}
    for name, case in snapshot.items():
        validate_testcase_name(name)
        if not isinstance(case, TestCaseData):
            raise ValidationError(f"snapshot entry {name!r} is not TestCaseData")
        if case.name != name:
            raise ValidationError(
                f"snapshot key {name!r} does not match testcase name {case.name!r}"
            )
        validated[name] = case
    return validated


def replace_local_snapshot(
    directory: str | Path,
    snapshot: Mapping[str, TestCaseData],
) -> None:
    """Atomically replace direct testcase files while preserving every other artifact."""

    target = Path(directory)
    parent = target.parent
    cases = _validated_snapshot(snapshot)
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
    moved_old = False
    installed_new = False
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
        for name, case in cases.items():
            input_path, output_path = local_testcase_paths(stage, name)
            atomic_write_bytes(input_path, case.input, create_parents=False)
            atomic_write_bytes(output_path, case.output, create_parents=False)
        if target.exists():
            os.replace(target, backup)
            moved_old = True
        os.replace(stage, target)
        installed_new = True
        if moved_old:
            shutil.rmtree(backup)
            moved_old = False
    except (OSError, FileOperationError) as exc:
        rollback_error: OSError | None = None
        if installed_new:
            try:
                shutil.rmtree(target)
                installed_new = False
            except OSError as rollback_exc:
                rollback_error = rollback_exc
        if moved_old and not target.exists():
            try:
                os.replace(backup, target)
                moved_old = False
            except OSError as rollback_exc:
                rollback_error = rollback_error or rollback_exc
        detail = f"; rollback also failed: {rollback_error}" if rollback_error else ""
        raise FileOperationError(
            f"could not replace testcase snapshot {display_path(target)}: {exc}{detail}"
        ) from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if backup.exists() and not moved_old:
            shutil.rmtree(backup, ignore_errors=True)


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
    local = read_testcases(target, require_nonempty=False) if existed else {}
    remote = fetch_remote_snapshot(client, problem_id)
    changes = compare_snapshots(local, remote)
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
        if size > max_bytes:
            raise ValidationError(
                f"testcase {name!r} exceeds the estimated upload limit ({max_bytes} bytes)"
            )
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
    missing = set(local) - set(normalized)
    if missing:
        raise ValidationError(
            "server normalization response omitted local testcases: "
            + ", ".join(sorted(missing))
        )
    replace_local_snapshot(
        target,
        {name: normalized[name] for name in local},
    )
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
    "pull_testcases",
    "push_testcases",
    "replace_local_snapshot",
]
