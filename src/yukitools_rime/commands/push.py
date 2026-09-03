"""Preflighted local-to-remote synchronization."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol, TypeAlias, TypeVar

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
)
from yukitools_rime.errors import APIError, LayoutError, UsageError, ValidationError
from yukitools_rime.files import normalize_text, read_text, require_regular_file
from yukitools_rime.layout import (
    ProblemLayout,
    ProjectLayout,
    TargetSelection,
    TestCaseData,
    discover_project,
    load_problem,
    read_testcases,
)
from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProjectConfig,
    Statement,
    Which,
)
from yukitools_rime.rime_config import parse_problem_config, parse_testset_config
from yukitools_rime.testcase_sync import (
    TestcaseAPI,
    TestcaseSnapshot,
    batch_upload_files,
    fetch_remote_snapshot,
    replace_local_snapshot,
)


class PushClient(TestcaseAPI, Protocol):
    """Remote operations needed by push."""

    def get_problem_edit(self, problem_id: int) -> ProblemEditContent: ...

    def save_problem_edit(self, problem_id: int, request: ProblemEditRequest) -> object: ...

    def get_generator(self, problem_id: int) -> GeneratorContent | None: ...

    def save_generator(self, problem_id: int, request: GeneratorRequest) -> object: ...

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent | None: ...

    def save_judge_code(
        self, problem_id: int, request: JudgeCodeRequest
    ) -> JudgeCodeSaveResponse: ...

    def get_editorial(self, problem_id: int) -> EditorialContent: ...

    def save_editorial(self, problem_id: int, request: EditorialRequest) -> object: ...


ClientFactory: TypeAlias = Callable[[ProblemLayout], PushClient]
PushTarget: TypeAlias = ProjectLayout | TargetSelection | ProblemLayout
Sleep: TypeAlias = Callable[[float], None]
Clock: TypeAlias = Callable[[], float]


class JudgeCompileError(APIError):
    """A newly saved custom judge reached CE."""


class PushExecutionError(APIError):
    """A push stopped after zero or more irreversible remote operations."""

    def __init__(
        self,
        failed_item: str,
        completed_items: tuple[str, ...],
        cause: Exception,
    ) -> None:
        completed = ", ".join(completed_items) if completed_items else "(none)"
        super().__init__(
            f"{failed_item} failed; completed items: {completed}; cause: {cause}"
        )
        self.failed_item = failed_item
        self.completed_items = completed_items
        self.cause = cause


@dataclass(frozen=True, slots=True)
class PushProblemResult:
    problem_id: int
    path: Path
    planned_items: tuple[str, ...]
    completed_items: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.planned_items)


@dataclass(frozen=True, slots=True)
class PushResult:
    problems: tuple[PushProblemResult, ...]
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return any(problem.changed for problem in self.problems)

    @property
    def planned_items(self) -> tuple[str, ...]:
        return tuple(item for problem in self.problems for item in problem.planned_items)

    @property
    def completed_items(self) -> tuple[str, ...]:
        return tuple(item for problem in self.problems for item in problem.completed_items)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(warning for problem in self.problems for warning in problem.warnings)


@dataclass(frozen=True, slots=True)
class _LocalProgram:
    config: GeneratorConfig | JudgeConfig
    source: str

    @property
    def deleting(self) -> bool:
        return not self.source.strip()


@dataclass(frozen=True, slots=True)
class _LocalProblem:
    problem: ProblemLayout
    config: ProblemConfig
    statement: Statement
    generator: _LocalProgram | None
    judge: _LocalProgram | None
    editorial: Statement | None
    testcase_dir: Path | None
    testcases: Mapping[str, TestCaseData] | None


@dataclass(frozen=True, slots=True)
class _TestcasePlan:
    local: Mapping[str, TestCaseData]
    input_batches: tuple[dict[str, bytes], ...]
    output_batches: tuple[dict[str, bytes], ...]
    stale: tuple[str, ...]
    directory: Path

    @property
    def has_writes(self) -> bool:
        return bool(self.input_batches or self.output_batches or self.stale)


@dataclass(frozen=True, slots=True)
class _ProblemPlan:
    local: _LocalProblem
    client: PushClient
    problem_request: ProblemEditRequest | None
    generator_request: GeneratorRequest | None
    judge_request: JudgeCodeRequest | None
    editorial_request: EditorialRequest | None
    testcases: _TestcasePlan | None

    @property
    def planned_items(self) -> tuple[str, ...]:
        problem_id = self.local.problem.problem_id
        prefix = f"problem {problem_id}"
        items: list[str] = []
        if self.problem_request is not None:
            items.append(f"{prefix} settings/statement")
        if self.generator_request is not None:
            items.append(f"{prefix} generator")
        if self.judge_request is not None:
            items.append(f"{prefix} judge")
        if self.editorial_request is not None:
            items.append(f"{prefix} editorial")
        testcases = self.testcases
        if testcases is not None:
            for batch in testcases.input_batches:
                items.append(f"{prefix} testcase inputs [{', '.join(batch)}]")
            for batch in testcases.output_batches:
                items.append(f"{prefix} testcase outputs [{', '.join(batch)}]")
            for name in testcases.stale:
                items.append(f"{prefix} delete testcase input {name}")
                items.append(f"{prefix} delete testcase output {name}")
            if testcases.has_writes:
                items.append(f"{prefix} testcase normalization refresh")
        return tuple(items)


def _selected_problems(target: PushTarget) -> tuple[ProblemLayout, ...]:
    problems: tuple[ProblemLayout, ...]
    if isinstance(target, ProblemLayout):
        problems = (target,)
    elif isinstance(target, (ProjectLayout, TargetSelection)):
        problems = target.problems
    else:
        raise TypeError("target must be a ProjectLayout, TargetSelection, or ProblemLayout")
    if not problems:
        raise LayoutError("push target contains no managed problems")
    return problems


def _project_config(target: PushTarget) -> ProjectConfig:
    if isinstance(target, ProjectLayout):
        return target.config
    if isinstance(target, TargetSelection):
        return target.project.config
    return discover_project(target.path).config


def _document(problem: ProblemLayout, stem: str, *, required: bool) -> Statement | None:
    markdown = problem.path / f"{stem}.md"
    html = problem.path / f"{stem}.html"
    paths = tuple(path for path in (markdown, html) if path.exists() or path.is_symlink())
    if len(paths) > 1:
        raise LayoutError(f"{problem.path}: both {stem}.md and {stem}.html exist")
    if not paths:
        if required:
            raise LayoutError(f"{problem.path}: {stem}.md or {stem}.html is required")
        return None
    path = paths[0]
    statement = Statement(read_text(path), path.suffix == ".md")
    statement.validate_nonempty(label=stem)
    return statement


def _local_program(
    testset_path: Path,
    config: GeneratorConfig | JudgeConfig | None,
) -> _LocalProgram | None:
    if config is None:
        return None
    source_path = require_regular_file(testset_path, config.src, label="program source")
    return _LocalProgram(config, read_text(source_path))


def _preflight_local(
    problem: ProblemLayout,
    project_config: ProjectConfig,
    *,
    include_testcases: bool,
    generate: bool,
) -> _LocalProblem:
    current = load_problem(problem.path)
    config = parse_problem_config(read_text(current.config_path))
    statement = _document(current, "statement", required=True)
    assert statement is not None
    generator: _LocalProgram | None = None
    judge: _LocalProgram | None = None
    if current.testset is not None:
        testset = parse_testset_config(read_text(current.testset.config_path))
        generator = _local_program(current.testset.path, testset.generator)
        judge = _local_program(current.testset.path, testset.judge)
    if generate and generator is None:
        raise ValidationError(
            f"{current.path}: --generate requires a local "
            "yukicoder_generator declaration"
        )
    editorial = _document(current, "editorial", required=False)
    testcase_dir: Path | None = None
    testcases: Mapping[str, TestCaseData] | None = None
    if include_testcases:
        if current.testset is None:
            raise LayoutError(
                f"{current.path}: --testcases requires a direct-child TESTSET"
            )
        testcase_dir = current.testcase_dir(project_config)
        # This rejects a missing/empty directory and incomplete .in/.diff pairs
        # before any client is created or any remote write is possible.
        testcases = read_testcases(testcase_dir, require_nonempty=True)
    return _LocalProblem(
        current,
        config,
        statement,
        generator,
        judge,
        editorial,
        testcase_dir,
        testcases,
    )


def _same_document(remote_text: str, remote_markdown: bool, local: Statement) -> bool:
    return (
        remote_markdown == local.is_markdown
        and normalize_text(remote_text) == local.text
    )


def _testcase_plan(
    local: _LocalProblem,
    remote: TestcaseSnapshot,
    *,
    prune: bool,
) -> _TestcasePlan:
    assert local.testcase_dir is not None
    assert local.testcases is not None
    inputs = {
        name: case.input
        for name, case in local.testcases.items()
        if name not in remote or remote[name].input != case.input
    }
    outputs = {
        name: case.output
        for name, case in local.testcases.items()
        if name not in remote or remote[name].output != case.output
    }
    stale = tuple(sorted(set(remote) - set(local.testcases))) if prune else ()
    return _TestcasePlan(
        local.testcases,
        batch_upload_files(inputs),
        batch_upload_files(outputs),
        stale,
        local.testcase_dir,
    )


def _build_remote_plan(
    local: _LocalProblem,
    client: PushClient,
    *,
    include_testcases: bool,
    prune: bool,
    generate: bool,
) -> _ProblemPlan:
    problem_id = local.problem.problem_id
    edit = client.get_problem_edit(problem_id)
    if not isinstance(edit, ProblemEditContent):
        raise ValidationError("problem API returned an unexpected response type")
    if edit.problem_id != problem_id:
        raise ValidationError(
            f"requested problem {problem_id}, but remote returned problem {edit.problem_id}"
        )
    problem_request = None
    if edit.settings != local.config.settings or not _same_document(
        edit.content, edit.is_markdown, local.statement
    ):
        problem_request = ProblemEditRequest(local.config.settings, local.statement)

    generator_request: GeneratorRequest | None = None
    if local.generator is not None:
        generator_config = local.generator.config
        assert isinstance(generator_config, GeneratorConfig)
        remote_generator = client.get_generator(problem_id)
        if remote_generator is not None and not isinstance(
            remote_generator, GeneratorContent
        ):
            raise ValidationError("generator API returned an unexpected response type")
        deleting = local.generator.deleting
        remote_has_source = (
            remote_generator is not None and bool(remote_generator.source.strip())
        )
        changed = (
            remote_has_source
            if deleting
            else remote_generator is None
            or remote_generator.lang_id != generator_config.lang_id
            or remote_generator.test_case_num != generator_config.test_case_num
            or normalize_text(remote_generator.source) != local.generator.source
        )
        if changed or (generate and not deleting):
            generator_request = GeneratorRequest(
                generator_config.lang_id,
                "" if deleting else local.generator.source,
                generator_config.test_case_num,
                generate=True if generate and not deleting else None,
                prefix=generator_config.prefix,
            )

    judge_request: JudgeCodeRequest | None = None
    if local.judge is not None:
        judge_config = local.judge.config
        assert isinstance(judge_config, JudgeConfig)
        remote_judge = client.get_judge_code(problem_id)
        if remote_judge is not None and not isinstance(remote_judge, JudgeCodeContent):
            raise ValidationError("judge API returned an unexpected response type")
        deleting = local.judge.deleting
        remote_has_source = remote_judge is not None and bool(remote_judge.source.strip())
        changed = (
            remote_has_source
            if deleting
            else remote_judge is None
            or remote_judge.lang_id != judge_config.lang_id
            or normalize_text(remote_judge.source) != local.judge.source
        )
        if changed:
            judge_request = JudgeCodeRequest(
                judge_config.lang_id,
                "" if deleting else local.judge.source,
            )

    editorial_request: EditorialRequest | None = None
    if local.editorial is not None:
        remote_editorial = client.get_editorial(problem_id)
        if not isinstance(remote_editorial, EditorialContent):
            raise ValidationError("editorial API returned an unexpected response type")
        if not _same_document(
            remote_editorial.content,
            remote_editorial.is_markdown,
            local.editorial,
        ):
            editorial_request = EditorialRequest(local.editorial)

    testcase_plan = None
    if include_testcases:
        remote_cases = fetch_remote_snapshot(client, problem_id)
        testcase_plan = _testcase_plan(local, remote_cases, prune=prune)
    return _ProblemPlan(
        local,
        client,
        problem_request,
        generator_request,
        judge_request,
        editorial_request,
        testcase_plan,
    )


def _validate_options(
    *,
    include_testcases: bool,
    prune: bool,
    dry_run: bool,
    generate: bool,
    no_wait_compile: bool,
    judge_timeout: float,
    judge_poll_interval: float,
    testcase_refresh_delay: float,
) -> None:
    for name, value in (
        ("include_testcases", include_testcases),
        ("prune", prune),
        ("dry_run", dry_run),
        ("generate", generate),
        ("no_wait_compile", no_wait_compile),
    ):
        if not isinstance(value, bool):
            raise ValidationError(f"{name} must be true or false")
    if prune and not include_testcases:
        raise UsageError("--prune requires --testcases")
    for name, numeric_value, allow_zero in (
        ("judge_timeout", judge_timeout, True),
        ("judge_poll_interval", judge_poll_interval, False),
        ("testcase_refresh_delay", testcase_refresh_delay, True),
    ):
        if isinstance(numeric_value, bool) or not isinstance(
            numeric_value, (int, float)
        ):
            raise ValidationError(f"{name} must be a number")
        if (
            not math.isfinite(numeric_value)
            or numeric_value < 0
            or (not allow_zero and numeric_value == 0)
        ):
            qualifier = "non-negative" if allow_zero else "positive"
            raise ValidationError(f"{name} must be a finite {qualifier} number")


_T = TypeVar("_T")


def _perform(
    label: str,
    completed: list[str],
    operation: Callable[[], _T],
) -> _T:
    try:
        result = operation()
    except Exception as exc:
        raise PushExecutionError(label, tuple(completed), exc) from exc
    completed.append(label)
    return result


def _wait_for_judge(
    plan: _ProblemPlan,
    initial_status: str,
    *,
    no_wait_compile: bool,
    timeout: float,
    interval: float,
    sleep: Sleep,
    monotonic: Clock,
) -> str | None:
    problem_id = plan.local.problem.problem_id

    def outcome(status: str) -> str | None:
        if status == "AC":
            return None
        if status == "CE":
            raise JudgeCompileError(
                f"problem {problem_id} judge compilation failed with CE"
            )
        return status

    pending = outcome(initial_status)
    if pending is None:
        return None
    if no_wait_compile:
        return (
            f"problem {problem_id}: judge compilation was not awaited "
            f"(status={initial_status or 'unknown'})"
        )
    deadline = monotonic() + timeout
    status = initial_status
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return (
                f"problem {problem_id}: judge compilation timed out "
                f"(last status={status or 'unknown'})"
            )
        sleep(min(interval, remaining))
        code = plan.client.get_judge_code(problem_id)
        if code is None:
            return f"problem {problem_id}: judge compilation status API is unavailable"
        status = code.status
        pending = outcome(status)
        if pending is None:
            return None


def _execute_plan(
    plan: _ProblemPlan,
    completed: list[str],
    *,
    no_wait_compile: bool,
    judge_timeout: float,
    judge_poll_interval: float,
    testcase_refresh_delay: float,
    sleep: Sleep,
    monotonic: Clock,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    problem_id = plan.local.problem.problem_id
    prefix = f"problem {problem_id}"
    before = len(completed)
    warnings: list[str] = []
    problem_request = plan.problem_request
    if problem_request is not None:
        _perform(
            f"{prefix} settings/statement",
            completed,
            partial(plan.client.save_problem_edit, problem_id, problem_request),
        )
    generator_request = plan.generator_request
    if generator_request is not None:
        _perform(
            f"{prefix} generator",
            completed,
            partial(plan.client.save_generator, problem_id, generator_request),
        )
    judge_request = plan.judge_request
    if judge_request is not None:
        response = _perform(
            f"{prefix} judge",
            completed,
            partial(plan.client.save_judge_code, problem_id, judge_request),
        )
        if judge_request.source:
            try:
                warning = _wait_for_judge(
                    plan,
                    response.status,
                    no_wait_compile=no_wait_compile,
                    timeout=judge_timeout,
                    interval=judge_poll_interval,
                    sleep=sleep,
                    monotonic=monotonic,
                )
            except Exception as exc:
                raise PushExecutionError(
                    f"{prefix} judge compilation",
                    tuple(completed),
                    exc,
                ) from exc
            if warning is not None:
                warnings.append(warning)
    editorial_request = plan.editorial_request
    if editorial_request is not None:
        _perform(
            f"{prefix} editorial",
            completed,
            partial(plan.client.save_editorial, problem_id, editorial_request),
        )

    testcases = plan.testcases
    if testcases is not None and testcases.has_writes:
        for batch in testcases.input_batches:
            label = f"{prefix} testcase inputs [{', '.join(batch)}]"
            _perform(
                label,
                completed,
                partial(plan.client.upload_testcases, problem_id, Which.IN, batch),
            )
        for batch in testcases.output_batches:
            label = f"{prefix} testcase outputs [{', '.join(batch)}]"
            _perform(
                label,
                completed,
                partial(plan.client.upload_testcases, problem_id, Which.OUT, batch),
            )
        for name in testcases.stale:
            _perform(
                f"{prefix} delete testcase input {name}",
                completed,
                partial(plan.client.delete_testcase, problem_id, Which.IN, name),
            )
            _perform(
                f"{prefix} delete testcase output {name}",
                completed,
                partial(plan.client.delete_testcase, problem_id, Which.OUT, name),
            )

        def refresh() -> None:
            if testcase_refresh_delay:
                sleep(testcase_refresh_delay)
            normalized = fetch_remote_snapshot(plan.client, problem_id)
            replace_local_snapshot(testcases.directory, normalized)

        _perform(
            f"{prefix} testcase normalization refresh",
            completed,
            refresh,
        )
    return tuple(completed[before:]), tuple(warnings)


def push(
    target: PushTarget,
    client_factory: ClientFactory,
    *,
    include_testcases: bool = False,
    prune: bool = False,
    dry_run: bool = False,
    generate: bool = False,
    no_wait_compile: bool = False,
    judge_timeout: float = 180.0,
    judge_poll_interval: float = 3.0,
    testcase_refresh_delay: float = 2.0,
    sleep: Sleep = time.sleep,
    monotonic: Clock = time.monotonic,
) -> PushResult:
    """Validate and plan every selected problem before the first remote write."""

    _validate_options(
        include_testcases=include_testcases,
        prune=prune,
        dry_run=dry_run,
        generate=generate,
        no_wait_compile=no_wait_compile,
        judge_timeout=judge_timeout,
        judge_poll_interval=judge_poll_interval,
        testcase_refresh_delay=testcase_refresh_delay,
    )
    project_config = _project_config(target)
    # Phase one is entirely local and validates every selected problem.
    local = tuple(
        _preflight_local(
            problem,
            project_config,
            include_testcases=include_testcases,
            generate=generate,
        )
        for problem in _selected_problems(target)
    )
    # Phase two performs all remote reads and computes every mutation.
    plans = tuple(
        _build_remote_plan(
            problem,
            client_factory(problem.problem),
            include_testcases=include_testcases,
            prune=prune,
            generate=generate,
        )
        for problem in local
    )
    if dry_run:
        return PushResult(
            tuple(
                PushProblemResult(
                    plan.local.problem.problem_id,
                    plan.local.problem.path,
                    plan.planned_items,
                    (),
                )
                for plan in plans
            ),
            dry_run=True,
        )

    completed: list[str] = []
    results: list[PushProblemResult] = []
    for plan in plans:
        problem_completed, warnings = _execute_plan(
            plan,
            completed,
            no_wait_compile=no_wait_compile,
            judge_timeout=judge_timeout,
            judge_poll_interval=judge_poll_interval,
            testcase_refresh_delay=testcase_refresh_delay,
            sleep=sleep,
            monotonic=monotonic,
        )
        results.append(
            PushProblemResult(
                plan.local.problem.problem_id,
                plan.local.problem.path,
                plan.planned_items,
                problem_completed,
                warnings,
            )
        )
    return PushResult(tuple(results))


push_selection = push


__all__ = [
    "ClientFactory",
    "JudgeCompileError",
    "PushClient",
    "PushExecutionError",
    "PushProblemResult",
    "PushResult",
    "PushTarget",
    "push",
    "push_selection",
]
