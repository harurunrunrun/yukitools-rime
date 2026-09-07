"""Transactional synchronization of remote testcases and Rime output files."""

from __future__ import annotations

import hashlib
import os
import shutil
import string
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from yukitools_rime.errors import ConflictError, FileOperationError, ValidationError
from yukitools_rime.files import (
    _prepare_directory_swap,
    atomic_write_bytes,
    discard_tree,
    display_path,
    read_bytes,
)
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
DEFAULT_SERVER_CASE_CHARS = string.ascii_letters + "._-" + string.digits

TestcaseSnapshot = Mapping[str, TestCaseData]


@dataclass(frozen=True, slots=True)
class RemoteTestcaseSides:
    """A byte-exact remote snapshot that can represent an incomplete pair."""

    inputs: Mapping[str, bytes]
    outputs: Mapping[str, bytes]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.inputs) | frozenset(self.outputs)

    @property
    def complete_names(self) -> frozenset[str]:
        return frozenset(self.inputs) & frozenset(self.outputs)

    def require_complete(
        self,
        names: Iterable[str] | None = None,
    ) -> dict[str, TestCaseData]:
        """Return requested pairs, rejecting any side that is absent."""

        required = self.names if names is None else frozenset(names)
        missing_outputs = sorted(required - set(self.outputs))
        missing_inputs = sorted(required - set(self.inputs))
        if missing_outputs or missing_inputs:
            details: list[str] = []
            if missing_outputs:
                details.append("missing remote outputs for " + ", ".join(missing_outputs))
            if missing_inputs:
                details.append("missing remote inputs for " + ", ".join(missing_inputs))
            raise ValidationError("remote testcase names differ: " + "; ".join(details))
        return {
            name: TestCaseData(name, self.inputs[name], self.outputs[name])
            for name in sorted(required)
        }

    def complete_snapshot(self) -> dict[str, TestCaseData]:
        """Return complete pairs while ignoring unrelated incomplete remote cases."""

        return {
            name: TestCaseData(name, self.inputs[name], self.outputs[name])
            for name in sorted(self.complete_names)
        }


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


def _discard_staged_children(root: Path) -> None:
    """Best-effort cleanup without masking the original staging failure."""

    with suppress(BaseException):
        children = tuple(root.iterdir())
        for path in children:
            with suppress(BaseException):
                if path.is_dir() and not path.is_symlink():
                    discard_tree(path)
                else:
                    path.unlink(missing_ok=True)


class TestcaseAPI(Protocol):
    """The small YukicoderClient surface needed by testcase synchronization."""

    def list_testcases(self, problem_id: int, which: Which | str) -> list[str]: ...

    def list_testcases_detail(self, problem_id: int, which: Which | str) -> Sequence[object]: ...

    def testcase_name_rule(self) -> str: ...

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
    """Push outcome; the snapshot contains synchronized local testcase names only."""

    remote_snapshot: TestcaseSnapshot
    uploaded_inputs: int
    uploaded_outputs: int
    pruned: int


@dataclass(frozen=True, slots=True)
class RemoteTestcaseHashes:
    inputs: Mapping[str, str]
    outputs: Mapping[str, str]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.inputs) | frozenset(self.outputs)

    @property
    def complete_names(self) -> frozenset[str]:
        return frozenset(self.inputs) & frozenset(self.outputs)

    def require_complete(self) -> tuple[str, ...]:
        return _require_paired_remote_names(
            tuple(sorted(self.inputs)),
            tuple(sorted(self.outputs)),
        )

    def matches(self, which: Which, name: str, content: bytes) -> bool:
        """Return whether raw bytes have the digest reported for one remote side."""

        hashes = self.inputs if which is Which.IN else self.outputs
        return hashes.get(name) == _sha256(content)


def _server_allowed_chars(client: TestcaseAPI) -> str:
    getter = getattr(client, "testcase_name_rule", None)
    if not callable(getter):
        return DEFAULT_SERVER_CASE_CHARS
    allowed = getter()
    if not isinstance(allowed, str) or not allowed:
        raise ValidationError("testcase name rule did not return a non-empty string")
    return allowed


