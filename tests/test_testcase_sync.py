from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from yukitools_rime import files as files_module
from yukitools_rime import testcase_sync as testcase_sync_module
from yukitools_rime.errors import ConflictError, FileOperationError, ValidationError
from yukitools_rime.layout import TestCaseData as CaseData
from yukitools_rime.models import Which
from yukitools_rime.testcase_sync import (
    MULTIPART_FILE_OVERHEAD,
    SnapshotChanges,
    batch_upload_files,
    compare_snapshots,
    fetch_remote_sides,
    fetch_remote_snapshot,
    fetch_remote_snapshot_to,
    pull_testcases,
    push_testcases,
    replace_local_snapshot,
)


class FakeAPI:
    def __init__(
        self,
        inputs: Mapping[str, bytes] | None = None,
        outputs: Mapping[str, bytes] | None = None,
        *,
        normalize: bool = False,
    ) -> None:
        self.data = {"in": dict(inputs or {}), "out": dict(outputs or {})}
        self.normalize = normalize
        self.uploads: list[tuple[str, tuple[str, ...]]] = []
        self.deletes: list[tuple[str, str]] = []
        self.gets: list[tuple[str, str]] = []
        self.list_calls = 0

    @staticmethod
    def side(which: Which | str) -> str:
        return which.value if isinstance(which, Which) else which

    def list_testcases(self, problem_id: int, which: Which | str) -> list[str]:
        assert problem_id == 42
        self.list_calls += 1
        return list(reversed(self.data[self.side(which)]))

    def get_testcase(self, problem_id: int, which: Which | str, name: str) -> bytes:
        assert problem_id == 42
        side = self.side(which)
        self.gets.append((side, name))
        return self.data[side][name]

    def upload_testcases(
        self, problem_id: int, which: Which | str, files: Mapping[str, bytes]
    ) -> object:
        assert problem_id == 42
        side = self.side(which)
        self.uploads.append((side, tuple(files)))
        for name, content in files.items():
            normalized = content.replace(b"\r\n", b"\n") if self.normalize else content
            self.data[side][name] = normalized
        return object()

    def delete_testcase(self, problem_id: int, which: Which | str, name: str) -> None:
        assert problem_id == 42
        side = self.side(which)
        self.deletes.append((side, name))
        del self.data[side][name]


def case(name: str, input_data: bytes = b"in", output_data: bytes = b"out") -> CaseData:
    return CaseData(name, input_data, output_data)


def write_case(directory: Path, value: CaseData) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{value.name}.in").write_bytes(value.input)
    (directory / f"{value.name}.diff").write_bytes(value.output)


def test_case_data_requires_raw_bytes() -> None:
    with pytest.raises(ValidationError, match="input must be bytes"):
        CaseData("sample", "text", b"output")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="output must be bytes"):
        CaseData("sample", b"input", "text")  # type: ignore[arg-type]


def test_fetch_requires_exact_unique_names_and_reads_raw_bytes() -> None:
    api = FakeAPI({"b.txt": b"bi", "a.txt": b"ai"}, {"a.txt": b"ao", "b.txt": b"bo"})
    snapshot = fetch_remote_snapshot(api, 42)
    assert list(snapshot) == ["a.txt", "b.txt"]
    assert snapshot["a.txt"] == case("a.txt", b"ai", b"ao")
    assert api.gets == [("in", "a.txt"), ("out", "a.txt"), ("in", "b.txt"), ("out", "b.txt")]
    with pytest.raises(ValidationError, match="names differ"):
        fetch_remote_snapshot(FakeAPI({"a": b"x"}, {"b": b"x"}), 42)
    duplicate = FakeAPI({"a": b"x"}, {"a": b"y"})
    duplicate.list_testcases = lambda _pid, _which: ["a", "a"]  # type: ignore[method-assign]
    with pytest.raises(ValidationError, match="duplicate"):
        fetch_remote_snapshot(duplicate, 42)


def test_fetch_rejects_unsafe_names_but_preserves_empty_remote_bytes() -> None:
    with pytest.raises(ValidationError):
        fetch_remote_snapshot(FakeAPI({"bad name": b"x"}, {"bad name": b"y"}), 42)
    snapshot = fetch_remote_snapshot(FakeAPI({"a": b""}, {"a": b"y"}), 42)
    assert snapshot["a"] == case("a", b"", b"y")


