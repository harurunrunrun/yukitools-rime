from __future__ import annotations

from pathlib import Path

import pytest

from yukitools_rime.errors import ConfigError, LayoutError, ValidationError
from yukitools_rime.layout import (
    TestCaseData as CaseData,
)
from yukitools_rime.layout import (
    discover_project,
    load_project,
    local_testcase_paths,
    read_testcases,
    resolve_target,
)
from yukitools_rime.layout import (
    testcase_change_counts as change_counts,
)
from yukitools_rime.models import ProblemConfig, ProblemSettings, ProjectConfig
from yukitools_rime.rime_config import render_problem_block, render_project_block


def make_problem(root: Path, name: str, problem_id: int) -> Path:
    path = root / name
    path.mkdir()
    config = ProblemConfig(
        problem_id,
        ProblemSettings(name, "", 1, 1000, 256, "-", "0", False, False, 0, 0),
        name,
    )
    (path / "PROBLEM").write_text(render_problem_block(config), encoding="utf-8")
    return path


def test_shallow_discovery_and_target_resolution(tmp_path: Path) -> None:
    (tmp_path / "PROJECT").write_text(render_project_block(ProjectConfig()), encoding="utf-8")
    problem = make_problem(tmp_path, "a", 1)
    tests = problem / "tests"
    tests.mkdir()
    (tests / "TESTSET").write_text("", encoding="utf-8")
    nested = problem / "nested"
    nested.mkdir()
    (nested / "PROBLEM").write_text(render_problem_block(
        ProblemConfig(
            99,
            ProblemSettings("nested", "", 1, 1000, 256, "-", "0", False, False, 0, 0),
            "N",
        )
    ), encoding="utf-8")
    project = discover_project(tests)
    assert [item.problem_id for item in project.problems] == [1]
    selected = resolve_target(tests)
    assert selected.problem is not None
    assert selected.problem.path == problem
    assert resolve_target(tmp_path).problems == project.problems


def test_duplicate_problem_ids_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "PROJECT").write_text(render_project_block(ProjectConfig()), encoding="utf-8")
    make_problem(tmp_path, "a", 1)
    make_problem(tmp_path, "b", 1)
    with pytest.raises(LayoutError, match="duplicate problem id"):
        load_project(tmp_path)


def test_testcase_mapping_reads_raw_bytes_and_dots(tmp_path: Path) -> None:
    infile, outfile = local_testcase_paths(tmp_path, "sample.01")
    infile.write_bytes(b"1\r\n")
    outfile.write_bytes(b"2\n")
    cases = read_testcases(tmp_path)
    assert cases == {"sample.01": CaseData("sample.01", b"1\r\n", b"2\n")}


def test_testcase_pairs_and_names_are_validated(tmp_path: Path) -> None:
    (tmp_path / "lonely.in").write_bytes(b"x")
    with pytest.raises(LayoutError, match=r"missing \.diff"):
        read_testcases(tmp_path)
    with pytest.raises(ValidationError):
        local_testcase_paths(tmp_path, "../escape")


def test_change_counts() -> None:
    local = {
        "old": CaseData("old", b"i", b"o"),
        "same": CaseData("same", b"i", b"o"),
        "changed": CaseData("changed", b"i", b"old"),
    }
    remote = {
        "new": CaseData("new", b"i", b"o"),
        "same": CaseData("same", b"i", b"o"),
        "changed": CaseData("changed", b"i", b"new"),
    }
    assert change_counts(local, remote) == (1, 1, 1)


def test_missing_and_multiple_direct_testsets_are_distinguished(
    tmp_path: Path,
) -> None:
    (tmp_path / "PROJECT").write_text(
        render_project_block(ProjectConfig()), encoding="utf-8"
    )
    problem = make_problem(tmp_path, "a", 1)
    assert load_project(tmp_path).problems[0].testset is None

    for name in ("tests-one", "tests-two"):
        directory = problem / name
        directory.mkdir()
        (directory / "TESTSET").write_text("", encoding="utf-8")
    with pytest.raises(LayoutError, match="multiple TESTSET"):
        load_project(tmp_path)


def test_target_symlink_cannot_escape_an_explicit_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "PROJECT").write_text(
        render_project_block(ProjectConfig()), encoding="utf-8"
    )
    problem = make_problem(root, "a", 1)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = problem / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(LayoutError, match="escapes project root"):
        resolve_target(link, project=load_project(root))


def test_testcase_output_symlink_cannot_escape_problem(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "PROJECT").write_text(
        render_project_block(ProjectConfig()), encoding="utf-8"
    )
    problem_path = make_problem(root, "a", 1)
    tests = problem_path / "tests"
    tests.mkdir()
    (tests / "TESTSET").write_text("", encoding="utf-8")
    outside = tmp_path / "outside-cases"
    (outside / "tests").mkdir(parents=True)
    try:
        (problem_path / "rime-out").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    problem = load_project(root).problems[0]
    with pytest.raises(LayoutError, match="escapes problem root"):
        problem.testcase_dir(ProjectConfig())


def test_case_looking_symlink_is_rejected_without_reading_target(
    tmp_path: Path,
) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    outside = tmp_path / "secret"
    outside.write_bytes(b"do-not-upload")
    try:
        (cases / "sample.in").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    (cases / "sample.diff").write_bytes(b"answer")

    with pytest.raises(LayoutError, match="regular file"):
        read_testcases(cases)


def test_invalid_utf8_project_is_a_configuration_error(tmp_path: Path) -> None:
    (tmp_path / "PROJECT").write_bytes(b"\xff")
    with pytest.raises(ConfigError, match="UTF-8"):
        load_project(tmp_path)
