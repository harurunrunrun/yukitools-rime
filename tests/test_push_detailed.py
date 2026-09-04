from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from yukitools_rime.api.types import (
    EditorialContent,
    EditorialRequest,
    GeneratorContent,
    JudgeCodeContent,
    JudgeCodeRequest,
    JudgeCodeSaveResponse,
    ProblemEditContent,
)
from yukitools_rime.commands import push as push_module
from yukitools_rime.commands.push import PushInterrupted
from yukitools_rime.errors import LayoutError, UsageError, ValidationError
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
    Statement,
    Which,
)
from yukitools_rime.rime_config import render_problem_block, render_project_block
from yukitools_rime.testcase_sync import RemoteTestcaseSides


def settings() -> ProblemSettings:
    return ProblemSettings(
        title="title",
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
    )


def make_project(root: Path) -> ProjectLayout:
    root.mkdir()
    (root / "PROJECT").write_text(
        render_project_block(ProjectConfig(rime_out_dir="generated")),
        encoding="utf-8",
    )
    problem = root / "problem"
    problem.mkdir()
    (problem / "PROBLEM").write_text(
        render_problem_block(ProblemConfig(1, settings(), "A")),
        encoding="utf-8",
    )
    (problem / "statement.md").write_text("body\n", encoding="utf-8")
    return load_project(root)


def local_problem(
    problem: ProblemLayout,
    *,
    generator: push_module._LocalProgram | None = None,
    judge: push_module._LocalProgram | None = None,
    editorial: Statement | None = None,
) -> push_module._LocalProblem:
    return push_module._LocalProblem(
        problem=problem,
        config=problem.config,
        statement=Statement.markdown("body\n"),
        generator=generator,
        judge=judge,
        editorial=editorial,
        testcase_dir=None,
        testcases=None,
    )


class PlanClient:
    def __init__(self) -> None:
        self.edit: object = ProblemEditContent(1, "body\n", True, True, settings())
        self.generator: object = GeneratorContent("cpp17", "generator\n", True, 2)
        self.judge: object = JudgeCodeContent("cpp17", "judge\n", "AC")
        self.editorial: object = EditorialContent("editorial\n", True)

    def get_problem_edit(self, _problem_id: int) -> object:
        return self.edit

    def get_generator(self, _problem_id: int) -> object:
        return self.generator

    def get_judge_code(self, _problem_id: int) -> object:
        return self.judge

    def get_editorial(self, _problem_id: int) -> object:
        return self.editorial


