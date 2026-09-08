from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from yukitools_rime.api.types import (
    EditorialContent,
    EditorialRequest,
    GeneratorContent,
    GeneratorRequest,
    JudgeCodeContent,
    JudgeCodeRequest,
    JudgeCodeSaveResponse,
    ProblemEditContent,
    ProblemEditRequest,
    SaveResponse,
    StatusInfo,
    UploadResponse,
    ValidatorCase,
    ValidatorContent,
    ValidatorRequest,
)
from yukitools_rime.commands import push as push_module
from yukitools_rime.commands.push import (
    JudgeCompileError,
    PushExecutionError,
    PushInterrupted,
    ValidatorValidationError,
    push,
)
from yukitools_rime.errors import LayoutError, UsageError, ValidationError
from yukitools_rime.layout import ProblemLayout, ProjectLayout, load_project, read_testcases
from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    ValidatorConfig,
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


def settings(title: str = "local") -> ProblemSettings:
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
        judge_type=1,
    )


def make_project(
    root: Path,
    *,
    count: int = 1,
    programs: bool = True,
    editorial: bool = False,
) -> ProjectLayout:
    (root / "PROJECT").write_text(
        render_project_block(ProjectConfig(rime_out_dir="generated")),
        encoding="utf-8",
    )
    for problem_id in range(1, count + 1):
        problem = root / chr(ord("a") + problem_id - 1)
        problem.mkdir()
        (problem / "PROBLEM").write_text(
            render_problem_block(ProblemConfig(problem_id, settings(), problem.name.upper())),
            encoding="utf-8",
        )
        (problem / "statement.md").write_text("local statement\n", encoding="utf-8")
        tests = problem / "tests"
        tests.mkdir()
        if programs:
            config = RimeTestsetConfig(
                GeneratorConfig("cpp17", "generator.cpp", 2, "case", "cxx"),
                JudgeConfig("cpp17", "judge.cpp", "cxx"),
            )
            (tests / "generator.cpp").write_text("local generator\n", encoding="utf-8")
            (tests / "judge.cpp").write_text("local judge\n", encoding="utf-8")
        else:
            config = RimeTestsetConfig()
        (tests / "TESTSET").write_text(render_testset_block(config), encoding="utf-8")
        if editorial:
            (problem / "editorial.md").write_text("local editorial\n", encoding="utf-8")
    return load_project(root)


def side(which: Which | str) -> str:
    return which.value if isinstance(which, Which) else which