def _validate_server_name(name: str, allowed_chars: str, *, label: str) -> str:
    validate_testcase_name(name, label=label)
    converted = "".join(character for character in name if character in allowed_chars)
    if converted != name:
        replacement = repr(converted) if converted else "an empty name"
        raise ValidationError(f"{label} {name!r} would be stored by yukicoder as {replacement}")
    return name


def validate_testcase_names_for_server(
    client: TestcaseAPI,
    names: Iterable[str],
) -> None:
    """Reject names that the server would silently rewrite."""

    allowed = _server_allowed_chars(client)
    for name in names:
        _validate_server_name(name, allowed, label="testcase name")


def validate_snapshot_names_for_server(
    client: TestcaseAPI,
    snapshot: Mapping[str, TestCaseData],
) -> None:
    """Reject names that the server would silently rewrite before uploading."""

    validate_testcase_names_for_server(client, snapshot)


def _detail_map(raw: object, *, side: Which) -> dict[str, str]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValidationError(f"remote {side.value} testcase details are not a sequence")
    result: dict[str, str] = {}
    spellings: dict[str, str] = {}
    for item in raw:
        name = getattr(item, "name", None)
        digest = getattr(item, "sha256", None)
        if not isinstance(name, str) or not isinstance(digest, str):
            raise ValidationError(f"remote {side.value} testcase details have an invalid entry")
        validate_testcase_name(name, label=f"remote {side.value} name")
        if len(digest) != 64 or any(character not in string.hexdigits for character in digest):
            raise ValidationError(f"remote {side.value} testcase {name!r} has an invalid sha256")
        if name in result:
            raise ValidationError(f"remote {side.value} testcase details contain duplicate names")
        previous = spellings.setdefault(name.casefold(), name)
        if previous != name:
            raise ValidationError(
                f"remote {side.value} testcase details contain case-insensitive duplicates"
            )
        result[name] = digest.lower()
    return result


