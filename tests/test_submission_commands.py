from __future__ import annotations

from pathlib import Path

import pytest

import yukitools_rime.commands.submission as submission_module
from yukitools_rime.api.types import SolutionRequest, StatusInfo, SubmissionInfo
from yukitools_rime.commands.submission import (
    SubmissionRecordError,
    SubmissionResponseError,
    manage_expected_solution,
    parse_submission_id,
    submit_solution,
    wait_for_submission,
)
from yukitools_rime.errors import ConfigError, FileOperationError, LayoutError, ValidationError
from yukitools_rime.layout import (
    ProblemLayout,
    ProjectLayout,
    SolutionLayout,
    TargetSelection,
)
from yukitools_rime.models import (
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    SolutionConfig,
)
from yukitools_rime.rime_config import parse_solution_config, render_solution_block


class FakeSubmission:
    def __init__(self, response: str = "123") -> None:
        self.response = response
        self.submits: list[tuple[int, str, str]] = []
        self.solutions: list[tuple[int, SolutionRequest]] = []
        self.status_calls = 0
        self.submission_calls: list[int] = []
        self.submission_results = [SubmissionInfo("AC", 1)]
        self.saved = object()

    def submit(self, problem_id: int, lang: str, source: str) -> str:
        self.submits.append((problem_id, lang, source))
        return self.response

    def set_solution(self, submission_id: int, request: SolutionRequest) -> object:
        self.solutions.append((submission_id, request))
        return self.saved

    def statuses(self) -> list[StatusInfo]:
        self.status_calls += 1
        return [
            StatusInfo("WJ", "judging"),
            StatusInfo("Pending", "judging"),
            StatusInfo("AC", "success"),
            StatusInfo("WA", "wrong"),
        ]

    def get_submission(self, submission_id: int) -> SubmissionInfo:
        self.submission_calls.append(submission_id)
        if len(self.submission_results) > 1:
            return self.submission_results.pop(0)
        return self.submission_results[0]


def selection(
    tmp_path: Path,
    *,
    config: object | None = None,
    source: bytes = b"print(1)\n",
    select_solution: bool = True,
) -> tuple[TargetSelection, ProblemLayout]:
    problem_path = tmp_path / "problem"
    solution_path = problem_path / "solution"
    solution_path.mkdir(parents=True)
    solution_config = config if config is not None else SolutionConfig("py", "main.py", "script")
    if isinstance(solution_config, SolutionConfig):
        (solution_path / solution_config.src).write_bytes(source)
        (solution_path / "SOLUTION").write_bytes(
            render_solution_block(solution_config).encode("utf-8")
        )
    solution = SolutionLayout(solution_path, solution_config)
    settings = ProblemSettings("title", "", 1, 1000, 256, "-", "0", False, False, 0, 0)
    problem = ProblemLayout(
        problem_path,
        ProblemConfig(42, settings, "A"),
        None,
        (solution,),
    )
    project = ProjectLayout(tmp_path, ProjectConfig(), (problem,))
    selected = TargetSelection(
        project,
        (problem,),
        solution_path if select_solution else problem_path,
        solution if select_solution else None,
    )
    return selected, problem


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("123", 123),
        (" 123\n", 123),
        ('{"SubmissionId": 10}', 10),
        ('{"submissionId": "11"}', 11),
        ('{"Id": 12}', 12),
        ('{"id": 13}', 13),
        ("14", 14),
        ('"15"', 15),
    ],
)
def test_parse_submission_id_variants(response: str, expected: int) -> None:
    assert parse_submission_id(response) == expected


@pytest.mark.parametrize(
    "response",
    ["", "nope", "{}", "[]", "0", "true", '{"id": 1, "Id": 2}', '{"id": false}'],
)
def test_parse_submission_id_rejects_ambiguous_or_invalid(response: str) -> None:
    with pytest.raises(SubmissionResponseError):
        parse_submission_id(response)


def test_submit_uses_managed_solution_owner_config_and_normalized_source(
    tmp_path: Path,
) -> None:
    selected, owner = selection(tmp_path, source=b"\xef\xbb\xbfprint(1)\r\n")
    fake = FakeSubmission('{"submissionId": 77}')
    created: list[ProblemLayout] = []

    def factory(problem: ProblemLayout) -> FakeSubmission:
        created.append(problem)
        return fake

    result = submit_solution(selected, factory)
    assert result.problem_id == 42
    assert result.submission_id == 77
    assert result.lang_id == "py"
    assert fake.submits == [(42, "py", "print(1)\n")]
    assert created == [owner]
    assert not result.already_submitted
    assert selected.solution is not None
    stored = parse_solution_config((selected.solution.path / "SOLUTION").read_text())
    assert stored.submission_id == 77