@dataclass
class FakeClient:
    problem_id: int = 1
    edit: ProblemEditContent | None = None
    generator: GeneratorContent | None = None
    judge: JudgeCodeContent | None = None
    editorial: EditorialContent = field(default_factory=EditorialContent)
    cases: dict[tuple[str, str], bytes] = field(default_factory=dict)
    fail_on: str | None = None
    save_judge_status: str = "AC"
    poll_statuses: list[str] = field(default_factory=list)
    normalize_uploads: bool = False
    upload_warning: str = ""
    reported_file_names: tuple[str, ...] | None = None
    calls: list[str] = field(default_factory=list)
    problem_requests: list[ProblemEditRequest] = field(default_factory=list)
    generator_requests: list[GeneratorRequest] = field(default_factory=list)
    judge_requests: list[JudgeCodeRequest] = field(default_factory=list)
    editorial_requests: list[EditorialRequest] = field(default_factory=list)
    judge_saved: bool = False

    def __post_init__(self) -> None:
        if self.edit is None:
            self.edit = ProblemEditContent(
                self.problem_id,
                "local statement\n",
                True,
                True,
                settings(),
            )
        if self.generator is None:
            self.generator = GeneratorContent("cpp17", "local generator\n", True, 2)
        if self.judge is None:
            self.judge = JudgeCodeContent("cpp17", "local judge\n", "AC")

    def record(self, call: str) -> None:
        self.calls.append(call)
        if self.fail_on == call:
            raise RuntimeError(f"{call} failed")

    def get_problem_edit(self, problem_id: int) -> ProblemEditContent:
        self.record("get-problem")
        assert self.edit is not None
        return self.edit

    def save_problem_edit(self, problem_id: int, request: ProblemEditRequest) -> SaveResponse:
        self.record("save-problem")
        self.problem_requests.append(request)
        self.edit = ProblemEditContent(
            problem_id,
            request.statement.text,
            request.statement.is_markdown,
            True,
            request.settings,
        )
        return SaveResponse("saved")

    def get_generator(self, problem_id: int) -> GeneratorContent | None:
        self.record("get-generator")
        return self.generator

    def save_generator(self, problem_id: int, request: GeneratorRequest) -> SaveResponse:
        self.record("save-generator")
        self.generator_requests.append(request)
        self.generator = GeneratorContent(
            request.lang_id,
            request.source,
            False,
            request.test_case_num,
        )
        return SaveResponse("saved")

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent | None:
        self.record("get-judge")
        if self.judge_saved:
            status = self.poll_statuses.pop(0) if self.poll_statuses else self.save_judge_status
            assert self.judge_requests
            request = self.judge_requests[-1]
            self.judge = JudgeCodeContent(request.lang_id, request.source, status)
        return self.judge

    def save_judge_code(self, problem_id: int, request: JudgeCodeRequest) -> JudgeCodeSaveResponse:
        self.record("save-judge")
        self.judge_requests.append(request)
        self.judge_saved = True
        self.judge = JudgeCodeContent(
            request.lang_id,
            request.source,
            self.save_judge_status,
        )
        return JudgeCodeSaveResponse("saved", self.save_judge_status)

    def statuses(self) -> list[StatusInfo]:
        self.record("statuses")
        return [
            StatusInfo("WJ", "judging"),
            StatusInfo("Judge", "judging"),
        ]

    def get_editorial(self, problem_id: int) -> EditorialContent:
        self.record("get-editorial")
        return self.editorial

    def save_editorial(self, problem_id: int, request: EditorialRequest) -> SaveResponse:
        self.record("save-editorial")
        self.editorial_requests.append(request)
        self.editorial = EditorialContent(
            request.statement.text,
            request.statement.is_markdown,
        )
        return SaveResponse("saved")

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
        files: Mapping[str, bytes],
    ) -> UploadResponse:
        value = side(which)
        self.record(f"upload-{value}-{'-'.join(files)}")
        for name, content in files.items():
            normalized = content + b"|server" if self.normalize_uploads else content
            self.cases[(value, name)] = normalized
        reported = tuple(files) if self.reported_file_names is None else self.reported_file_names
        return UploadResponse(reported, self.upload_warning)

    def delete_testcase(self, problem_id: int, which: Which | str, name: str) -> None:
        value = side(which)
        self.record(f"delete-{value}-{name}")
        self.cases.pop((value, name), None)


def factory(clients: Mapping[int, FakeClient]) -> push_module.ClientFactory:
    return lambda problem: clients[problem.problem_id]


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def write_case(
    problem: ProblemLayout,
    name: str,
    input_data: bytes,
    output_data: bytes | None,
) -> Path:
    directory = problem.path / "generated" / "tests"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.in").write_bytes(input_data)
    if output_data is not None:
        (directory / f"{name}.diff").write_bytes(output_data)
    return directory


def test_unchanged_resources_do_not_write(tmp_path: Path) -> None:
    project = make_project(tmp_path, editorial=True)
    client = FakeClient(editorial=EditorialContent("local editorial\n", True))

    result = push(project, factory({1: client}))

    assert result.planned_items == ()
    assert result.completed_items == ()
    assert client.calls == [
        "get-problem",
        "get-generator",
        "get-judge",
        "get-editorial",
    ]


def test_every_problem_is_remotely_preflighted_before_first_write(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path, count=2, programs=False)
    first = FakeClient(
        problem_id=1,
        edit=ProblemEditContent(1, "remote\n", True, True, settings()),
        generator=GeneratorContent(),
        judge=JudgeCodeContent(),
    )
    second = FakeClient(problem_id=2, fail_on="get-problem")

    with pytest.raises(RuntimeError, match="get-problem failed"):
        push(project, factory({1: first, 2: second}))

    assert "save-problem" not in first.calls


