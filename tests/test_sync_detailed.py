from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yukitools_rime.api import (
    EditorialContent,
    GeneratorContent,
    JudgeCodeContent,
    ProblemEditContent,
)
from yukitools_rime.commands import sync
from yukitools_rime.errors import ConflictError, FileOperationError, LayoutError
from yukitools_rime.layout import (
    ProblemLayout,
    ProjectLayout,
    TargetSelection,
    load_project,
)
from yukitools_rime.layout import (
    TestCaseData as CaseData,
)
from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    Which,
)
from yukitools_rime.rime_config import (
    TestsetConfig as RimeTestsetConfig,
)
from yukitools_rime.rime_config import (
    render_problem_block,
    render_project_block,
    render_testset_block,
)
from yukitools_rime.testcase_sync import SnapshotChanges


def _settings(title: str = "local") -> ProblemSettings:
    return ProblemSettings(
        title=title,
        tags="tag",
        level=1.0,
        time_limit_ms=1000,
        memory_limit=256,
        eps_mode="-",
        eps="0",
        wip=False,
        recruiting_tester=False,
        problem_type=0,
        judge_type=0,
        show_ans=False,
        enable_pure_judge=False,
        force_single_server_judge=False,
        allowed_langs=[],
    )


def _make_project(
    tmp_path: Path,
    *,
    programs: bool = True,
    with_testset: bool = True,
) -> ProjectLayout:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = ProjectConfig(rime_out_dir="generated")
    (tmp_path / "PROJECT").write_text(render_project_block(config), encoding="utf-8")
    problem = tmp_path / "a"
    problem.mkdir()
    (problem / "PROBLEM").write_text(
        render_problem_block(ProblemConfig(1, _settings(), "A")),
        encoding="utf-8",
    )
    (problem / "statement.md").write_text("local statement\n", encoding="utf-8")
    if with_testset:
        tests = problem / "tests"
        tests.mkdir()
        if programs:
            testset = RimeTestsetConfig(
                GeneratorConfig("cpp17", "generator.cpp", 2, None, "cxx"),
                JudgeConfig("cpp17", "judge.cpp", "cxx"),
            )
            (tests / "generator.cpp").write_text("local generator\n", encoding="utf-8")
            (tests / "judge.cpp").write_text("local judge\n", encoding="utf-8")
        else:
            testset = RimeTestsetConfig()
        (tests / "TESTSET").write_text(render_testset_block(testset), encoding="utf-8")
    return load_project(tmp_path)


class _Client:
    def __init__(
        self,
        edit: ProblemEditContent,
        *,
        generator: GeneratorContent | None = None,
        judge: JudgeCodeContent | None = None,
        editorial: EditorialContent | None = None,
        cases: dict[tuple[str, str], bytes] | None = None,
    ) -> None:
        self.edit = edit
        self.generator = generator
        self.judge = judge
        self.editorial = editorial or EditorialContent()
        self.cases = cases or {}

    def get_problem_edit(self, problem_id: int) -> ProblemEditContent:
        return self.edit

    def get_generator(self, problem_id: int) -> GeneratorContent | None:
        return self.generator

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent | None:
        return self.judge

    def get_editorial(self, problem_id: int) -> EditorialContent:
        return self.editorial

    def list_testcases(self, problem_id: int, which: Which | str) -> list[str]:
        side = which.value if isinstance(which, Which) else which
        return sorted(name for (case_side, name) in self.cases if case_side == side)

    def get_testcase(self, problem_id: int, which: Which | str, name: str) -> bytes:
        side = which.value if isinstance(which, Which) else which
        return self.cases[(side, name)]

    def upload_testcases(
        self,
        problem_id: int,
        which: Which | str,
        files: Any,
    ) -> object:
        raise AssertionError("not used")

    def delete_testcase(self, problem_id: int, which: Which | str, name: str) -> None:
        raise AssertionError("not used")


def _edit(
    *,
    statement: str = "local statement\n",
    markdown: bool = True,
    showable: bool = True,
    settings: ProblemSettings | None = None,
) -> ProblemEditContent:
    return ProblemEditContent(
        problem_id=1,
        content=statement,
        is_markdown=markdown,
        showable=showable,
        settings=settings or _settings(),
    )


def _remote(
    problem: ProblemLayout,
    *,
    edit: ProblemEditContent | None = None,
    generator: GeneratorContent | None = None,
    judge: JudgeCodeContent | None = None,
    editorial: EditorialContent | None = None,
    testcases: dict[str, CaseData] | None = None,
) -> sync._RemoteProblem:
    return sync._RemoteProblem(
        problem,
        edit or _edit(),
        generator,
        judge,
        editorial or EditorialContent(),
        testcases,
    )