def fetch_remote_hashes(
    client: TestcaseAPI,
    problem_id: int,
) -> RemoteTestcaseHashes | None:
    """Fetch side-specific SHA-256 maps when the client supports detail listings."""

    getter = getattr(client, "list_testcases_detail", None)
    if not callable(getter):
        return None
    return RemoteTestcaseHashes(
        _detail_map(getter(problem_id, Which.IN), side=Which.IN),
        _detail_map(getter(problem_id, Which.OUT), side=Which.OUT),
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _reuse_or_fetch_side(
    client: TestcaseAPI,
    problem_id: int,
    which: Which,
    details: Mapping[str, str],
    reuse: Mapping[str, TestCaseData],
) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for name, digest in details.items():
        previous = reuse.get(name)
        candidate = None
        if previous is not None:
            candidate = previous.input if which is Which.IN else previous.output
        if candidate is not None and _sha256(candidate) == digest:
            result[name] = candidate
            continue
        data = client.get_testcase(problem_id, which, name)
        if not isinstance(data, bytes):
            raise ValidationError(f"remote testcase {name!r} did not return raw bytes")
        if _sha256(data) != digest:
            raise ValidationError(
                f"remote {which.value} testcase {name!r} does not match its sha256"
            )
        result[name] = data
    return result


def _reuse_or_fetch_body(
    client: TestcaseAPI,
    problem_id: int,
    which: Which,
    name: str,
    *,
    digest: str | None,
    candidate: bytes | None,
) -> bytes:
    if digest is not None and candidate is not None and _sha256(candidate) == digest:
        return candidate
    data = client.get_testcase(problem_id, which, name)
    if not isinstance(data, bytes):
        raise ValidationError(f"remote testcase {name!r} did not return raw bytes")
    if digest is not None and _sha256(data) != digest:
        raise ValidationError(f"remote {which.value} testcase {name!r} does not match its sha256")
    return data


def _requested_remote_names(names: Iterable[str] | None) -> frozenset[str] | None:
    if names is None:
        return None
    return frozenset(
        validate_testcase_name(name, label="requested remote testcase name") for name in names
    )


def _select_remote_names(
    names: Iterable[str],
    requested: frozenset[str] | None,
) -> tuple[str, ...]:
    return tuple(names) if requested is None else tuple(name for name in names if name in requested)


def _select_remote_details(
    details: Mapping[str, str],
    requested: frozenset[str] | None,
) -> Mapping[str, str]:
    if requested is None:
        return details
    return {name: digest for name, digest in details.items() if name in requested}


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


def _remote_side_names(
    client: TestcaseAPI,
    problem_id: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    inputs = _remote_names(client.list_testcases(problem_id, Which.IN), side=Which.IN)
    outputs = _remote_names(client.list_testcases(problem_id, Which.OUT), side=Which.OUT)
    return inputs, outputs


def _require_paired_remote_names(
    inputs: tuple[str, ...],
    outputs: tuple[str, ...],
) -> tuple[str, ...]:
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


def _paired_remote_names(client: TestcaseAPI, problem_id: int) -> tuple[str, ...]:
    return _require_paired_remote_names(*_remote_side_names(client, problem_id))


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


def fetch_remote_snapshot(
    client: TestcaseAPI,
    problem_id: int,
    *,
    reuse: Mapping[str, TestCaseData] | None = None,
) -> TestcaseSnapshot:
    """Fetch a byte-exact remote snapshot, reusing hash-identical local bytes."""

    details = fetch_remote_hashes(client, problem_id)
    if details is None:
        return {
            name: _fetch_remote_case(client, problem_id, name)
            for name in _paired_remote_names(client, problem_id)
        }
    names = details.require_complete()
    reusable = reuse or {}
    inputs = _reuse_or_fetch_side(client, problem_id, Which.IN, details.inputs, reusable)
    outputs = _reuse_or_fetch_side(client, problem_id, Which.OUT, details.outputs, reusable)
    return {name: TestCaseData(name, inputs[name], outputs[name]) for name in names}


def fetch_remote_sides(
    client: TestcaseAPI,
    problem_id: int,
    *,
    reuse: Mapping[str, TestCaseData] | None = None,
    names: Iterable[str] | None = None,
) -> RemoteTestcaseSides:
    """Fetch selected sides, reusing hash-identical local bytes when possible."""

    requested = _requested_remote_names(names)
    details = fetch_remote_hashes(client, problem_id)
    if details is not None:
        reusable = reuse or {}
        input_details = _select_remote_details(details.inputs, requested)
        output_details = _select_remote_details(details.outputs, requested)
        return RemoteTestcaseSides(
            _reuse_or_fetch_side(client, problem_id, Which.IN, input_details, reusable),
            _reuse_or_fetch_side(client, problem_id, Which.OUT, output_details, reusable),
        )

    input_names, output_names = _remote_side_names(client, problem_id)
    input_names = _select_remote_names(input_names, requested)
    output_names = _select_remote_names(output_names, requested)

    def fetch(which: Which, names: tuple[str, ...]) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for name in names:
            data = client.get_testcase(problem_id, which, name)
            if not isinstance(data, bytes):
                raise ValidationError(f"remote testcase {name!r} did not return raw bytes")
            result[name] = data
        return result

    return RemoteTestcaseSides(
        fetch(Which.IN, input_names),
        fetch(Which.OUT, output_names),
    )


def fetch_remote_snapshot_to(
    client: TestcaseAPI,
    problem_id: int,
    directory: str | Path,
    *,
    reuse: Mapping[str, TestCaseData] | None = None,
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

    details = fetch_remote_hashes(client, problem_id)
    if details is None:
        names = _paired_remote_names(client, problem_id)
    else:
        names = details.require_complete()
    reusable = reuse or {}
    try:
        for name in names:
            previous = reusable.get(name)
            input_digest = None if details is None else details.inputs[name]
            output_digest = None if details is None else details.outputs[name]
            input_candidate = None if previous is None else previous.input
            output_candidate = None if previous is None else previous.output
            input_path, output_path = local_testcase_paths(root, name)
            input_data = _reuse_or_fetch_body(
                client,
                problem_id,
                Which.IN,
                name,
                digest=input_digest,
                candidate=input_candidate,
            )
            atomic_write_bytes(input_path, input_data, create_parents=False)
            output_data = _reuse_or_fetch_body(
                client,
                problem_id,
                Which.OUT,
                name,
                digest=output_digest,
                candidate=output_candidate,
            )
            atomic_write_bytes(output_path, output_data, create_parents=False)
    except BaseException:
        # The directory was required to be empty, so every direct child belongs
        # to this attempt.  Clean it even when a mocked writer mutates then raises.
        _discard_staged_children(root)
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
    missing_parents: list[Path] = []
    cursor = parent
    while not cursor.exists():
        missing_parents.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent

    stage: Path | None = None
    backup: Path | None = None
    committed = False
    try:
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

        stage, backup = _prepare_directory_swap(
            parent, target.name, label=f"testcase snapshot for {display_path(target)}"
        )
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
        committed = True
        if backup.exists():
            discard_tree(backup)
    except BaseException as exc:
        if stage is None or backup is None:
            raise
        # The stage disappears only when the final atomic replace commits.
        if not stage.exists() and target.exists():
            committed = True
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
        if stage is not None and stage.exists():
            discard_tree(stage)
        if backup is not None and backup.exists() and committed:
            discard_tree(backup)
        if not committed:
            # mkdir(parents=True) may have created more than the immediate parent.
            for created in missing_parents:
                with suppress(OSError):
                    created.rmdir()


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
    remote = fetch_remote_snapshot(client, problem_id, reuse=local)
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
    validate_snapshot_names_for_server(client, local)
    details = fetch_remote_hashes(client, problem_id)
    if details is None:
        remote = fetch_remote_sides(client, problem_id)
        input_uploads = {
            name: case.input
            for name, case in local.items()
            if name not in remote.inputs or remote.inputs[name] != case.input
        }
        output_uploads = {
            name: case.output
            for name, case in local.items()
            if name not in remote.outputs or remote.outputs[name] != case.output
        }
        remote_input_names = set(remote.inputs)
        remote_output_names = set(remote.outputs)
    else:
        input_uploads = {
            name: case.input
            for name, case in local.items()
            if details.inputs.get(name) != _sha256(case.input)
        }
        output_uploads = {
            name: case.output
            for name, case in local.items()
            if details.outputs.get(name) != _sha256(case.output)
        }
        remote_input_names = set(details.inputs)
        remote_output_names = set(details.outputs)
    for batch in batch_upload_files(input_uploads):
        client.upload_testcases(problem_id, Which.IN, batch)
    for batch in batch_upload_files(output_uploads):
        client.upload_testcases(problem_id, Which.OUT, batch)

    stale_inputs = tuple(sorted(remote_input_names - set(local))) if prune else ()
    stale_outputs = tuple(sorted(remote_output_names - set(local))) if prune else ()
    for name in stale_inputs:
        client.delete_testcase(problem_id, Which.IN, name)
    for name in stale_outputs:
        client.delete_testcase(problem_id, Which.OUT, name)

    uploaded_input_names = set(input_uploads)
    uploaded_output_names = set(output_uploads)
    uploaded_names = uploaded_input_names | uploaded_output_names
    refreshed = dict(local)
    if uploaded_names:
        normalized = fetch_remote_sides(
            client,
            problem_id,
            reuse=local,
            names=uploaded_names,
        )
        normalized_uploaded = normalized.require_complete(uploaded_names)
        for name in uploaded_names:
            local_case = local[name]
            normalized_case = normalized_uploaded[name]
            refreshed[name] = TestCaseData(
                name,
                normalized_case.input if name in uploaded_input_names else local_case.input,
                (normalized_case.output if name in uploaded_output_names else local_case.output),
            )
    replace_local_snapshot(target, refreshed)
    return PushResult(
        refreshed,
        uploaded_inputs=len(input_uploads),
        uploaded_outputs=len(output_uploads),
        pruned=len(set(stale_inputs) | set(stale_outputs)),
    )


__all__ = [
    "DEFAULT_SERVER_CASE_CHARS",
    "MAX_UPLOAD_BYTES",
    "MAX_UPLOAD_FILES",
    "MULTIPART_FILE_OVERHEAD",
    "ConfirmReplacement",
    "PullResult",
    "PushResult",
    "RemoteTestcaseHashes",
    "RemoteTestcaseSides",
    "SnapshotChanges",
    "TestcaseAPI",
    "TestcaseSnapshot",
    "batch_upload_files",
    "compare_snapshots",
    "estimate_upload_size",
    "fetch_remote_hashes",
    "fetch_remote_sides",
    "fetch_remote_snapshot",
    "fetch_remote_snapshot_to",
    "pull_testcases",
    "push_testcases",
    "replace_local_snapshot",
    "validate_snapshot_names_for_server",
    "validate_testcase_names_for_server",
]
