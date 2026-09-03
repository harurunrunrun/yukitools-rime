from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from yukitools_rime import files as files_module
from yukitools_rime.errors import ConflictError, FileOperationError, ValidationError
from yukitools_rime.layout import TestCaseData as CaseData
from yukitools_rime.models import Which
from yukitools_rime.testcase_sync import (
    MULTIPART_FILE_OVERHEAD,
    SnapshotChanges,
    batch_upload_files,
    compare_snapshots,
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