def test_target_selection_and_project_config_variants(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    problem = project.problems[0]
    selection = TargetSelection(project, (problem,), problem.path)

    assert push_module._selected_problems(problem) == (problem,)
    assert push_module._project_config(selection) is project.config
    assert push_module._project_config(problem).rime_out_dir == "generated"

    empty = ProjectLayout(tmp_path / "empty", ProjectConfig(), ())
    with pytest.raises(LayoutError, match="no managed problems"):
        push_module._selected_problems(empty)
    with pytest.raises(TypeError, match="target must be"):
        push_module._selected_problems(object())  # type: ignore[arg-type]


def test_document_rejects_ambiguous_and_missing_required_files(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    problem = project.problems[0]
    (problem.path / "statement.html").write_text("<p>body</p>", encoding="utf-8")
    with pytest.raises(LayoutError, match="both statement"):
        push_module._document(problem, "statement", required=True)

    (problem.path / "statement.md").unlink()
    (problem.path / "statement.html").unlink()
    with pytest.raises(LayoutError, match=r"statement.md or statement.html is required"):
        push_module._document(problem, "statement", required=True)


def test_testcase_preflight_requires_direct_testset(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    with pytest.raises(LayoutError, match="direct-child TESTSET"):
        push_module._preflight_local(
            project.problems[0],
            project.config,
            include_testcases=True,
            generate=False,
        )


@pytest.mark.parametrize(
    ("resource", "message"),
    [
        ("problem-type", "problem API returned"),
        ("problem-id", "requested problem 1"),
        ("generator", "generator API returned"),
        ("judge", "judge API returned"),
        ("editorial", "editorial API returned"),
    ],
)
def test_remote_plan_rejects_unexpected_response_types_and_identity(
    tmp_path: Path,
    resource: str,
    message: str,
) -> None:
    project = make_project(tmp_path / "project")
    problem = project.problems[0]
    client = PlanClient()
    generator = None
    judge = None
    editorial = None
    if resource == "problem-type":
        client.edit = object()
    elif resource == "problem-id":
        client.edit = ProblemEditContent(2, "body\n", True, True, settings())
    elif resource == "generator":
        generator = push_module._LocalProgram(
            GeneratorConfig("cpp17", "generator.cpp", 2, rime_kind="cxx"),
            "generator\n",
        )
        client.generator = object()
    elif resource == "judge":
        judge = push_module._LocalProgram(
            JudgeConfig("cpp17", "judge.cpp", "cxx"),
            "judge\n",
        )
        client.judge = object()
    else:
        editorial = Statement.markdown("editorial\n")
        client.editorial = object()

    with pytest.raises(ValidationError, match=message):
        push_module._build_remote_plan(
            local_problem(
                problem,
                generator=generator,
                judge=judge,
                editorial=editorial,
            ),
            client,  # type: ignore[arg-type]
            include_testcases=False,
            prune=False,
            generate=False,
        )


def valid_options() -> dict[str, object]:
    return {
        "include_testcases": False,
        "prune": False,
        "dry_run": False,
        "generate": False,
        "no_wait_compile": False,
        "judge_timeout": 180.0,
        "judge_poll_interval": 3.0,
        "testcase_refresh_delay": 2.0,
    }


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("include_testcases", 1, "include_testcases must be true or false"),
        ("judge_timeout", "soon", "judge_timeout must be a number"),
        ("judge_timeout", -1, "finite non-negative"),
        ("judge_poll_interval", 0, "finite positive"),
        ("testcase_refresh_delay", float("inf"), "finite non-negative"),
    ],
)
def test_option_validation_rejects_invalid_types_and_ranges(
    name: str,
    value: object,
    message: str,
) -> None:
    options = valid_options()
    options[name] = value
    with pytest.raises((UsageError, ValidationError), match=message):
        push_module._validate_options(**options)  # type: ignore[arg-type]


class ExecutionClient:
    def __init__(self, judge_status: str = "AC") -> None:
        self.judge_status = judge_status
        self.editorials: list[EditorialRequest] = []
        self.uploads: list[tuple[Which, tuple[str, ...]]] = []
        self.deletes: list[tuple[Which, str]] = []

    def save_judge_code(
        self,
        _problem_id: int,
        _request: JudgeCodeRequest,
    ) -> JudgeCodeSaveResponse:
        return JudgeCodeSaveResponse("saved", self.judge_status)

    def get_judge_code(self, _problem_id: int) -> JudgeCodeContent:
        return JudgeCodeContent("cpp17", "judge\n", self.judge_status)

    def save_editorial(
        self,
        _problem_id: int,
        request: EditorialRequest,
    ) -> object:
        self.editorials.append(request)
        return object()

    def upload_testcases(
        self,
        _problem_id: int,
        which: Which,
        files: Mapping[str, bytes],
    ) -> object:
        self.uploads.append((which, tuple(files)))
        return object()

    def delete_testcase(self, _problem_id: int, which: Which, name: str) -> None:
        self.deletes.append((which, name))


def execute(
    plan: push_module._ProblemPlan,
    *,
    sleep: push_module.Sleep = lambda _seconds: None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return push_module._execute_plan(
        plan,
        [],
        no_wait_compile=False,
        judge_timeout=5,
        judge_poll_interval=1,
        testcase_refresh_delay=0,
        sleep=sleep,
        monotonic=lambda: 0,
    )


def test_execute_immediate_ac_judge_and_editorial(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path / "project")
    local = local_problem(project.problems[0])
    client = ExecutionClient("AC")
    plan = push_module._ProblemPlan(
        local,
        client,  # type: ignore[arg-type]
        None,
        None,
        JudgeCodeRequest("cpp17", "judge\n"),
        EditorialRequest(Statement.markdown("editorial\n")),
        None,
    )

    completed, warnings = execute(plan)

    assert completed == ("problem 1 judge", "problem 1 editorial")
    assert warnings == ()
    assert len(client.editorials) == 1


def test_execute_judge_wait_keyboard_interrupt_reports_saved_judge(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path / "project")
    local = local_problem(project.problems[0])
    client = ExecutionClient("WJ")
    plan = push_module._ProblemPlan(
        local,
        client,  # type: ignore[arg-type]
        None,
        None,
        JudgeCodeRequest("cpp17", "judge\n"),
        None,
        None,
    )

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(PushInterrupted) as caught:
        execute(plan, sleep=interrupt)
    assert caught.value.failed_item == "problem 1 judge compilation"
    assert caught.value.completed_items == ("problem 1 judge",)


def test_execute_nonstandard_upload_responses_and_side_specific_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path / "project")
    local = local_problem(project.problems[0])
    client = ExecutionClient()
    case = CaseData("sample", b"input", b"output")
    testcase_plan = push_module._TestcasePlan(
        {"sample": case},
        ({"sample": b"input"},),
        ({"sample": b"output"},),
        ("input-only",),
        ("output-only",),
        tmp_path / "unused",
    )
    plan = push_module._ProblemPlan(
        local,
        client,  # type: ignore[arg-type]
        None,
        None,
        None,
        None,
        testcase_plan,
    )
    replaced: list[Mapping[str, CaseData]] = []
    monkeypatch.setattr(
        push_module,
        "fetch_remote_sides",
        lambda _client, _problem_id: RemoteTestcaseSides(
            {"sample": b"input"},
            {"sample": b"output"},
        ),
    )
    monkeypatch.setattr(
        push_module,
        "replace_local_snapshot",
        lambda _directory, snapshot: replaced.append(snapshot),
    )

    completed, warnings = execute(plan)

    assert warnings == ()
    assert len(completed) == 5
    assert client.uploads == [
        (Which.IN, ("sample",)),
        (Which.OUT, ("sample",)),
    ]
    assert client.deletes == [
        (Which.IN, "input-only"),
        (Which.OUT, "output-only"),
    ]
    assert replaced == [{"sample": case}]


def test_testcase_plan_without_writes_has_no_refresh_item(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    local = local_problem(project.problems[0])
    testcase_plan = push_module._TestcasePlan(
        {},
        (),
        (),
        (),
        (),
        tmp_path / "unused",
    )
    plan = push_module._ProblemPlan(
        local,
        ExecutionClient(),  # type: ignore[arg-type]
        None,
        None,
        None,
        None,
        testcase_plan,
    )

    assert not testcase_plan.has_writes
    assert plan.planned_items == ()
    assert execute(plan) == ((), ())