def test_fetch_to_stages_exact_bytes_and_reads_values_lazily(tmp_path: Path) -> None:
    raw_input = b"input\x00\xff\r\n"
    raw_output = b"output\xfe\x00\n"
    api = FakeAPI(
        {"sample.01": raw_input},
        {"sample.01": raw_output},
    )
    stage = tmp_path / "stage"
    stage.mkdir()

    snapshot = fetch_remote_snapshot_to(api, 42, stage)

    staged_files = tuple(path for path in stage.rglob("*") if path.is_file())
    staged_contents = {path.read_bytes(): path for path in staged_files}
    assert raw_input in staged_contents
    assert raw_output in staged_contents
    assert tuple(snapshot) == ("sample.01",)
    assert api.gets == [("in", "sample.01"), ("out", "sample.01")]

    changed_input = b"changed only on disk\x00"
    staged_contents[raw_input].write_bytes(changed_input)
    assert snapshot["sample.01"] == case("sample.01", changed_input, raw_output)


@pytest.mark.parametrize(
    ("input_names", "output_names", "message"),
    [
        (("../escape",), ("../escape",), "remote"),
        (("same", "same"), ("same", "same"), "duplicate"),
        (("Case", "case"), ("Case", "case"), "case-insensitive"),
        (("input_only",), ("output_only",), "names differ"),
    ],
)
def test_fetch_to_validates_all_remote_names_before_downloading_bodies(
    tmp_path: Path,
    input_names: tuple[str, ...],
    output_names: tuple[str, ...],
    message: str,
) -> None:
    api = FakeAPI()

    def list_names(_problem_id: int, which: Which | str) -> list[str]:
        return list(input_names if FakeAPI.side(which) == "in" else output_names)

    api.list_testcases = list_names  # type: ignore[method-assign]
    stage = tmp_path / "stage"
    stage.mkdir()

    with pytest.raises(ValidationError, match=message):
        fetch_remote_snapshot_to(api, 42, stage)

    assert api.gets == []
    assert not tuple(stage.iterdir())


def test_compare_reports_remote_direction_names_and_counts() -> None:
    local = {"same": case("same"), "change": case("change"), "gone": case("gone")}
    remote = {
        "same": case("same"),
        "change": case("change", b"new"),
        "added": case("added"),
    }
    changes = compare_snapshots(local, remote)
    assert changes == SnapshotChanges(("added",), ("change",), ("gone",))
    assert changes.counts == (1, 1, 1)
    assert changes.has_changes


def test_replace_preserves_artifacts_and_removes_stale_cases(tmp_path: Path) -> None:
    target = tmp_path / "cases"
    write_case(target, case("old"))
    (target / "README").write_text("keep")
    artifact_dir = target / "cache.in"
    artifact_dir.mkdir()
    (artifact_dir / "data").write_text("keep")
    replace_local_snapshot(target, {"new.txt": case("new.txt", b"1", b"2")})
    assert not (target / "old.in").exists()
    assert (target / "new.txt.in").read_bytes() == b"1"
    assert (target / "new.txt.diff").read_bytes() == b"2"
    assert (target / "README").read_text() == "keep"
    assert (target / "cache.in" / "data").read_text() == "keep"


def test_replace_wraps_stage_allocation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cases"
    write_case(target, case("old"))

    def fail_mkdtemp(*_args: object, **_kwargs: object) -> str:
        raise PermissionError("stage denied")

    monkeypatch.setattr(files_module.tempfile, "mkdtemp", fail_mkdtemp)
    with pytest.raises(FileOperationError, match="stage denied"):
        replace_local_snapshot(target, {"new": case("new")})
    assert (target / "old.in").read_bytes() == b"in"


def test_pull_initializes_missing_without_prompt(tmp_path: Path) -> None:
    target = tmp_path / "missing"
    called = False

    def confirm(_changes: SnapshotChanges) -> bool:
        nonlocal called
        called = True
        return False

    result = pull_testcases(
        FakeAPI({"sample.txt": b"in"}, {"sample.txt": b"out"}), 42, target, confirm=confirm
    )
    assert result.applied and not called
    assert (target / "sample.txt.in").read_bytes() == b"in"


