"""Submission and expected-solution command services."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from yukitools_rime.api.types import SolutionRequest
from yukitools_rime.errors import LayoutError, ValidationError
from yukitools_rime.files import read_text, require_regular_file
from yukitools_rime.layout import ProblemLayout, TargetSelection
from yukitools_rime.models import SolutionConfig


class SubmissionAPI(Protocol):
    def submit(self, problem_id: int, lang: str, source: str) -> str: ...

    def set_solution(self, submission_id: int, request: SolutionRequest) -> object: ...


SubmissionClientFactory = Callable[[ProblemLayout], SubmissionAPI]


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    problem_id: int
    submission_id: int | None
    solution_path: Path
    source_path: Path
    lang_id: str
    raw_response: str


@dataclass(frozen=True, slots=True)
class ExpectedSolutionResult:
    problem_id: int
    submission_id: int
    deleted: bool
    summary: str | None
    response: object


class SubmissionResponseError(ValidationError):
    """The submit endpoint returned no unambiguous positive submission id."""


def _single_problem(selection: TargetSelection) -> ProblemLayout:
    if not isinstance(selection, TargetSelection):
        raise TypeError("selection must be TargetSelection")
    if len(selection.problems) != 1:
        raise LayoutError("target must identify exactly one managed problem")
    return selection.problems[0]


def _client(
    source: SubmissionAPI | SubmissionClientFactory,
    problem: ProblemLayout,
) -> SubmissionAPI:
    if hasattr(source, "submit") and hasattr(source, "set_solution"):
        return cast(SubmissionAPI, source)
    return source(problem)


def _positive_id(value: object) -> int:
    if isinstance(value, bool):
        raise SubmissionResponseError("submission id must be a positive integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value.strip(), 10)
        except ValueError as exc:
            raise SubmissionResponseError(f"submission id is not an integer: {value!r}") from exc
    else:
        raise SubmissionResponseError("submission id must be an integer")
    if result < 1:
        raise SubmissionResponseError("submission id must be a positive integer")
    return result


def parse_submission_id(response: str) -> int:
    """Parse bare decimal text or the known JSON response key spellings."""

    if not isinstance(response, str):
        raise SubmissionResponseError("submit response must be text")
    raw = response.strip()
    if not raw:
        raise SubmissionResponseError("submit response is empty")
    try:
        return _positive_id(raw)
    except SubmissionResponseError:
        pass
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise SubmissionResponseError("submit response contains no submission id") from exc
    if isinstance(decoded, (int, str)) and not isinstance(decoded, bool):
        return _positive_id(decoded)
    if not isinstance(decoded, dict):
        raise SubmissionResponseError("submit JSON response must be an object")
    candidates: list[int] = []
    for key in ("SubmissionId", "submissionId", "Id", "id"):
        if key in decoded:
            candidates.append(_positive_id(decoded[key]))
    if not candidates:
        raise SubmissionResponseError("submit JSON response contains no submission id")
    if len(set(candidates)) != 1:
        raise SubmissionResponseError("submit JSON response has conflicting submission ids")
    return candidates[0]


def submit_solution(
    selection: TargetSelection,
    client: SubmissionAPI | SubmissionClientFactory,
) -> SubmissionResult:
    """Submit the explicitly targeted managed SOLUTION using its owning problem."""

    problem = _single_problem(selection)
    solution = selection.solution
    if solution is None:
        raise LayoutError("target must be inside a managed SOLUTION directory")
    if not isinstance(solution.config, SolutionConfig):
        raise LayoutError(f"{solution.config_path}: not a yukicoder solution")
    source_path = require_regular_file(
        solution.path,
        solution.config.src,
        label="solution source",
    )
    source = read_text(source_path)
    if not source.strip():
        raise ValidationError(f"solution source is empty: {source_path}")
    raw_response = _client(client, problem).submit(
        problem.problem_id,
        solution.config.lang_id,
        source,
    )
    try:
        submission_id = parse_submission_id(raw_response)
    except SubmissionResponseError:
        submission_id = None
    return SubmissionResult(
        problem_id=problem.problem_id,
        submission_id=submission_id,
        solution_path=solution.path,
        source_path=source_path,
        lang_id=solution.config.lang_id,
        raw_response=raw_response,
    )


def manage_expected_solution(
    submission_id: int,
    selection: TargetSelection,
    client: SubmissionAPI | SubmissionClientFactory,
    *,
    summary: str | None = None,
    delete: bool = False,
) -> ExpectedSolutionResult:
    """Register a summary or delete the expected-solution marker, exclusively."""

    problem = _single_problem(selection)
    if isinstance(submission_id, bool) or not isinstance(submission_id, int) or submission_id < 1:
        raise ValidationError("submission_id must be a positive integer")
    if not isinstance(delete, bool):
        raise ValidationError("delete must be true or false")
    summary_mode = summary is not None
    if summary_mode == delete:
        raise ValidationError("specify exactly one of summary or delete")
    if summary is not None:
        if not isinstance(summary, str) or not summary.strip():
            raise ValidationError("summary must not be empty")
        request = SolutionRequest(summary=summary)
    else:
        request = SolutionRequest(delete=True)
    response = _client(client, problem).set_solution(submission_id, request)
    return ExpectedSolutionResult(
        problem_id=problem.problem_id,
        submission_id=submission_id,
        deleted=delete,
        summary=summary,
        response=response,
    )


set_expected_solution = manage_expected_solution
submit = submit_solution


__all__ = [
    "ExpectedSolutionResult",
    "SubmissionAPI",
    "SubmissionClientFactory",
    "SubmissionResponseError",
    "SubmissionResult",
    "manage_expected_solution",
    "parse_submission_id",
    "set_expected_solution",
    "submit",
    "submit_solution",
]