def test_submit_reloads_recorded_id_when_selection_is_reused(tmp_path: Path) -> None:
    selected, _ = selection(tmp_path)
    fake = FakeSubmission("77")

    first = submit_solution(selected, fake)
    second = submit_solution(selected, fake)

    assert not first.already_submitted
    assert second.already_submitted
    assert second.submission_id == 77
    assert fake.submits == [(42, "py", "print(1)\n")]


def test_submit_skips_recorded_solution_without_source_or_client(tmp_path: Path) -> None:
    selected, _ = selection(
        tmp_path,
        config=SolutionConfig("py", "main.py", "script", submission_id=55),
    )
    assert selected.solution is not None
    (selected.solution.path / "main.py").unlink()
    created = False
    reported: list[tuple[int, int]] = []

    def forbidden_factory(_problem: ProblemLayout) -> FakeSubmission:
        nonlocal created
        created = True
        raise AssertionError("client must not be created")

    result = submit_solution(
        selected,
        forbidden_factory,
        on_submitted=lambda problem_id, submission_id: reported.append((problem_id, submission_id)),
    )

    assert result.already_submitted
    assert result.submission_id == 55
    assert result.raw_response == ""
    assert not created
    assert reported == []


def test_force_submit_overwrites_recorded_id(tmp_path: Path) -> None:
    selected, _ = selection(
        tmp_path,
        config=SolutionConfig("py", "main.py", "script", submission_id=55),
    )
    fake = FakeSubmission("77")

    result = submit_solution(selected, fake, force=True)

    assert not result.already_submitted
    assert fake.submits == [(42, "py", "print(1)\n")]
    assert selected.solution is not None
    stored = parse_solution_config((selected.solution.path / "SOLUTION").read_text())
    assert stored.submission_id == 77


def test_force_submit_with_unknown_response_keeps_recorded_id(tmp_path: Path) -> None:
    selected, _ = selection(
        tmp_path,
        config=SolutionConfig("py", "main.py", "script", submission_id=55),
    )
    fake = FakeSubmission('{"accepted":true}')

    result = submit_solution(selected, fake, force=True)

    assert result.submission_id is None
    assert selected.solution is not None
    stored = parse_solution_config((selected.solution.path / "SOLUTION").read_text())
    assert stored.submission_id == 55


def test_submission_record_reloads_concurrent_solution_edits(tmp_path: Path) -> None:
    selected, _ = selection(tmp_path)
    fake = FakeSubmission("77")
    assert selected.solution is not None
    config_path = selected.solution.path / "SOLUTION"

    def edit_during_submit(problem_id: int, lang: str, source: str) -> str:
        fake.submits.append((problem_id, lang, source))
        edited = SolutionConfig(
            "py",
            "main.py",
            "script",
            rime_options={"edited": True},
        )
        config_path.write_bytes(render_solution_block(edited).encode("utf-8"))
        return "77"

    fake.submit = edit_during_submit  # type: ignore[method-assign]
    submit_solution(selected, fake)

    stored = parse_solution_config(config_path.read_text())
    assert stored.rime_options == {"edited": True}
    assert stored.submission_id == 77


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_submit_waits_using_server_status_categories_and_reports_result(
    tmp_path: Path,
) -> None:
    selected, _ = selection(tmp_path)
    fake = FakeSubmission('{"submissionId": 77}')
    fake.submission_results = [
        SubmissionInfo("", 0),
        SubmissionInfo("WJ", 0),
        SubmissionInfo("WA", 234),
    ]
    clock = FakeClock()

    result = submit_solution(
        selected,
        fake,
        wait=True,
        timeout=60,
        interval=5,
        clock=clock,
        sleep=clock.sleep,
    )

    assert result.submission_id == 77
    assert result.judge_status == "WA"
    assert result.run_time_ms == 234
    assert not result.wait_timed_out
    assert fake.status_calls == 1
    assert fake.submission_calls == [77, 77, 77]
    assert clock.sleeps == [5, 5, 5]