def test_pull_requires_decision_and_decline_does_not_mutate(tmp_path: Path) -> None:
    target = tmp_path / "cases"
    write_case(target, case("old"))
    api = FakeAPI({"new": b"i"}, {"new": b"o"})
    with pytest.raises(ConflictError):
        pull_testcases(api, 42, target)
    result = pull_testcases(api, 42, target, confirm=lambda _changes: False)
    assert not result.applied
    assert (target / "old.in").read_bytes() == b"in"
    assert pull_testcases(api, 42, target, confirm=lambda _changes: True).applied
    assert not (target / "old.in").exists()
    assert (target / "new.in").read_bytes() == b"i"


def test_pull_without_difference_does_not_need_prompt(tmp_path: Path) -> None:
    target = tmp_path / "cases"
    write_case(target, case("same"))
    result = pull_testcases(FakeAPI({"same": b"in"}, {"same": b"out"}), 42, target)
    assert not result.applied
    assert not result.changes.has_changes


def test_batching_honors_count_size_and_oversize() -> None:
    files = {f"{index:03}.txt": b"x" for index in range(201)}
    assert [len(batch) for batch in batch_upload_files(files)] == [100, 100, 1]
    size = MULTIPART_FILE_OVERHEAD + len("a") + 1
    assert len(batch_upload_files({"a": b"x", "b": b"x"}, max_bytes=size)) == 2
    assert batch_upload_files({"a": b"xx"}, max_bytes=size) == ({"a": b"xx"},)


@pytest.mark.parametrize("kind", ["missing", "incomplete", "nonregular"])
def test_push_rejects_invalid_local_before_api(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "cases"
    if kind == "incomplete":
        target.mkdir()
        (target / "sample.in").write_bytes(b"x")
    elif kind == "nonregular":
        (target / "sample.in").mkdir(parents=True)
    api = FakeAPI()
    with pytest.raises(Exception, match=r"testcase|\.diff"):
        push_testcases(api, 42, target)
    assert api.list_calls == 0


def test_push_uploads_prunes_and_refetches_normalized_bytes(tmp_path: Path) -> None:
    target = tmp_path / "cases"
    write_case(target, case("same", b"same-i", b"same-o"))
    write_case(target, case("change", b"new-i\r\n", b"new-o\r\n"))
    write_case(target, case("added", b"add-i\r\n", b"add-o\r\n"))
    (target / "README").write_text("artifact")
    api = FakeAPI(
        {"same": b"same-i", "change": b"old-i", "stale": b"stale-i"},
        {"same": b"same-o", "change": b"old-o", "stale": b"stale-o"},
        normalize=True,
    )
    result = push_testcases(api, 42, target, prune=True)
    assert result.uploaded_inputs == result.uploaded_outputs == 2
    assert result.pruned == 1
    assert api.deletes == [("in", "stale"), ("out", "stale")]
    assert result.remote_snapshot["change"].input == b"new-i\n"
    assert (target / "change.in").read_bytes() == b"new-i\n"
    assert (target / "README").read_text() == "artifact"
    assert not (target / "stale.in").exists()
    assert api.list_calls == 4


def test_push_without_prune_does_not_import_remote_only_case(tmp_path: Path) -> None:
    target = tmp_path / "cases"
    write_case(target, case("local"))
    api = FakeAPI(
        {"local": b"in", "remote": b"ri"},
        {"local": b"out", "remote": b"ro"},
    )
    result = push_testcases(api, 42, target)
    assert result.pruned == 0
    assert not (target / "remote.in").exists()


def test_replace_unlinks_case_symlink_without_touching_its_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_bytes(b"keep")
    target = tmp_path / "cases"
    target.mkdir()
    (target / "old.in").symlink_to(secret)

    replace_local_snapshot(target, {"new": case("new")})

    assert secret.read_bytes() == b"keep"
    assert not (target / "old.in").exists()


def test_fetch_remote_sides_represents_incomplete_pairs() -> None:
    api = FakeAPI(
        {"paired": b"pi", "input_only": b"i"},
        {"paired": b"po", "output_only": b"o"},
    )

    remote = fetch_remote_sides(api, 42)

    assert remote.inputs == {"input_only": b"i", "paired": b"pi"}
    assert remote.outputs == {"output_only": b"o", "paired": b"po"}
    assert remote.names == frozenset({"input_only", "output_only", "paired"})
    assert remote.complete_snapshot() == {"paired": case("paired", b"pi", b"po")}
    with pytest.raises(
        ValidationError,
        match=r"missing remote outputs for input_only; missing remote inputs for output_only",
    ):
        remote.require_complete()


def test_push_repairs_missing_side_and_ignores_unrelated_incomplete_case(
    tmp_path: Path,
) -> None:
    target = tmp_path / "cases"
    write_case(target, case("sample", b"input", b"output"))
    api = FakeAPI(
        {"sample": b"input", "unrelated": b"orphan"},
        {},
    )

    result = push_testcases(api, 42, target)

    assert api.uploads == [("out", ("sample",))]
    assert result.uploaded_inputs == 0
    assert result.uploaded_outputs == 1
    assert result.remote_snapshot == {"sample": case("sample", b"input", b"output")}
    assert api.data["in"]["unrelated"] == b"orphan"


def test_push_prunes_only_remote_sides_that_exist(tmp_path: Path) -> None:
    target = tmp_path / "cases"
    write_case(target, case("keep"))
    api = FakeAPI(
        {"keep": b"in", "input_stale": b"stale"},
        {"keep": b"out", "output_stale": b"stale"},
    )

    result = push_testcases(api, 42, target, prune=True)

    assert api.deletes == [("in", "input_stale"), ("out", "output_stale")]
    assert result.pruned == 2
    assert result.remote_snapshot == {"keep": case("keep")}


def test_push_retries_only_side_left_by_partial_upload_failure(tmp_path: Path) -> None:
    target = tmp_path / "cases"
    write_case(target, case("sample"))
    api = FakeAPI()
    upload = api.upload_testcases
    fail_output = True

    def flaky_upload(
        problem_id: int,
        which: Which | str,
        files: Mapping[str, bytes],
    ) -> object:
        nonlocal fail_output
        if FakeAPI.side(which) == "out" and fail_output:
            fail_output = False
            raise RuntimeError("output upload failed")
        return upload(problem_id, which, files)

    api.upload_testcases = flaky_upload  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="output upload failed"):
        push_testcases(api, 42, target)
    assert api.data == {"in": {"sample": b"in"}, "out": {}}

    before = len(api.uploads)
    result = push_testcases(api, 42, target)

    assert api.uploads[before:] == [("out", ("sample",))]
    assert result.uploaded_inputs == 0
    assert result.uploaded_outputs == 1


