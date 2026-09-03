"""Read-only command services for languages and remote testcase names."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from yukitools_rime.api.client import DEFAULT_BASE_URL, YukicoderClient
from yukitools_rime.api.types import Language
from yukitools_rime.errors import ValidationError
from yukitools_rime.layout import ProblemLayout, ProjectLayout, TargetSelection
from yukitools_rime.models import Which, validate_testcase_name


class LanguagesAPI(Protocol):
    def languages(self) -> list[Language]: ...


class TestcaseQueryAPI(Protocol):
    def list_testcases(self, problem_id: int, which: Which | str) -> list[str]: ...


LanguagesClientFactory = Callable[[], LanguagesAPI]
ProblemClientFactory = Callable[[ProblemLayout], TestcaseQueryAPI]


@dataclass(frozen=True, slots=True)
class TestcaseListResult:
    """Names returned for one problem and one input/output side."""

    problem_id: int
    rime_id: str
    problem_path: Path
    which: Which
    names: tuple[str, ...]


def _languages_client(
    source: LanguagesAPI | LanguagesClientFactory,
) -> LanguagesAPI:
    if hasattr(source, "languages"):
        return cast(LanguagesAPI, source)
    return source()


def list_languages(
    client: LanguagesAPI | LanguagesClientFactory | None = None,
    *,
    include_disabled: bool = False,
    base_url: str = DEFAULT_BASE_URL,
) -> tuple[Language, ...]:
    """List languages without requiring or discovering a Rime project."""

    if not isinstance(include_disabled, bool):
        raise ValidationError("include_disabled must be true or false")
    if client is None:
        with YukicoderClient.anonymous(base_url) as anonymous:
            languages = anonymous.languages()
    else:
        languages = _languages_client(client).languages()
    for language in languages:
        if not isinstance(language, Language):
            raise ValidationError("languages response contains an invalid entry")
    if not include_disabled:
        languages = [
            language for language in languages if language.status in {"", "enable"}
        ]
    return tuple(languages)


def _selected_problems(
    target: ProjectLayout | TargetSelection,
) -> tuple[ProblemLayout, ...]:
    if isinstance(target, ProjectLayout):
        return target.problems
    if isinstance(target, TargetSelection):
        return target.problems
    raise TypeError("target must be ProjectLayout or TargetSelection")


def _problem_client(
    source: TestcaseQueryAPI | ProblemClientFactory,
    problem: ProblemLayout,
) -> TestcaseQueryAPI:
    if hasattr(source, "list_testcases"):
        return cast(TestcaseQueryAPI, source)
    return source(problem)


def _which(which: Which | str | None) -> tuple[Which, ...]:
    if which is None:
        return Which.IN, Which.OUT
    if isinstance(which, Which):
        return (which,)
    try:
        return (Which(which),)
    except ValueError as exc:
        raise ValidationError("which must be 'in', 'out', or None") from exc


def _names(raw: list[str], *, problem_id: int, which: Which) -> tuple[str, ...]:
    names = tuple(
        validate_testcase_name(
            name,
            label=f"problem {problem_id} remote {which.value} testcase",
        )
        for name in raw
    )
    if len(names) != len(set(names)):
        raise ValidationError(
            f"problem {problem_id} remote {which.value} list contains duplicate names"
        )
    return tuple(sorted(names))


def list_remote_testcases(
    target: ProjectLayout | TargetSelection,
    client: TestcaseQueryAPI | ProblemClientFactory,
    *,
    which: Which | str | None = None,
) -> tuple[TestcaseListResult, ...]:
    """List names only; testcase bodies are deliberately never requested."""

    sides = _which(which)
    results: list[TestcaseListResult] = []
    for problem in _selected_problems(target):
        problem_client = _problem_client(client, problem)
        for side in sides:
            names = _names(
                problem_client.list_testcases(problem.problem_id, side),
                problem_id=problem.problem_id,
                which=side,
            )
            results.append(
                TestcaseListResult(
                    problem_id=problem.problem_id,
                    rime_id=problem.config.rime_id,
                    problem_path=problem.path,
                    which=side,
                    names=names,
                )
            )
    return tuple(results)


query_languages = list_languages
query_testcases = list_remote_testcases


__all__ = [
    "LanguagesAPI",
    "LanguagesClientFactory",
    "ProblemClientFactory",
    "TestcaseListResult",
    "TestcaseQueryAPI",
    "list_languages",
    "list_remote_testcases",
    "query_languages",
    "query_testcases",
]