def test_submit_callback_preserves_id_before_polling_failure(tmp_path: Path) -> None:
    selected, _ = selection(tmp_path)
    fake = FakeSubmission('{"submissionId": 77}')
    reported: list[tuple[int, int]] = []

    def report_submitted(problem_id: int, submission_id: int) -> None:
        assert selected.solution is not None
        stored = parse_solution_config((selected.solution.path / "SOLUTION").read_text())
        assert stored.submission_id == 77
        reported.append((problem_id, submission_id))

    def fail_statuses() -> list[StatusInfo]:
        assert reported == [(42, 77)]
        raise RuntimeError("status endpoint failed")

    fake.statuses = fail_statuses  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="status endpoint failed"):
        submit_solution(
            selected,
            fake,
            wait=True,
            on_submitted=report_submitted,
        )

    assert reported == [(42, 77)]
    assert fake.submits == [(42, "py", "print(1)\n")]
    assert selected.solution is not None
    stored = parse_solution_config((selected.solution.path / "SOLUTION").read_text())
    assert stored.submission_id == 77


def test_submission_record_failure_reports_remote_id_and_skips_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected, _ = selection(
        tmp_path,
        config=SolutionConfig("py", "main.py", "script", submission_id=55),
    )
    fake = FakeSubmission("77")
    assert selected.solution is not None
    config_path = selected.solution.path / "SOLUTION"
    before = config_path.read_bytes()
    reported: list[tuple[int, int]] = []

    def fail_write(_path: Path, _source: str) -> None:
        raise ConfigError("disk full")

    monkeypatch.setattr(submission_module, "write_config_atomic", fail_write)

    with pytest.raises(SubmissionRecordError, match=r"submission 77 .*disk full"):
        submit_solution(
            selected,
            fake,
            force=True,
            wait=True,
            on_submitted=lambda problem_id, submission_id: reported.append(
                (problem_id, submission_id)
            ),
        )

    assert fake.submits == [(42, "py", "print(1)\n")]
    assert fake.status_calls == 0
    assert reported == []
    assert config_path.read_bytes() == before


def test_submit_rejects_non_boolean_force_before_source_or_client(
    tmp_path: Path,
) -> None:
    selected, _ = selection(tmp_path)
    assert selected.solution is not None
    (selected.solution.path / "main.py").unlink()
    fake = FakeSubmission()

    with pytest.raises(ValidationError, match="force"):
        submit_solution(selected, fake, force=1)  # type: ignore[arg-type]

    assert fake.submits == []


def test_wait_for_submission_times_out_at_deadline() -> None:
    fake = FakeSubmission()
    fake.submission_results = [SubmissionInfo("Pending", 0)]
    clock = FakeClock()

    result = wait_for_submission(
        fake,
        123,
        timeout=12,
        interval=5,
        clock=clock,
        sleep=clock.sleep,
    )

    assert result is None
    assert fake.submission_calls == [123, 123, 123]
    assert clock.sleeps == [5, 5, 2]
    assert clock.now == 12


@pytest.mark.parametrize(
    ("timeout", "interval", "message"),
    [
        (-1, 1, "timeout"),
        (float("nan"), 1, "timeout"),
        (float("inf"), 1, "timeout"),
        (True, 1, "timeout"),
        (1, 0, "interval"),
        (1, -1, "interval"),
        (1, float("nan"), "interval"),
        (1, True, "interval"),
    ],
)
def test_wait_for_submission_rejects_invalid_polling_values(
    timeout: float,
    interval: float,
    message: str,
) -> None:
    fake = FakeSubmission()
    with pytest.raises(ValidationError, match=message):
        wait_for_submission(fake, 1, timeout=timeout, interval=interval)
    assert fake.status_calls == 0


def test_submit_no_id_skips_waiting_even_when_requested(tmp_path: Path) -> None:
    selected, _ = selection(tmp_path)
    fake = FakeSubmission('{"accepted":true}')

    result = submit_solution(selected, fake, wait=True)

    assert result.submission_id is None
    assert result.judge_status is None
    assert not result.wait_timed_out
    assert fake.status_calls == 0


def test_submit_does_not_turn_an_unrecognized_success_response_into_a_retryable_error(
    tmp_path: Path,
) -> None:
    selected, _ = selection(tmp_path)
    fake = FakeSubmission('{"accepted":true}')

    result = submit_solution(selected, fake)

    assert result.problem_id == 42
    assert result.submission_id is None
    assert result.raw_response == '{"accepted":true}'
    assert fake.submits == [(42, "py", "print(1)\n")]


