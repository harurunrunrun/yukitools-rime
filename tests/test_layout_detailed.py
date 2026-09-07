from __future__ import annotations

from pathlib import Path

import pytest

from yukitools_rime.errors import LayoutError, ValidationError
from yukitools_rime.layout import (
    ProblemLayout,
    ProjectLayout,
    SolutionLayout,
    TargetSelection,
    find_project_root,
    inspect_testcases,
    load_problem,
    load_project,
    read_testcases,
    resolve_target,
)
from yukitools_rime.layout import (
    TestCaseData as CaseData,
)
from yukitools_rime.models import (
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    SolutionConfig,
)
from yukitools_rime.rime_config import (
    render_problem_block,
    render_project_block,
    render_solution_block,
)


def problem_config(problem_id: int = 1, rime_id: str = "A") -> ProblemConfig:
    return ProblemConfig(
        problem_id,
        ProblemSettings(
            "Title",
            "",
            1,
            1000,
            256,
            "-",
            "0",
            False,
            False,
            0,
            0,
        ),
        rime_id,
    )


def create_project(tmp_path: Path, *, problems: int = 1) -> tuple[Path, list[Path]]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "PROJECT").write_text(render_project_block(ProjectConfig()), encoding="utf-8")
    paths: list[Path] = []
    for index in range(problems):
        problem = root / chr(ord("a") + index)
        problem.mkdir()
        (problem / "PROBLEM").write_text(
            render_problem_block(problem_config(index + 1, chr(ord("A") + index))),
            encoding="utf-8",
        )
        paths.append(problem)
    return root, paths


@pytest.mark.parametrize(
    ("name", "input_data", "output_data"),
    [
        ("bad name", b"in", b"out"),
        ("sample", "in", b"out"),
        ("sample", b"in", "out"),
    ],
)
def test_case_data_validates_name_and_raw_byte_sides(
    name: str, input_data: object, output_data: object
) -> None:
    with pytest.raises(ValidationError):
        CaseData(name, input_data, output_data)  # type: ignore[arg-type]


def test_layout_properties_and_problem_lookup(tmp_path: Path) -> None:
    root, (problem_path,) = create_project(tmp_path)
    solution_path = problem_path / "solution"
    solution_path.mkdir()
    solution_config = SolutionConfig("cpp", "main.cpp", "cxx")
    (solution_path / "SOLUTION").write_text(
        render_solution_block(solution_config), encoding="utf-8"
    )

    project = load_project(root)
    problem = project.problems[0]
    solution = problem.solutions[0]

    assert project.config_path == root / "PROJECT"
    assert problem.config_path == problem_path / "PROBLEM"
    assert problem.problem_id == 1
    assert solution.config_path == solution_path / "SOLUTION"
    assert project.problem_by_id(1) is problem
    with pytest.raises(LayoutError, match="problem id 99"):
        project.problem_by_id(99)


def test_target_selection_problem_is_none_for_project_scope(tmp_path: Path) -> None:
    root, _ = create_project(tmp_path, problems=2)
    selection = resolve_target(root)

    assert selection.problem is None
    assert len(selection.problems) == 2


def test_output_directory_rejects_file_and_rime_configuration_collision(
    tmp_path: Path,
) -> None:
    root, (problem_path,) = create_project(tmp_path)
    problem = load_project(root).problems[0]
    output = problem_path / "rime-out"
    output.write_bytes(b"file")
    with pytest.raises(LayoutError, match="must be a directory"):
        problem.output_dir(ProjectConfig())

    output.unlink()
    output.mkdir()
    (output / "PROBLEM").write_text("collision", encoding="utf-8")
    with pytest.raises(LayoutError, match="collides"):
        problem.output_dir(ProjectConfig())


def test_testcase_directory_requires_testset_or_explicit_default(tmp_path: Path) -> None:
    root, _ = create_project(tmp_path)
    problem = load_project(root).problems[0]

    with pytest.raises(LayoutError, match="no direct-child TESTSET"):
        problem.testcase_dir(ProjectConfig())
    assert (
        problem.testcase_dir(ProjectConfig(), default_testset_name="generated")
        == problem.path / "rime-out" / "generated"
    )


def test_find_project_root_from_file_and_failure_paths(tmp_path: Path) -> None:
    root, (problem_path,) = create_project(tmp_path)
    source = problem_path / "source.cpp"
    source.write_text("int main() {}", encoding="utf-8")

    assert find_project_root(source) == root.resolve()
    with pytest.raises(LayoutError, match="target does not exist"):
        find_project_root(tmp_path / "missing")
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(LayoutError, match="PROJECT not found"):
        find_project_root(outside)


def test_project_marker_must_be_regular_not_symlink(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    # Keep the target name distinct on case-insensitive filesystems. Using
    # "PROJECT" here aliases the "project" directory on Windows.
    outside = tmp_path / "outside-project-marker"
    outside.write_text(render_project_block(ProjectConfig()), encoding="utf-8")
    try:
        (root / "PROJECT").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(LayoutError, match="PROJECT not found"):
        load_project(root)


def test_direct_problem_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "PROJECT").write_text(render_project_block(ProjectConfig()), encoding="utf-8")
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "PROBLEM").write_text(render_problem_block(problem_config()), encoding="utf-8")
    try:
        (root / "linked").symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(LayoutError, match="symlink target"):
        load_project(root)


