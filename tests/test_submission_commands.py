from __future__ import annotations

from pathlib import Path

import pytest

from yukitools_rime.api.types import SolutionRequest, StatusInfo, SubmissionInfo
from yukitools_rime.commands.submission import (
    SubmissionResponseError,
    manage_expected_solution,
    parse_submission_id,
    submit_solution,
    wait_for_submission,
)
from yukitools_rime.errors import FileOperationError, LayoutError, ValidationError
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

    def fail_statuses() -> list[StatusInfo]:
        assert reported == [(42, 77)]
        raise RuntimeError("status endpoint failed")

    fake.statuses = fail_statuses  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="status endpoint failed"):
        submit_solution(
            selected,
            fake,
            wait=True,
            on_submitted=lambda problem_id, submission_id: reported.append(
                (problem_id, submission_id)
            ),
        )

    assert reported == [(42, 77)]
    assert fake.submits == [(42, "py", "print(1)\n")]


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