def _write_case(directory: Path, name: str, input_data: bytes, output_data: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.in").write_bytes(input_data)
    (directory / f"{name}.diff").write_bytes(output_data)


def _metadata(root: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_result_views_cover_empty_and_populated_aggregates(tmp_path: Path) -> None:
    changes = SnapshotChanges(added=("a",))
    empty_pull = sync.PullProblemResult(1, tmp_path)
    changed_pull = sync.PullProblemResult(
        2,
        tmp_path,
        warnings=("warning",),
        testcase_changes=changes,
        testcases_applied=True,
    )
    pull = sync.PullResult((empty_pull, changed_pull))
    assert not empty_pull.applied
    assert changed_pull.applied
    assert pull.applied
    assert pull.warnings == ("warning",)

    entry = sync.DiffEntry("statement", "content differs", ("--- remote", "+++ local"))
    empty_diff = sync.ProblemDiffResult(1, tmp_path)
    changed_diff = sync.ProblemDiffResult(2, tmp_path, (entry,), ("warning",))
    result = sync.DiffResult((empty_diff, changed_diff))
    assert entry.lines == ("statement: content differs", "--- remote", "+++ local")
    assert not empty_diff.has_changes
    assert changed_diff.has_changes
    assert changed_diff.lines == entry.lines
    assert result.has_changes
    assert result.warnings == ("warning",)
    assert result.lines == (
        "--- problem 1 ---",
        "--- problem 2 ---",
        *entry.lines,
    )
    assert not sync.DiffResult(()).has_changes
    assert not sync.PullResult(()).applied


def test_target_helpers_accept_project_problem_and_selection(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    problem = project.problems[0]
    selection = TargetSelection(project, (problem,), problem.path)

    assert sync._selected_problems(problem) == (problem,)
    assert sync._selected_problems(selection) == (problem,)
    assert sync._project_config(project) == project.config
    assert sync._project_config(selection) == project.config
    assert sync._project_config(problem) == project.config


def test_fetch_remote_requires_staging_for_testcases(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    remote_client = _Client(_edit())

    with pytest.raises(ValueError, match="staging"):
        sync._fetch_remote(
            project,
            lambda problem: remote_client,
            include_testcases=True,
            testcase_staging=None,
        )


def test_document_and_mutation_helpers_cover_all_states(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    problem = project.problems[0]
    statement = problem.path / "statement.md"

    assert sync._document(problem, "editorial") is None
    assert sync._document(problem, "statement") == (statement, "local statement\n", True)
    html = problem.path / "statement.html"
    html.write_text("html", encoding="utf-8")
    with pytest.raises(LayoutError, match="both statement"):
        sync._document(problem, "statement")
    html.unlink()

    missing = problem.path / "missing"
    assert sync._mutation_if_changed(missing, None) is None
    removal = sync._mutation_if_changed(statement, None)
    assert removal == sync._Mutation(statement, None)
    assert sync._mutation_if_changed(statement, b"local statement\n") is None
    replacement = sync._mutation_if_changed(statement, b"new")
    assert replacement == sync._Mutation(statement, b"new")

    mutations: list[sync._Mutation] = []
    sync._append_mutation(mutations, statement, b"local statement\n")
    assert mutations == []
    sync._append_mutation(mutations, missing, b"new")
    assert mutations == [sync._Mutation(missing, b"new")]

    mutations.clear()
    sync._update_document(
        mutations,
        problem,
        "editorial",
        "remote",
        True,
        only_if_present=True,
    )
    assert mutations == []


@pytest.mark.parametrize("kind", ["file", "symlink", "nonempty"])
def test_missing_testset_rejects_unsafe_destination(
    tmp_path: Path,
    kind: str,
) -> None:
    project = _make_project(tmp_path, with_testset=False)
    problem = project.problems[0]
    tests = problem.path / "tests"
    if kind == "file":
        tests.write_text("file", encoding="utf-8")
    elif kind == "symlink":
        actual = tmp_path / "actual-tests"
        actual.mkdir()
        tests.symlink_to(actual, target_is_directory=True)
    else:
        tests.mkdir()
        (tests / "user-file").write_text("keep", encoding="utf-8")

    with pytest.raises(ConflictError, match=r"unsafe|non-empty"):
        sync._testset_state(problem)


def test_missing_testset_wraps_iteration_and_accepts_empty_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path, with_testset=False)
    problem = project.problems[0]
    tests = problem.path / "tests"
    tests.mkdir()
    real_iterdir = Path.iterdir

    def denied(path: Path) -> Any:
        if path == tests:
            raise PermissionError("inspect denied")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(FileOperationError, match="inspect denied"):
        sync._testset_state(problem)

    monkeypatch.undo()
    path, source, config = sync._testset_state(problem)
    assert path == tests
    assert source == ""
    assert config == RimeTestsetConfig()


def test_testcase_planning_unchanged_and_confirmation_required(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    problem = project.problems[0]
    directory = problem.path / "generated" / "tests"
    _write_case(directory, "same", b"in", b"out")
    snapshot = {"same": CaseData("same", b"in", b"out")}

    changes, apply, target = sync._plan_testcases(
        _remote(problem, testcases=snapshot),
        project.config,
        None,
    )
    assert changes is not None
    assert not changes.has_changes
    assert not apply
    assert target == directory

    changed = {"same": CaseData("same", b"changed", b"out")}
    with pytest.raises(ConflictError, match="confirmation"):
        sync._plan_testcases(
            _remote(problem, testcases=changed),
            project.config,
            None,
        )


def test_build_plan_creates_testset_for_remote_cases(tmp_path: Path) -> None:
    project = _make_project(tmp_path, with_testset=False)
    problem = project.problems[0]
    plan = sync._build_pull_plan(
        _remote(problem, testcases={}),
        project.config,
        None,
    )

    assert any(mutation.path.name == "TESTSET" for mutation in plan.mutations)


@pytest.mark.parametrize("resource", ["generator", "judge"])
@pytest.mark.parametrize("symlink", [False, True])
def test_build_plan_refuses_new_program_source_collision(
    tmp_path: Path,
    resource: str,
    symlink: bool,
) -> None:
    project = _make_project(tmp_path, programs=False)
    problem = project.problems[0]
    source = problem.path / "tests" / f"{resource}.cpp"
    if symlink:
        target = tmp_path / f"{resource}-target"
        target.write_text("user", encoding="utf-8")
        source.symlink_to(target)
    else:
        source.write_text("user", encoding="utf-8")

    generator = GeneratorContent("cpp17", "remote\n", True, 2) if resource == "generator" else None
    judge = JudgeCodeContent("cpp17", "remote\n", "AC") if resource == "judge" else None
    with pytest.raises(ConflictError, match=resource):
        sync._build_pull_plan(
            _remote(problem, generator=generator, judge=judge),
            project.config,
            None,
        )


def test_restore_mutations_records_first_error_and_directory_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_file = tmp_path / "new"
    old_file = tmp_path / "old"

    def fail_remove(path: Path, *, missing_ok: bool) -> None:
        raise FileOperationError("first restore failure")

    def fail_write(path: Path, data: bytes) -> None:
        raise FileOperationError("second restore failure")

    monkeypatch.setattr(sync, "remove_file", fail_remove)
    monkeypatch.setattr(sync, "atomic_write_bytes", fail_write)
    error = sync._restore_mutations([(old_file, b"old"), (new_file, None)])
    assert error is not None
    assert "first restore failure" in str(error)

    monkeypatch.undo()
    missing = tmp_path / "missing-directory"
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "child").write_text("x", encoding="utf-8")
    error = sync._restore_mutations([], (missing, nonempty))
    assert isinstance(error, OSError)


def test_missing_parent_dirs_skips_removals_and_collects_nested(tmp_path: Path) -> None:
    removal = sync._Mutation(tmp_path / "absent", None)
    addition = sync._Mutation(tmp_path / "one" / "two" / "file", b"data")
    missing = set(sync._missing_parent_dirs((removal, addition)))

    assert missing == {tmp_path / "one", tmp_path / "one" / "two"}


def test_apply_mutation_reports_rollback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation = sync._Mutation(tmp_path / "new", b"value")

    def fail_write(path: Path, data: bytes) -> None:
        raise FileOperationError("write failed")

    monkeypatch.setattr(sync, "atomic_write_bytes", fail_write)
    monkeypatch.setattr(
        sync,
        "_restore_mutations",
        lambda backups, created_dirs=(): FileOperationError("rollback failed"),
    )

    with pytest.raises(FileOperationError, match="rollback also failed"):
        sync._apply_mutations((mutation,))


def test_pull_testcase_swap_and_regular_rollback_double_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    remote_client = _Client(
        _edit(statement="remote statement\n"),
        cases={("in", "new"): b"in", ("out", "new"): b"out"},
    )

    def fail_swap(directory: Path, snapshot: Any) -> None:
        raise OSError("swap failed")

    monkeypatch.setattr(sync, "replace_local_snapshot", fail_swap)
    monkeypatch.setattr(
        sync,
        "_restore_mutations",
        lambda backups, created_dirs=(): FileOperationError("rollback failed"),
    )

    with pytest.raises(FileOperationError, match="regular-file rollback also failed"):
        sync.pull(
            project,
            lambda problem: remote_client,
            include_testcases=True,
        )


def test_text_and_source_helpers_cover_missing_format_equal_and_invalid(
    tmp_path: Path,
) -> None:
    assert sync._unified("value", "same\r\n", "same\n") == ()
    missing = sync._text_entries("statement", "remote", True, None)
    assert missing == [sync.DiffEntry("statement", "missing locally")]

    path = tmp_path / "statement.html"
    path.write_text("remote", encoding="utf-8")
    entries = sync._text_entries("statement", "remote", True, (path, "remote", False))
    assert len(entries) == 1
    assert entries[0].detail.startswith("format differs")

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(FileOperationError, match="regular file"):
        sync._source_text(directory)
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(FileOperationError, match="regular file"):
        sync._source_text(link)


def test_diff_matrix_is_byte_and_mtime_read_only(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    problem = project.problems[0]
    (problem.path / "editorial.html").write_text("local editorial\n", encoding="utf-8")
    case_dir = problem.path / "generated" / "tests"
    _write_case(case_dir, "local_only", b"local", b"local")
    _write_case(case_dir, "changed", b"old", b"same")
    before = _metadata(tmp_path)

    remote = _remote(
        problem,
        edit=_edit(
            statement="<p>remote statement</p>\n",
            markdown=False,
            showable=False,
            settings=_settings("remote"),
        ),
        generator=None,
        judge=JudgeCodeContent("cpp17", "", "AC"),
        editorial=EditorialContent("remote editorial\n", True),
        testcases={
            "remote_only": CaseData("remote_only", b"remote", b"remote"),
            "changed": CaseData("changed", b"new", b"same"),
        },
    )
    result = sync._diff_problem(remote, project.config)
    resources = {entry.resource: entry.detail for entry in result.entries}

    assert "settings.title" in resources
    assert resources["statement"] in {
        "format differs (remote=HTML, local=Markdown)",
        "content differs",
    }
    assert any(entry.resource == "editorial" for entry in result.entries)
    assert resources["generator"] == "registered locally but empty remotely"
    assert resources["judge"] == "registered locally but empty remotely"
    assert resources["testcase remote_only"] == "exists only remotely"
    assert resources["testcase local_only"] == "exists only locally"
    assert resources["testcase changed"] == "raw bytes differ"
    assert len(result.warnings) == 2
    assert _metadata(tmp_path) == before


def test_diff_reports_remote_programs_missing_locally_and_missing_statement(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path, programs=False)
    problem = project.problems[0]
    (problem.path / "statement.md").unlink()
    result = sync._diff_problem(
        _remote(
            problem,
            generator=GeneratorContent("cpp17", "remote generator\n", True, 4),
            judge=JudgeCodeContent("cpp17", "remote judge\n", "AC"),
        ),
        project.config,
    )
    details = {(entry.resource, entry.detail) for entry in result.entries}

    assert ("statement", "missing locally") in details
    assert ("generator", "present remotely but missing locally") in details
    assert ("judge", "present remotely but missing locally") in details


def test_diff_equal_programs_and_unavailable_judge_have_no_program_entries(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    problem = project.problems[0]
    result = sync._diff_problem(
        _remote(
            problem,
            generator=GeneratorContent(
                "cpp17",
                "local generator\n",
                True,
                2,
            ),
            judge=None,
        ),
        project.config,
    )

    assert not any(entry.resource.startswith("generator") for entry in result.entries)
    assert any("judge API" in warning for warning in result.warnings)


def test_diff_absent_local_programs_cover_empty_remote_paths(tmp_path: Path) -> None:
    project = _make_project(tmp_path, programs=False)
    problem = project.problems[0]
    result = sync._diff_problem(
        _remote(
            problem,
            generator=None,
            judge=JudgeCodeContent("cpp17", "", "AC"),
        ),
        project.config,
    )

    assert not any(entry.resource in {"generator", "judge"} for entry in result.entries)
    assert len(result.warnings) == 2


def test_diff_equal_judge_has_no_judge_entries(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    problem = project.problems[0]
    result = sync._diff_problem(
        _remote(
            problem,
            generator=GeneratorContent("cpp17", "local generator\n", True, 2),
            judge=JudgeCodeContent("cpp17", "local judge\n", "AC"),
        ),
        project.config,
    )

    assert not any(entry.resource.startswith("judge") for entry in result.entries)


def test_build_plan_warns_for_nonpublic_problem(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    problem = project.problems[0]
    plan = sync._build_pull_plan(
        _remote(
            problem,
            edit=_edit(showable=False),
        ),
        project.config,
        None,
    )

    assert any("not public" in warning for warning in plan.warnings)