def test_submit_requires_explicit_managed_solution(tmp_path: Path) -> None:
    selected, _ = selection(tmp_path, select_solution=False)
    with pytest.raises(LayoutError, match="SOLUTION"):
        submit_solution(selected, FakeSubmission())


def test_submit_requires_solution_config_and_nonempty_utf8_regular_source(
    tmp_path: Path,
) -> None:
    selected, _ = selection(tmp_path, config=object())
    with pytest.raises(LayoutError, match="not a yukicoder solution"):
        submit_solution(selected, FakeSubmission())

    empty, _ = selection(tmp_path / "empty", source=b" \n")
    with pytest.raises(ValidationError, match="empty"):
        submit_solution(empty, FakeSubmission())

    invalid, _ = selection(tmp_path / "invalid", source=b"\xff")
    with pytest.raises(FileOperationError, match="UTF-8"):
        submit_solution(invalid, FakeSubmission())


def test_submit_rejects_symlink_source(tmp_path: Path) -> None:
    selected, _ = selection(tmp_path)
    assert selected.solution is not None
    source = selected.solution.path / "main.py"
    source.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text("secret")
    try:
        source.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(FileOperationError):
        submit_solution(selected, FakeSubmission())


def test_manage_expected_solution_summary_and_delete(tmp_path: Path) -> None:
    selected, _ = selection(tmp_path, select_solution=False)
    fake = FakeSubmission()
    summary_result = manage_expected_solution(55, selected, fake, summary="official")
    assert summary_result.summary == "official"
    assert not summary_result.deleted
    assert fake.solutions[-1][0] == 55
    assert fake.solutions[-1][1].to_api_dict() == {"summary": "official"}

    delete_result = manage_expected_solution(56, selected, fake, delete=True)
    assert delete_result.deleted
    assert delete_result.response is fake.saved
    assert fake.solutions[-1][1].to_api_dict() == {"delete": True}


@pytest.mark.parametrize(
    ("summary", "delete"),
    [(None, False), ("both", True), (" \n", False)],
)
def test_manage_expected_solution_requires_exact_nonempty_mode(
    tmp_path: Path,
    summary: str | None,
    delete: bool,
) -> None:
    selected, _ = selection(tmp_path, select_solution=False)
    fake = FakeSubmission()
    with pytest.raises(ValidationError):
        manage_expected_solution(55, selected, fake, summary=summary, delete=delete)
    assert not fake.solutions


def test_manage_expected_solution_requires_one_problem(tmp_path: Path) -> None:
    selected, problem = selection(tmp_path, select_solution=False)
    all_selection = TargetSelection(
        selected.project,
        (problem, problem),
        selected.project.root,
    )
    with pytest.raises(LayoutError, match="exactly one"):
        manage_expected_solution(1, all_selection, FakeSubmission(), delete=True)


def test_submission_rejects_non_selection_and_non_text_response(tmp_path: Path) -> None:
    selected, _ = selection(tmp_path)
    with pytest.raises(TypeError, match="TargetSelection"):
        submit_solution(object(), FakeSubmission())  # type: ignore[arg-type]
    with pytest.raises(SubmissionResponseError, match="must be text"):
        parse_submission_id(None)  # type: ignore[arg-type]

    fake = FakeSubmission('{"id": []}')
    result = submit_solution(selected, fake)
    assert result.submission_id is None


@pytest.mark.parametrize("submission_id", [True, 0, -1, "1"])
def test_manage_expected_solution_rejects_invalid_submission_ids(
    tmp_path: Path,
    submission_id: object,
) -> None:
    selected, _ = selection(tmp_path, select_solution=False)
    fake = FakeSubmission()
    with pytest.raises(ValidationError, match="submission_id"):
        manage_expected_solution(
            submission_id,  # type: ignore[arg-type]
            selected,
            fake,
            delete=True,
        )
    assert fake.solutions == []


def test_manage_expected_solution_rejects_non_boolean_delete(tmp_path: Path) -> None:
    selected, _ = selection(tmp_path, select_solution=False)
    fake = FakeSubmission()
    with pytest.raises(ValidationError, match="delete must be true or false"):
        manage_expected_solution(
            1,
            selected,
            fake,
            delete=1,  # type: ignore[arg-type]
        )
    assert fake.solutions == []