def test_dry_run_reports_plan_without_remote_or_local_writes(tmp_path: Path) -> None:
    project = make_project(tmp_path, editorial=True)
    problem = project.problems[0]
    write_case(problem, "local", b"new-in", b"new-out")
    client = FakeClient(
        edit=ProblemEditContent(1, "remote statement\n", True, True, settings("remote")),
        generator=GeneratorContent("cpp17", "old generator\n", True, 1),
        judge=JudgeCodeContent("cpp17", "old judge\n", "AC"),
        editorial=EditorialContent("old editorial\n", True),
        cases={
            ("in", "remote"): b"remote-in",
            ("out", "remote"): b"remote-out",
        },
    )
    before = snapshot(tmp_path)

    result = push(
        project,
        factory({1: client}),
        include_testcases=True,
        prune=True,
        dry_run=True,
    )

    assert result.dry_run
    assert result.changed
    assert result.completed_items == ()
    assert not any(call.startswith(("save-", "upload-", "delete-")) for call in client.calls)
    assert snapshot(tmp_path) == before


def test_prune_requires_testcases_before_client_creation(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    called = False

    def make_client(problem: ProblemLayout) -> FakeClient:
        nonlocal called
        called = True
        return FakeClient()

    with pytest.raises(UsageError, match="requires --testcases"):
        push(project, make_client, prune=True)
    assert not called


@pytest.mark.parametrize("incomplete", [False, True])
def test_empty_or_incomplete_local_testcases_stop_before_api(
    tmp_path: Path,
    incomplete: bool,
) -> None:
    project = make_project(tmp_path)
    if incomplete:
        write_case(project.problems[0], "broken", b"input", None)
    called = False

    def make_client(problem: ProblemLayout) -> FakeClient:
        nonlocal called
        called = True
        return FakeClient()

    with pytest.raises(LayoutError):
        push(project, make_client, include_testcases=True)
    assert not called


def test_testcase_output_symlink_escape_stops_before_api(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = make_project(project_root)
    outside = tmp_path / "outside"
    testcase_dir = outside / "tests"
    testcase_dir.mkdir(parents=True)
    (testcase_dir / "sample.in").write_bytes(b"input")
    (testcase_dir / "sample.diff").write_bytes(b"output")
    (project.problems[0].path / "generated").symlink_to(
        outside,
        target_is_directory=True,
    )
    called = False

    def make_client(problem: ProblemLayout) -> FakeClient:
        nonlocal called
        called = True
        return FakeClient()

    with pytest.raises(LayoutError, match=r"symlink|escapes problem root"):
        push(project, make_client, include_testcases=True)

    assert not called


def test_testcases_upload_prune_and_refresh_server_normalization(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    problem = project.problems[0]
    directory = write_case(problem, "same", b"new-input", b"same-output")
    write_case(problem, "added", b"added-input", b"added-output")
    client = FakeClient(
        cases={
            ("in", "same"): b"old-input",
            ("out", "same"): b"same-output",
            ("in", "stale"): b"stale-input",
            ("out", "stale"): b"stale-output",
        },
        normalize_uploads=True,
    )
    sleeps: list[float] = []

    result = push(
        project,
        factory({1: client}),
        include_testcases=True,
        prune=True,
        testcase_refresh_delay=1.25,
        sleep=sleeps.append,
    )

    assert any(call.startswith("upload-in-") for call in client.calls)
    assert any(call.startswith("upload-out-") for call in client.calls)
    assert "delete-in-stale" in client.calls
    assert "delete-out-stale" in client.calls
    local = read_testcases(directory)
    assert set(local) == {"added", "same"}
    assert local["same"].input == b"new-input|server"
    assert local["same"].output == b"same-output"
    assert local["added"].input == b"added-input|server"
    assert local["added"].output == b"added-output|server"
    assert result.completed_items[-1].endswith("normalization refresh")
    assert sleeps == [1.25]


def test_push_uploads_and_refreshes_zero_byte_testcase_output(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    problem = project.problems[0]
    directory = write_case(problem, "sample", b"input", b"")
    client = FakeClient(
        cases={
            ("in", "sample"): b"input",
            ("out", "sample"): b"old-output",
        }
    )

    result = push(
        project,
        factory({1: client}),
        include_testcases=True,
        testcase_refresh_delay=0,
    )

    assert "upload-in-sample" not in client.calls
    assert "upload-out-sample" in client.calls
    assert client.cases[("in", "sample")] == b"input"
    assert client.cases[("out", "sample")] == b""
    refreshed = read_testcases(directory)["sample"]
    assert refreshed.input == b"input"
    assert refreshed.output == b""
    assert result.completed_items == (
        "problem 1 testcase outputs [sample]",
        "problem 1 testcase normalization refresh",
    )


def test_normalization_refresh_does_not_import_remote_only_cases_without_prune(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    problem = project.problems[0]
    directory = write_case(problem, "local", b"new-input", b"new-output")
    client = FakeClient(
        cases={
            ("in", "local"): b"old-input",
            ("out", "local"): b"new-output",
            ("in", "remote_only"): b"remote-input",
            ("out", "remote_only"): b"remote-output",
        }
    )

    push(
        project,
        factory({1: client}),
        include_testcases=True,
        testcase_refresh_delay=0,
    )

    local = read_testcases(directory)
    assert set(local) == {"local"}
    assert client.cases[("in", "remote_only")] == b"remote-input"
    assert client.cases[("out", "remote_only")] == b"remote-output"


def test_upload_response_warnings_and_changed_filenames_are_reported(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    write_case(project.problems[0], "sample", b"input", b"output")
    client = FakeClient(
        cases={},
        upload_warning="server normalized the upload",
        reported_file_names=("renamed",),
    )

    result = push(
        project,
        factory({1: client}),
        include_testcases=True,
        testcase_refresh_delay=0,
    )

    assert sum("server normalized the upload" in warning for warning in result.warnings) == 2
    assert (
        sum("server reported different file names" in warning for warning in result.warnings) == 2
    )


def test_empty_program_sources_explicitly_delete_remote_programs(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    problem = project.problems[0]
    (problem.path / "tests" / "generator.cpp").write_text("  \n", encoding="utf-8")
    (problem.path / "tests" / "judge.cpp").write_text("\n", encoding="utf-8")
    client = FakeClient(save_judge_status="")

    push(project, factory({1: client}))

    assert client.generator_requests[0].source == ""
    assert client.judge_requests[0].source == ""
    assert client.calls.count("get-judge") == 1


@dataclass
class FakeClock:
    value: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def changed_judge_project(tmp_path: Path) -> ProjectLayout:
    project = make_project(tmp_path)
    (project.problems[0].path / "tests" / "judge.cpp").write_text("new judge\n", encoding="utf-8")
    return project


def test_judge_wait_reaches_ac(tmp_path: Path) -> None:
    project = changed_judge_project(tmp_path)
    clock = FakeClock()
    client = FakeClient(
        save_judge_status="WJ",
        poll_statuses=["Judge", "AC"],
    )

    result = push(
        project,
        factory({1: client}),
        judge_timeout=10,
        judge_poll_interval=3,
        sleep=clock.sleep,
        monotonic=clock.now,
    )

    assert result.warnings == ()
    assert clock.sleeps == [3, 3]
    assert client.calls.count("get-judge") == 3


def test_judge_ce_is_a_partial_execution_error(tmp_path: Path) -> None:
    project = changed_judge_project(tmp_path)
    clock = FakeClock()
    client = FakeClient(save_judge_status="WJ", poll_statuses=["CE"])

    with pytest.raises(PushExecutionError) as caught:
        push(
            project,
            factory({1: client}),
            judge_timeout=10,
            sleep=clock.sleep,
            monotonic=clock.now,
        )

    assert isinstance(caught.value.cause, JudgeCompileError)
    assert caught.value.failed_item.endswith("judge compilation")
    assert caught.value.completed_items == ("problem 1 judge",)


def test_judge_timeout_is_warning(tmp_path: Path) -> None:
    project = changed_judge_project(tmp_path)
    clock = FakeClock()
    client = FakeClient(save_judge_status="WJ")

    result = push(
        project,
        factory({1: client}),
        judge_timeout=5,
        judge_poll_interval=3,
        sleep=clock.sleep,
        monotonic=clock.now,
    )

    assert any("timed out" in warning for warning in result.warnings)
    assert clock.sleeps == [3, 2]


def test_judge_wait_retries_transient_missing_status(tmp_path: Path) -> None:
    project = changed_judge_project(tmp_path)
    clock = FakeClock()
    client = FakeClient(save_judge_status="WJ", poll_statuses=["AC"])
    original_get = client.get_judge_code
    polls = 0

    def transient(problem_id: int) -> JudgeCodeContent | None:
        nonlocal polls
        if client.judge_saved:
            polls += 1
            if polls == 1:
                client.record("get-judge")
                return None
        return original_get(problem_id)

    client.get_judge_code = transient  # type: ignore[method-assign]
    result = push(
        project,
        factory({1: client}),
        judge_timeout=10,
        judge_poll_interval=3,
        sleep=clock.sleep,
        monotonic=clock.now,
    )
    assert result.warnings == ()
    assert clock.sleeps == [3, 3]


def test_no_wait_skips_judge_polling(tmp_path: Path) -> None:
    project = changed_judge_project(tmp_path)
    client = FakeClient(save_judge_status="WJ")

    result = push(project, factory({1: client}), no_wait_compile=True)

    assert client.calls.count("get-judge") == 1
    assert any("not awaited" in warning for warning in result.warnings)


def test_failure_reports_completed_items_in_write_order(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    problem = project.problems[0]
    (problem.path / "PROBLEM").write_text(
        render_problem_block(ProblemConfig(1, settings("changed"), "A")),
        encoding="utf-8",
    )
    (problem.path / "tests" / "generator.cpp").write_text("changed generator\n", encoding="utf-8")
    (problem.path / "tests" / "judge.cpp").write_text("changed judge\n", encoding="utf-8")
    client = FakeClient(fail_on="save-judge")

    with pytest.raises(PushExecutionError) as caught:
        push(project, factory({1: client}))

    assert caught.value.failed_item == "problem 1 judge"
    assert caught.value.completed_items == (
        "problem 1 settings/statement",
        "problem 1 generator",
    )
    assert client.calls[-3:] == ["save-problem", "save-generator", "save-judge"]


def test_interrupt_reports_completed_remote_items(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    problem = project.problems[0]
    (problem.path / "PROBLEM").write_text(
        render_problem_block(ProblemConfig(1, settings("changed"), "A")),
        encoding="utf-8",
    )
    (problem.path / "tests" / "generator.cpp").write_text("changed generator\n", encoding="utf-8")
    client = FakeClient()

    def interrupt(_problem_id: int, _request: GeneratorRequest) -> SaveResponse:
        raise KeyboardInterrupt

    client.save_generator = interrupt  # type: ignore[method-assign]
    with pytest.raises(PushInterrupted) as caught:
        push(project, factory({1: client}))

    assert caught.value.failed_item == "problem 1 generator"
    assert caught.value.completed_items == ("problem 1 settings/statement",)
    assert "remote application state is unknown" in str(caught.value)


def test_generate_forces_only_generator_write(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    client = FakeClient()

    result = push(project, factory({1: client}), generate=True)

    assert result.planned_items == ("problem 1 generator",)
    assert len(client.generator_requests) == 1
    assert client.generator_requests[0].generate is True


def test_generate_without_local_generator_fails_before_api(tmp_path: Path) -> None:
    project = make_project(tmp_path, programs=False)
    called = False

    def make_client(problem: ProblemLayout) -> FakeClient:
        nonlocal called
        called = True
        return FakeClient()

    with pytest.raises(
        ValidationError,
        match="--generate requires a local yukicoder_generator declaration",
    ):
        push(project, make_client, generate=True)

    assert not called


@pytest.mark.parametrize("test_case_num", [0, 51])
def test_generate_rejects_server_invalid_case_count_before_api(
    tmp_path: Path,
    test_case_num: int,
) -> None:
    project = make_project(tmp_path)
    problem = project.problems[0]
    assert problem.testset is not None
    problem.testset.config_path.write_text(
        render_testset_block(
            RimeTestsetConfig(
                GeneratorConfig(
                    "cpp17",
                    "generator.cpp",
                    test_case_num,
                    "case",
                    "cxx",
                ),
                JudgeConfig("cpp17", "judge.cpp", "cxx"),
            )
        ),
        encoding="utf-8",
    )
    called = False

    def make_client(problem: ProblemLayout) -> FakeClient:
        nonlocal called
        called = True
        return FakeClient()

    with pytest.raises(
        ValidationError,
        match="test_case_num between 1 and 50",
    ):
        push(project, make_client, generate=True)

    assert not called


def test_partial_testcase_upload_reports_completed_side_and_retry_repairs_it(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    write_case(project.problems[0], "sample", b"input", b"output")
    client = FakeClient(cases={}, fail_on="upload-out-sample")

    with pytest.raises(PushExecutionError) as caught:
        push(
            project,
            factory({1: client}),
            include_testcases=True,
            testcase_refresh_delay=0,
        )

    assert caught.value.failed_item == "problem 1 testcase outputs [sample]"
    assert caught.value.completed_items == ("problem 1 testcase inputs [sample]",)
    assert client.cases == {("in", "sample"): b"input"}

    client.fail_on = None
    before = len(client.calls)
    result = push(
        project,
        factory({1: client}),
        include_testcases=True,
        testcase_refresh_delay=0,
    )

    retry_writes = [
        call for call in client.calls[before:] if call.startswith(("upload-", "delete-"))
    ]
    assert retry_writes == ["upload-out-sample"]
    assert result.planned_items == (
        "problem 1 testcase outputs [sample]",
        "problem 1 testcase normalization refresh",
    )
    assert read_testcases(project.problems[0].path / "generated" / "tests")["sample"] == (
        push_module.TestCaseData("sample", b"input", b"output")
    )


def test_partial_prune_reports_completed_side_and_retry_deletes_remaining_side(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    write_case(project.problems[0], "keep", b"in", b"out")
    client = FakeClient(
        cases={
            ("in", "keep"): b"in",
            ("out", "keep"): b"out",
            ("in", "stale"): b"stale-in",
            ("out", "stale"): b"stale-out",
        },
        fail_on="delete-out-stale",
    )

    with pytest.raises(PushExecutionError) as caught:
        push(
            project,
            factory({1: client}),
            include_testcases=True,
            prune=True,
            testcase_refresh_delay=0,
        )

    assert caught.value.failed_item == "problem 1 delete testcase output stale"
    assert caught.value.completed_items == ("problem 1 delete testcase input stale",)
    assert ("in", "stale") not in client.cases
    assert client.cases[("out", "stale")] == b"stale-out"

    client.fail_on = None
    before = len(client.calls)
    result = push(
        project,
        factory({1: client}),
        include_testcases=True,
        prune=True,
        testcase_refresh_delay=0,
    )

    retry_writes = [
        call for call in client.calls[before:] if call.startswith(("upload-", "delete-"))
    ]
    assert retry_writes == ["delete-out-stale"]
    assert result.planned_items == ("problem 1 delete testcase output stale",)


def test_testcase_refresh_tolerates_unrelated_incomplete_remote_case(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    directory = write_case(project.problems[0], "sample", b"new", b"output")
    client = FakeClient(
        cases={
            ("in", "sample"): b"old",
            ("out", "sample"): b"output",
            ("in", "unrelated"): b"orphan",
        }
    )

    result = push(
        project,
        factory({1: client}),
        include_testcases=True,
        testcase_refresh_delay=0,
    )

    assert result.completed_items[-1] == "problem 1 testcase normalization refresh"
    assert read_testcases(directory)["sample"].input == b"new"
    assert client.cases[("in", "unrelated")] == b"orphan"


def test_dry_run_prunes_only_existing_remote_sides(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    write_case(project.problems[0], "keep", b"in", b"out")
    client = FakeClient(
        cases={
            ("in", "keep"): b"in",
            ("out", "keep"): b"out",
            ("in", "input_only"): b"stale",
            ("out", "output_only"): b"stale",
        }
    )

    result = push(
        project,
        factory({1: client}),
        include_testcases=True,
        prune=True,
        dry_run=True,
    )

    assert result.planned_items == (
        "problem 1 delete testcase input input_only",
        "problem 1 delete testcase output output_only",
    )
    assert ("in", "input_only") in client.cases
    assert ("out", "output_only") in client.cases


class ValidatorClient(FakeClient):
    def __init__(
        self,
        remote: ValidatorContent,
        *,
        polls: list[ValidatorContent] | None = None,
        judging: tuple[str, ...] = ("Pending",),
        judge_compile_message: str = "",
    ) -> None:
        super().__init__()
        self.validator = remote
        self.validator_polls = list(polls or [])
        self.validator_requests: list[ValidatorRequest] = []
        self.validator_saved = False
        self.judging = judging
        self.judge_compile_message = judge_compile_message

    def statuses(self) -> list[StatusInfo]:
        self.record("statuses")
        return [StatusInfo(status, "judging") for status in self.judging]

    def get_validator(self, problem_id: int) -> ValidatorContent:
        self.record("get-validator")
        if self.validator_saved and self.validator_polls:
            self.validator = self.validator_polls.pop(0)
        return self.validator

    def save_validator(
        self,
        problem_id: int,
        request: ValidatorRequest,
    ) -> JudgeCodeSaveResponse:
        self.record("save-validator")
        self.validator_requests.append(request)
        self.validator_saved = True
        self.validator = ValidatorContent(
            lang_id=request.lang_id,
            source=request.source,
            status="Pending",
        )
        return JudgeCodeSaveResponse("saved", "Pending")

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent | None:
        code = super().get_judge_code(problem_id)
        if code is None:
            return None
        return JudgeCodeContent(
            code.lang_id,
            code.source,
            code.status,
            self.judge_compile_message,
        )


def add_local_validator(project: ProjectLayout, source: str) -> None:
    problem = project.problems[0]
    tests = problem.path / "tests"
    config = RimeTestsetConfig(
        GeneratorConfig("cpp17", "generator.cpp", 2, "case", "cxx"),
        JudgeConfig("cpp17", "judge.cpp", "cxx"),
        ValidatorConfig("cpp17", "validator.cpp", "cxx", {"flags": ["-Wall"]}),
    )
    (tests / "TESTSET").write_text(render_testset_block(config), encoding="utf-8")
    (tests / "validator.cpp").write_text(source, encoding="utf-8")


def test_validator_push_follows_testcase_refresh_and_polls_every_five_seconds(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    add_local_validator(project, "new validator\n")
    write_case(project.problems[0], "sample", b"input", b"output")
    clock = FakeClock()
    client = ValidatorClient(
        ValidatorContent("cpp17", "old validator\n", "AC"),
        polls=[ValidatorContent("cpp17", "new validator\n", "AC", cases=())],
    )

    result = push(
        project,
        factory({1: client}),
        include_testcases=True,
        testcase_refresh_delay=0,
        sleep=clock.sleep,
        monotonic=clock.now,
    )

    assert result.planned_items[-1] == "problem 1 validator"
    assert result.completed_items[-1] == "problem 1 validator"
    assert client.validator_requests == [ValidatorRequest("cpp17", "new validator\n")]
    assert client.calls.index("save-validator") > max(
        index for index, call in enumerate(client.calls) if call.startswith("get-out-")
    )
    assert client.calls.count("statuses") == 1
    assert clock.sleeps == [5.0]


def test_unchanged_ac_validator_skips_put_and_poll(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    add_local_validator(project, "same validator\n")
    clock = FakeClock()
    client = ValidatorClient(
        ValidatorContent("cpp17", "same validator\n", "AC", cases=()),
    )

    result = push(
        project,
        factory({1: client}),
        sleep=clock.sleep,
        monotonic=clock.now,
    )

    assert result.planned_items == ()
    assert client.validator_requests == []
    assert client.calls.count("get-validator") == 1
    assert clock.sleeps == []


def test_validator_no_wait_saves_without_polling(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    add_local_validator(project, "new validator\n")
    client = ValidatorClient(
        ValidatorContent("cpp17", "old validator\n", "AC"),
        polls=[ValidatorContent("cpp17", "new validator\n", "AC", cases=())],
    )

    result = push(project, factory({1: client}), no_wait_compile=True)

    assert client.validator_requests == [ValidatorRequest("cpp17", "new validator\n")]
    assert client.calls.count("get-validator") == 1
    assert any("validator validation was not awaited" in item for item in result.warnings)


def test_validator_terminal_failure_reports_case_details(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    add_local_validator(project, "new validator\n")
    clock = FakeClock()
    client = ValidatorClient(
        ValidatorContent("cpp17", "old validator\n", "AC"),
        polls=[
            ValidatorContent(
                "cpp17",
                "new validator\n",
                "WA",
                cases=(ValidatorCase("sample-01", "WA"),),
            )
        ],
    )

    with pytest.raises(PushExecutionError) as caught:
        push(
            project,
            factory({1: client}),
            sleep=clock.sleep,
            monotonic=clock.now,
        )

    assert isinstance(caught.value.cause, ValidatorValidationError)
    assert "sample-01 (WA)" in str(caught.value.cause)
    assert caught.value.completed_items == ("problem 1 validator",)


def test_empty_validator_source_deletes_without_waiting(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    add_local_validator(project, " \n")
    clock = FakeClock()
    client = ValidatorClient(
        ValidatorContent("cpp17", "remote validator\n", "AC", cases=()),
    )

    result = push(
        project,
        factory({1: client}),
        sleep=clock.sleep,
        monotonic=clock.now,
    )

    assert result.completed_items == ("problem 1 validator",)
    assert client.validator_requests == [ValidatorRequest("cpp17", "")]
    assert "statuses" not in client.calls
    assert clock.sleeps == []


def test_judge_wait_uses_server_judging_categories(tmp_path: Path) -> None:
    project = changed_judge_project(tmp_path)
    clock = FakeClock()
    client = ValidatorClient(
        ValidatorContent(),
        judging=("CompileQueue",),
    )
    client.save_judge_status = "CompileQueue"
    client.poll_statuses = ["AC"]

    result = push(
        project,
        factory({1: client}),
        sleep=clock.sleep,
        monotonic=clock.now,
    )

    assert result.warnings == ()
    assert clock.sleeps == [3.0]


def test_judge_terminal_status_includes_compile_message(tmp_path: Path) -> None:
    project = changed_judge_project(tmp_path)
    clock = FakeClock()
    client = ValidatorClient(
        ValidatorContent(),
        judging=("CompileQueue",),
        judge_compile_message="compiler rejected the source",
    )
    client.save_judge_status = "CompileQueue"
    client.poll_statuses = ["Rejected"]

    with pytest.raises(PushExecutionError) as caught:
        push(
            project,
            factory({1: client}),
            sleep=clock.sleep,
            monotonic=clock.now,
        )

    assert isinstance(caught.value.cause, JudgeCompileError)
    assert "Rejected" in str(caught.value.cause)
    assert "compiler rejected the source" in str(caught.value.cause)


def test_validator_timeout_uses_six_hundred_second_budget(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    add_local_validator(project, "new validator\n")
    clock = FakeClock()
    client = ValidatorClient(
        ValidatorContent("cpp17", "old validator\n", "AC"),
    )

    result = push(
        project,
        factory({1: client}),
        judge_timeout=1,
        judge_poll_interval=0.25,
        sleep=clock.sleep,
        monotonic=clock.now,
    )

    assert any("validator validation timed out" in item for item in result.warnings)
    assert len(clock.sleeps) == 120
    assert set(clock.sleeps) == {5.0}
    assert sum(clock.sleeps) == 600.0


def test_validator_compile_failure_includes_compile_message(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    add_local_validator(project, "new validator\n")
    clock = FakeClock()
    client = ValidatorClient(
        ValidatorContent("cpp17", "old validator\n", "AC"),
        polls=[
            ValidatorContent(
                "cpp17",
                "new validator\n",
                "CE",
                compile_message="validator compiler error",
            )
        ],
    )

    with pytest.raises(PushExecutionError) as caught:
        push(
            project,
            factory({1: client}),
            sleep=clock.sleep,
            monotonic=clock.now,
        )

    assert isinstance(caught.value.cause, ValidatorValidationError)
    assert "validator compiler error" in str(caught.value.cause)