def test_push_requires_uploaded_case_to_be_complete_after_refresh(tmp_path: Path) -> None:
    target = tmp_path / "cases"
    write_case(target, case("sample"))
    api = FakeAPI({"sample": b"in"}, {})

    def discard_upload(
        _problem_id: int,
        _which: Which | str,
        _files: Mapping[str, bytes],
    ) -> object:
        return object()

    api.upload_testcases = discard_upload  # type: ignore[method-assign]
    with pytest.raises(ValidationError, match="missing remote outputs for sample"):
        push_testcases(api, 42, target)


def _swap_artifacts(parent: Path, target_name: str) -> tuple[Path, ...]:
    return tuple(
        path
        for path in parent.iterdir()
        if path.name.startswith(f".{target_name}.stage-")
        or path.name.startswith(f".{target_name}.backup-")
    )


def test_replace_rolls_back_when_final_swap_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cases"
    write_case(target, case("old"))
    replace = testcase_sync_module.os.replace

    def fail_final(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == target and source_path.name.startswith(".cases.stage-"):
            raise PermissionError("final swap denied")
        replace(source, destination)

    monkeypatch.setattr(testcase_sync_module.os, "replace", fail_final)
    with pytest.raises(FileOperationError, match="final swap denied"):
        replace_local_snapshot(target, {"new": case("new")})

    assert (target / "old.in").read_bytes() == b"in"
    assert not (target / "new.in").exists()
    assert _swap_artifacts(tmp_path, target.name) == ()


def test_replace_reports_rollback_failure_and_keeps_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cases"
    write_case(target, case("old"))
    replace = testcase_sync_module.os.replace

    def fail_final_and_rollback(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == target and (
            source_path.name.startswith(".cases.stage-")
            or source_path.name.startswith(".cases.backup-")
        ):
            reason = "final swap denied" if ".stage-" in source_path.name else "restore denied"
            raise PermissionError(reason)
        replace(source, destination)

    monkeypatch.setattr(testcase_sync_module.os, "replace", fail_final_and_rollback)
    with pytest.raises(FileOperationError, match=r"rollback also failed: restore denied"):
        replace_local_snapshot(target, {"new": case("new")})

    assert not target.exists()
    artifacts = _swap_artifacts(tmp_path, target.name)
    assert len(artifacts) == 1
    assert ".backup-" in artifacts[0].name
    assert (artifacts[0] / "old.in").read_bytes() == b"in"


@pytest.mark.parametrize("failure", ["backup", "final"])
def test_replace_keyboard_interrupt_restores_original_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    target = tmp_path / "cases"
    write_case(target, case("old"))
    replace = testcase_sync_module.os.replace

    def interrupt(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if failure == "backup" and source_path == target:
            raise KeyboardInterrupt
        if (
            failure == "final"
            and destination_path == target
            and source_path.name.startswith(".cases.stage-")
        ):
            raise KeyboardInterrupt
        replace(source, destination)

    monkeypatch.setattr(testcase_sync_module.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        replace_local_snapshot(target, {"new": case("new")})

    assert (target / "old.in").read_bytes() == b"in"
    assert not (target / "new.in").exists()
    assert _swap_artifacts(tmp_path, target.name) == ()


def test_replace_recognizes_commit_when_final_swap_is_interrupted_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cases"
    write_case(target, case("old"))
    replace = testcase_sync_module.os.replace

    def interrupt_after_replace(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        replace(source, destination)
        if destination_path == target and source_path.name.startswith(".cases.stage-"):
            raise KeyboardInterrupt

    monkeypatch.setattr(testcase_sync_module.os, "replace", interrupt_after_replace)
    replace_local_snapshot(target, {"new": case("new")})

    assert (target / "new.in").read_bytes() == b"in"
    assert not (target / "old.in").exists()
    assert _swap_artifacts(tmp_path, target.name) == ()


@pytest.mark.parametrize("failure", ["stage", "write"])
def test_replace_failure_removes_new_empty_parent_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    created_root = tmp_path / "new" / "deep"
    target = created_root / "cases"

    if failure == "stage":

        def fail_mkdtemp(*_args: object, **_kwargs: object) -> str:
            raise PermissionError("stage denied")

        monkeypatch.setattr(files_module.tempfile, "mkdtemp", fail_mkdtemp)
        expected: type[BaseException] = FileOperationError
    else:

        def interrupt_write(
            _path: Path,
            _data: bytes,
            *,
            create_parents: bool = True,
        ) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(testcase_sync_module, "atomic_write_bytes", interrupt_write)
        expected = KeyboardInterrupt

    with pytest.raises(expected):
        replace_local_snapshot(target, {"new": case("new")})

    assert not (tmp_path / "new").exists()


def test_replace_keeps_original_when_backup_swap_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cases"
    write_case(target, case("old"))
    replace = testcase_sync_module.os.replace

    def fail_backup(source: str | Path, destination: str | Path) -> None:
        if Path(source) == target:
            raise PermissionError("backup swap denied")
        replace(source, destination)

    monkeypatch.setattr(testcase_sync_module.os, "replace", fail_backup)
    with pytest.raises(FileOperationError, match="backup swap denied"):
        replace_local_snapshot(target, {"new": case("new")})

    assert (target / "old.in").read_bytes() == b"in"
    assert not (target / "new.in").exists()
    assert _swap_artifacts(tmp_path, target.name) == ()


def test_failed_final_swap_removes_new_parent_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "new" / "deep" / "cases"
    replace = testcase_sync_module.os.replace

    def fail_final(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == target and source_path.name.startswith(".cases.stage-"):
            raise PermissionError("final swap denied")
        replace(source, destination)

    monkeypatch.setattr(testcase_sync_module.os, "replace", fail_final)
    with pytest.raises(FileOperationError, match="final swap denied"):
        replace_local_snapshot(target, {"new": case("new")})

    assert not (tmp_path / "new").exists()


def test_staged_snapshot_mapping_protocol_and_missing_key(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    snapshot = fetch_remote_snapshot_to(
        FakeAPI({"sample": b"in"}, {"sample": b"out"}),
        42,
        stage,
    )

    assert len(snapshot) == 1
    assert list(snapshot) == ["sample"]
    with pytest.raises(KeyError):
        snapshot["absent"]


def test_remote_fetches_reject_non_byte_bodies() -> None:
    strict = FakeAPI({"sample": b"in"}, {"sample": b"out"})
    strict.get_testcase = lambda _pid, _side, _name: "text"  # type: ignore[method-assign,return-value]
    with pytest.raises(ValidationError, match="raw bytes"):
        fetch_remote_snapshot(strict, 42)

    sides = FakeAPI({"sample": b"in"}, {"sample": b"out"})
    sides.get_testcase = lambda _pid, _side, _name: object()  # type: ignore[method-assign,return-value]
    with pytest.raises(ValidationError, match="raw bytes"):
        fetch_remote_sides(sides, 42)


@pytest.mark.parametrize("kind", ["symlink", "file", "nonempty"])
def test_fetch_to_rejects_invalid_staging_directory(
    tmp_path: Path,
    kind: str,
) -> None:
    stage = tmp_path / "stage"
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        stage.symlink_to(outside, target_is_directory=True)
    elif kind == "file":
        stage.write_bytes(b"not a directory")
    else:
        stage.mkdir()
        (stage / "artifact").write_bytes(b"keep")

    with pytest.raises(FileOperationError, match="staging directory"):
        fetch_remote_snapshot_to(FakeAPI(), 42, stage)


def test_fetch_to_wraps_directory_creation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    mkdir = Path.mkdir

    def fail_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == stage:
            raise PermissionError("mkdir denied")
        mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    with pytest.raises(FileOperationError, match="mkdir denied"):
        fetch_remote_snapshot_to(FakeAPI(), 42, stage)


def test_fetch_to_removes_partial_files_after_body_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    write = testcase_sync_module.atomic_write_bytes
    writes = 0

    def fail_second_write(
        path: Path,
        data: bytes,
        *,
        create_parents: bool = True,
    ) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise KeyboardInterrupt
        write(path, data, create_parents=create_parents)

    monkeypatch.setattr(testcase_sync_module, "atomic_write_bytes", fail_second_write)
    with pytest.raises(KeyboardInterrupt):
        fetch_remote_snapshot_to(
            FakeAPI({"sample": b"in"}, {"sample": b"out"}),
            42,
            stage,
        )
    assert tuple(stage.iterdir()) == ()


def test_snapshot_validation_rejects_collisions_and_invalid_entries(tmp_path: Path) -> None:
    target = tmp_path / "cases"
    with pytest.raises(ValidationError, match="collision"):
        replace_local_snapshot(target, {"Case": case("Case"), "case": case("case")})
    with pytest.raises(ValidationError, match="not TestCaseData"):
        replace_local_snapshot(target, {"sample": object()})  # type: ignore[dict-item]
    with pytest.raises(ValidationError, match="does not match"):
        replace_local_snapshot(target, {"sample": case("different")})
    assert not target.exists()


@pytest.mark.parametrize("kind", ["parent_symlink", "parent_file", "target_file"])
def test_replace_rejects_non_directory_layout(
    tmp_path: Path,
    kind: str,
) -> None:
    if kind == "parent_symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        parent = tmp_path / "parent"
        parent.symlink_to(outside, target_is_directory=True)
        target = parent / "cases"
    elif kind == "parent_file":
        parent = tmp_path / "parent"
        parent.write_bytes(b"file")
        target = parent / "cases"
    else:
        target = tmp_path / "cases"
        target.write_bytes(b"file")

    with pytest.raises(FileOperationError, match=r"invalid|could not prepare"):
        replace_local_snapshot(target, {"sample": case("sample")})


def test_estimate_and_batch_reject_invalid_values() -> None:
    with pytest.raises(ValidationError, match="content must be bytes"):
        testcase_sync_module.estimate_upload_size("sample", "text")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limits must be positive"):
        batch_upload_files({}, max_files=0)
    with pytest.raises(ValueError, match="limits must be positive"):
        batch_upload_files({}, max_bytes=0)