def test_load_problem_without_testset_or_solution(tmp_path: Path) -> None:
    root, (problem_path,) = create_project(tmp_path)

    loaded = load_problem(problem_path)

    assert loaded.path == problem_path.resolve()
    assert loaded.testset is None
    assert loaded.solutions == ()
    assert load_project(root).problems == (loaded,)


def test_resolve_target_supports_config_file_explicit_root_and_solution(
    tmp_path: Path,
) -> None:
    root, (problem_path,) = create_project(tmp_path)
    solution_path = problem_path / "solution"
    solution_path.mkdir()
    (solution_path / "SOLUTION").write_text(
        render_solution_block(SolutionConfig("cpp", "main.cpp", "cxx")),
        encoding="utf-8",
    )
    loaded = load_project(root)

    from_file = resolve_target(problem_path / "PROBLEM", project=loaded)
    assert from_file.problem is not None
    assert from_file.problem.path == problem_path.resolve()

    explicit_path = resolve_target(solution_path, project=root)
    assert explicit_path.solution is not None
    assert explicit_path.solution.path == solution_path.resolve()


def test_resolve_target_rejects_missing_outside_and_nonproblem_paths(tmp_path: Path) -> None:
    root, _ = create_project(tmp_path)
    loaded = load_project(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    unrelated = root / "notes"
    unrelated.mkdir()

    with pytest.raises(LayoutError, match="target does not exist"):
        resolve_target(root / "missing", project=loaded)
    with pytest.raises(LayoutError, match="escapes project root"):
        resolve_target(outside, project=loaded)
    with pytest.raises(LayoutError, match="not inside"):
        resolve_target(unrelated, project=loaded)


def test_inspect_missing_empty_and_irrelevant_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert inspect_testcases(missing) == ({}, (), ())
    assert read_testcases(missing, require_nonempty=False) == {}

    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "README").write_text("ignored", encoding="utf-8")
    assert inspect_testcases(cases) == ({}, (), ())
    with pytest.raises(LayoutError, match="no testcase pairs"):
        read_testcases(cases)


def test_inspect_rejects_file_and_case_suffix_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "cases"
    file_path.write_bytes(b"x")
    with pytest.raises(LayoutError, match="must be a directory"):
        inspect_testcases(file_path)

    file_path.unlink()
    file_path.mkdir()
    (file_path / "sample.in").mkdir()
    with pytest.raises(LayoutError, match="regular file"):
        inspect_testcases(file_path)


def test_inspect_reports_both_incomplete_sides(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "only_input.in").write_bytes(b"in")
    (cases / "only_output.diff").write_bytes(b"out")

    complete, missing_outputs, missing_inputs = inspect_testcases(cases)

    assert complete == {}
    assert missing_outputs == ("only_input",)
    assert missing_inputs == ("only_output",)
    with pytest.raises(LayoutError) as caught:
        read_testcases(cases)
    assert "missing .diff" in str(caught.value)
    assert "missing .in" in str(caught.value)


def test_inspect_rejects_case_insensitive_name_collision(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "Name.in").write_bytes(b"in")
    (cases / "name.diff").write_bytes(b"out")

    with pytest.raises(LayoutError, match="case-insensitive"):
        inspect_testcases(cases)


@pytest.mark.parametrize(
    ("input_data", "output_data", "message"),
    [
        (b"", b"out", "empty input"),
        (b"in", b"", "empty output"),
    ],
)
def test_read_testcases_rejects_empty_sides(
    tmp_path: Path, input_data: bytes, output_data: bytes, message: str
) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "sample.in").write_bytes(input_data)
    (cases / "sample.diff").write_bytes(output_data)

    with pytest.raises(LayoutError, match=message):
        read_testcases(cases)


def test_inspect_wraps_directory_iteration_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    original = Path.iterdir

    def fail_iterdir(self: Path):
        if self == cases:
            raise PermissionError("denied")
        return original(self)

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)
    with pytest.raises(LayoutError, match="cannot inspect testcase directory"):
        inspect_testcases(cases)


def test_inspect_wraps_case_read_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    input_path = cases / "sample.in"
    input_path.write_bytes(b"in")
    (cases / "sample.diff").write_bytes(b"out")
    original = Path.read_bytes

    def fail_read(self: Path) -> bytes:
        if self == input_path:
            raise PermissionError("denied")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(LayoutError, match="cannot read testcase"):
        inspect_testcases(cases)


def test_manual_layout_value_objects_keep_expected_fields(tmp_path: Path) -> None:
    config = problem_config()
    problem = ProblemLayout(tmp_path / "p", config, None, ())
    project = ProjectLayout(tmp_path, ProjectConfig(), (problem,))
    selection = TargetSelection(project, (problem,), problem.path)
    solution = SolutionLayout(tmp_path / "s", SolutionConfig("cpp", "main.cpp"))

    assert selection.problem is problem
    assert solution.path == tmp_path / "s"
