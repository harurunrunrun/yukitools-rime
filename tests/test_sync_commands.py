from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from yukitools_rime import rime_config
from yukitools_rime.api.types import (
    EditorialContent,
    GeneratorContent,
    JudgeCodeContent,
    ProblemEditContent,
)
from yukitools_rime.commands import sync
from yukitools_rime.errors import ValidationError
from yukitools_rime.layout import ProjectLayout, load_project, read_testcases
from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    Which,
)
from yukitools_rime.rime_config import (
    parse_problem_config,
    parse_testset_config,
    render_problem_block,
    render_project_block,
    render_testset_block,
)


def settings(title: str = "local", *, limit: int = 1000) -> ProblemSettings:
    return ProblemSettings(
        title,
        "tag",
        1,
        limit,
        256,
        "-",
        "0",
        False,
        False,
        0,
        0,
    )


def add_problem(root: Path, name: str, problem_id: int, *, programs: bool = True) -> Path:
    problem = root / name
    problem.mkdir()
    config = ProblemConfig(
        problem_id,
        settings(),
        name.upper(),
        "answer",
        {"local_option": True},
    )
    (problem / "PROBLEM").write_text(render_problem_block(config), encoding="utf-8")
    (problem / "statement.md").write_text("old statement\n", encoding="utf-8")
    tests = problem / "tests"
    tests.mkdir()
    if programs:
        testset = rime_config.TestsetConfig(
            GeneratorConfig(
                "cpp17",
                "custom-generator.cpp",
                2,
                "case",
                "cxx",
                {"flags": ["-O2"]},
            ),
            JudgeConfig("cpp17", "custom-judge.cpp", "cxx", {"flags": ["-O2"]}),
        )
        (tests / "custom-generator.cpp").write_text("old generator\n", encoding="utf-8")
        (tests / "custom-judge.cpp").write_text("old judge\n", encoding="utf-8")
    else:
        testset = rime_config.TestsetConfig()
    (tests / "TESTSET").write_text(render_testset_block(testset), encoding="utf-8")
    return problem


def make_project(tmp_path: Path, *, programs: bool = True) -> ProjectLayout:
    config = ProjectConfig(rime_out_dir="generated")
    (tmp_path / "PROJECT").write_text(render_project_block(config), encoding="utf-8")
    add_problem(tmp_path, "a", 1, programs=programs)
    return load_project(tmp_path)


def side(which: Which | str) -> str:
    return which.value if isinstance(which, Which) else which


@dataclass
class FakeClient:
    edit: ProblemEditContent
    generator: GeneratorContent | None = None
    judge: JudgeCodeContent | None = None
    editorial: EditorialContent = field(default_factory=EditorialContent)
    cases: dict[tuple[str, str], bytes] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    on_call: Any = None

    def record(self, value: str) -> None:
        self.calls.append(value)
        if self.on_call is not None:
            self.on_call(value)

    def get_problem_edit(self, problem_id: int) -> ProblemEditContent:
        self.record("edit")
        return self.edit

    def get_generator(self, problem_id: int) -> GeneratorContent | None:
        self.record("generator")
        return self.generator

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent | None:
        self.record("judge")
        return self.judge

    def get_editorial(self, problem_id: int) -> EditorialContent:
        self.record("editorial")
        return self.editorial

    def list_testcases(self, problem_id: int, which: Which | str) -> list[str]:
        value = side(which)
        self.record(f"list-{value}")
        return sorted(name for case_side, name in self.cases if case_side == value)

    def get_testcase(self, problem_id: int, which: Which | str, name: str) -> bytes:
        value = side(which)
        self.record(f"get-{value}-{name}")
        return self.cases[(value, name)]

    def upload_testcases(
        self,
        problem_id: int,
        which: Which | str,
        files: Any,
    ) -> object:
        raise AssertionError("pull/diff never uploads")

    def delete_testcase(self, problem_id: int, which: Which | str, name: str) -> None:
        raise AssertionError("pull/diff never deletes")


def client(
    *,
    problem_id: int = 1,
    statement: str = "remote statement\n",
    markdown: bool = True,
    generator: GeneratorContent | None = None,
    judge: JudgeCodeContent | None = None,
    editorial: EditorialContent | None = None,
    cases: dict[tuple[str, str], bytes] | None = None,
) -> FakeClient:
    return FakeClient(
        ProblemEditContent(
            problem_id,
            statement,
            markdown,
            True,
            settings("remote", limit=2500),
        ),
        generator,
        judge,
        editorial or EditorialContent(),
        cases or {},
    )


def factory(value: FakeClient) -> sync.ClientFactory:
    return lambda problem: value


def file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_pull_updates_resources_and_preserves_local_only_fields(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    problem_path = project.problems[0].path
    (problem_path / "editorial.md").write_text("old editorial\n", encoding="utf-8")
    remote = client(
        statement="remote html\r\n",
        markdown=False,
        generator=GeneratorContent("cpp20", "new generator\r\n", True, 10),
        judge=JudgeCodeContent("cpp20", "", ""),
        editorial=EditorialContent("new editorial\r\n", False),
    )

    result = sync.pull(project, factory(remote))

    parsed = parse_problem_config((problem_path / "PROBLEM").read_text())
    assert parsed.settings.title == "remote"
    assert parsed.rime_id == "A"
    assert parsed.reference_solution == "answer"
    assert parsed.rime_options == {"local_option": True}
    assert not (problem_path / "statement.md").exists()
    assert (problem_path / "statement.html").read_bytes() == b"remote html\n"
    assert not (problem_path / "editorial.md").exists()
    assert (problem_path / "editorial.html").read_bytes() == b"new editorial\n"
    testset = parse_testset_config((problem_path / "tests" / "TESTSET").read_text())
    assert testset.generator == GeneratorConfig(
        "cpp20",
        "custom-generator.cpp",
        10,
        "case",
        "cxx",
        {"flags": ["-O2"]},
    )
    assert testset.judge == JudgeConfig("cpp17", "custom-judge.cpp", "cxx", {"flags": ["-O2"]})
    assert (problem_path / "tests" / "custom-generator.cpp").read_text() == "new generator\n"
    assert (problem_path / "tests" / "custom-judge.cpp").read_text() == "old judge\n"
    assert result.applied
    assert any("remote judge" in warning for warning in result.warnings)
    assert remote.calls == ["edit", "generator", "judge", "editorial"]


def test_pull_preserves_bom_crlf_and_unmanaged_problem_and_testset_text(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    problem_path = project.problems[0].path
    problem_config = ProblemConfig(
        1,
        settings(),
        "A",
        "answer",
        {"local_option": True},
    )
    testset_config = rime_config.TestsetConfig(
        GeneratorConfig("cpp17", "custom-generator.cpp", 2, "case", "cxx"),
        JudgeConfig("cpp17", "custom-judge.cpp", "cxx"),
    )
    problem_source = (
        "\ufeff# problem header\r\n"
        + render_problem_block(problem_config).replace("\n", "\r\n")
        + "# problem footer\r\n"
    )
    testset_source = (
        "\ufeff# testset header\r\n"
        + render_testset_block(testset_config).replace("\n", "\r\n")
        + "# testset footer\r\n"
    )
    (problem_path / "PROBLEM").write_bytes(problem_source.encode("utf-8"))
    (problem_path / "tests" / "TESTSET").write_bytes(testset_source.encode("utf-8"))
    project = load_project(tmp_path)
    remote = client(generator=GeneratorContent("cpp20", "new generator\n", True, 8))

    sync.pull(project, factory(remote))

    for path, header, footer in (
        (problem_path / "PROBLEM", b"# problem header", b"# problem footer\r\n"),
        (problem_path / "tests" / "TESTSET", b"# testset header", b"# testset footer\r\n"),
    ):
        contents = path.read_bytes()
        assert contents.startswith(b"\xef\xbb\xbf" + header + b"\r\n")
        assert contents.endswith(footer)
        assert b"\n" not in contents[3:].replace(b"\r\n", b"")


def test_pull_does_not_create_missing_editorial_and_infers_new_program(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path, programs=False)
    problem_path = project.problems[0].path
    remote = client(generator=GeneratorContent("rust1", "fn main() {}\n", True, 4))

    sync.pull(project, factory(remote))

    assert not (problem_path / "editorial.md").exists()
    assert not (problem_path / "editorial.html").exists()
    testset = parse_testset_config((problem_path / "tests" / "TESTSET").read_text())
    assert testset.generator is not None
    assert testset.generator.src == "generator.rs"
    assert testset.generator.rime_kind == "rust"
    assert (problem_path / "tests" / "generator.rs").read_text() == "fn main() {}\n"


def test_pull_fetches_whole_project_before_writing(tmp_path: Path) -> None:
    config = ProjectConfig()
    (tmp_path / "PROJECT").write_text(render_project_block(config), encoding="utf-8")
    first = add_problem(tmp_path, "a", 1)
    add_problem(tmp_path, "b", 2)
    project = load_project(tmp_path)
    clients = {
        1: client(problem_id=1, statement="changed\n"),
        2: client(problem_id=2),
    }

    def fail(value: str) -> None:
        if value == "editorial":
            raise RuntimeError("remote failed")

    clients[2].on_call = fail
    with pytest.raises(RuntimeError, match="remote failed"):
        sync.pull(project, lambda problem: clients[problem.problem_id])
    assert (first / "statement.md").read_text() == "old statement\n"


def test_wrong_remote_problem_id_is_rejected_before_write(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    before = file_snapshot(tmp_path)
    with pytest.raises(ValidationError, match="remote returned problem"):
        sync.pull(project, factory(client(problem_id=99)))
    assert file_snapshot(tmp_path) == before


def write_local_case(problem: Path, name: str, input_data: bytes, output_data: bytes) -> Path:
    directory = problem / "generated" / "tests"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.in").write_bytes(input_data)
    (directory / f"{name}.diff").write_bytes(output_data)
    return directory


def test_testcase_conflict_callback_can_decline_while_other_pull_continues(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    problem = project.problems[0].path
    directory = write_local_case(problem, "old", b"old-in", b"old-out")
    remote = client(
        cases={
            ("in", "new"): b"new-in",
            ("out", "new"): b"new-out",
        }
    )
    observed: list[tuple[int, int, int]] = []

    def decline(layout: Any, changes: Any) -> bool:
        assert (problem / "statement.md").read_text() == "old statement\n"
        observed.append(changes.counts)
        return False

    result = sync.pull(
        project,
        factory(remote),
        include_testcases=True,
        confirm=decline,
    )

    assert observed == [(1, 0, 1)]
    cases = read_testcases(directory)
    assert set(cases) == {"old"}
    assert cases["old"].input == b"old-in"
    assert cases["old"].output == b"old-out"
    assert (problem / "statement.md").read_text() == "remote statement\n"
    assert result.problems[0].testcases_applied is False


def test_testcase_accept_replaces_exact_snapshot(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    problem = project.problems[0].path
    directory = write_local_case(problem, "old", b"old-in", b"old-out")
    remote = client(
        cases={
            ("in", "new.txt"): b"new-in\x00",
            ("out", "new.txt"): b"new-out\xff",
        }
    )
    result = sync.pull(
        project,
        factory(remote),
        include_testcases=True,
        confirm=lambda problem, changes: True,
    )
    cases = read_testcases(directory)
    assert set(cases) == {"new.txt"}
    assert cases["new.txt"].input == b"new-in\x00"
    assert cases["new.txt"].output == b"new-out\xff"
    assert result.problems[0].testcases_applied


def test_project_pull_with_testcases_keeps_all_staged_snapshots_available(
    tmp_path: Path,
) -> None:
    (tmp_path / "PROJECT").write_text(
        render_project_block(ProjectConfig(rime_out_dir="generated")),
        encoding="utf-8",
    )
    first = add_problem(tmp_path, "a", 1)
    second = add_problem(tmp_path, "b", 2)
    project = load_project(tmp_path)
    clients = {
        1: client(
            problem_id=1,
            cases={
                ("in", "first.bin"): b"first-in\x00",
                ("out", "first.bin"): b"first-out\xff",
            },
        ),
        2: client(
            problem_id=2,
            cases={
                ("in", "second.bin"): b"second-in\xfe",
                ("out", "second.bin"): b"second-out\x00",
            },
        ),
    }

    def before_last_fetch(value: str) -> None:
        if value == "get-out-second.bin":
            assert not (first / "generated" / "tests").exists()

    clients[2].on_call = before_last_fetch
    result = sync.pull(
        project,
        lambda problem: clients[problem.problem_id],
        include_testcases=True,
    )

    assert result.applied
    assert (first / "generated" / "tests" / "first.bin.in").read_bytes() == b"first-in\x00"
    assert (first / "generated" / "tests" / "first.bin.diff").read_bytes() == b"first-out\xff"
    assert (second / "generated" / "tests" / "second.bin.in").read_bytes() == b"second-in\xfe"
    assert (second / "generated" / "tests" / "second.bin.diff").read_bytes() == b"second-out\x00"
    assert clients[1].calls[-4:] == [
        "list-in",
        "list-out",
        "get-in-first.bin",
        "get-out-first.bin",
    ]
    assert clients[2].calls[-4:] == [
        "list-in",
        "list-out",
        "get-in-second.bin",
        "get-out-second.bin",
    ]


def test_diff_is_structured_and_strictly_read_only(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    problem = project.problems[0].path
    write_local_case(problem, "sample", b"input", b"local-output")
    remote = client(
        statement="different\n",
        generator=GeneratorContent("cpp20", "remote generator\n", True, 9),
        judge=JudgeCodeContent("cpp20", "remote judge\n", "AC"),
        cases={
            ("in", "sample"): b"input",
            ("out", "sample"): b"remote-output",
        },
    )
    before = file_snapshot(tmp_path)

    result = sync.diff_remote(project, factory(remote), include_testcases=True)

    assert result.has_changes
    assert any(entry.resource == "settings.title" for entry in result.problems[0].entries)
    assert any(
        entry.resource == "statement" and entry.unified for entry in result.problems[0].entries
    )
    assert any(
        entry.resource == "testcase sample" and entry.detail == "raw bytes differ"
        for entry in result.problems[0].entries
    )
    assert remote.calls[-4:] == ["list-in", "list-out", "get-in-sample", "get-out-sample"]
    assert file_snapshot(tmp_path) == before


def test_pull_rolls_back_regular_files_when_testcase_swap_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    problem = project.problems[0].path
    write_local_case(problem, "old", b"old-in", b"old-out")
    before = file_snapshot(tmp_path)
    remote = client(
        statement="changed\n",
        cases={("in", "new"): b"in", ("out", "new"): b"out"},
    )

    def fail(directory: Path, snapshot: Any) -> None:
        raise OSError("swap failed")

    monkeypatch.setattr(sync, "replace_local_snapshot", fail)
    with pytest.raises(OSError, match="swap failed"):
        sync.pull(
            project,
            factory(remote),
            include_testcases=True,
            confirm=lambda problem, changes: True,
        )
    assert file_snapshot(tmp_path) == before


def test_pull_rollback_removes_directories_created_before_testcase_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "PROJECT").write_text(
        render_project_block(ProjectConfig(rime_out_dir="generated")),
        encoding="utf-8",
    )
    problem_path = tmp_path / "a"
    problem_path.mkdir()
    (problem_path / "PROBLEM").write_text(
        render_problem_block(ProblemConfig(1, settings(), "A")),
        encoding="utf-8",
    )
    (problem_path / "statement.md").write_text("old statement\n", encoding="utf-8")
    project = load_project(tmp_path)
    before = file_snapshot(tmp_path)
    remote = client(
        generator=GeneratorContent("cpp20", "new generator\n", True, 2),
        cases={
            ("in", "new"): b"input",
            ("out", "new"): b"output",
        },
    )

    def fail(directory: Path, snapshot: Any) -> None:
        raise OSError("swap failed")

    monkeypatch.setattr(sync, "replace_local_snapshot", fail)
    with pytest.raises(OSError, match="swap failed"):
        sync.pull(
            project,
            factory(remote),
            include_testcases=True,
        )

    assert file_snapshot(tmp_path) == before
    assert not (problem_path / "tests").exists()
