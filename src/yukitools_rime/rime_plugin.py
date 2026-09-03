"""Runtime Rime adapters for the yukitools-rime configuration DSL.

Importing this module has no Rime dependency. Only install() imports Rime, which
keeps the standalone synchronization CLI usable without a Rime installation.
"""

from __future__ import annotations

import importlib
import os.path
from collections.abc import Callable
from typing import Any

from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    SolutionConfig,
)

_Handler = Callable[..., None]
_active: dict[str, _Handler] = {}


def _invoke(kind: str, **kwargs: object) -> None:
    handler = _active.get(kind)
    if handler is None:
        raise RuntimeError(f"{kind}() is only available while Rime is loading its matching config")
    handler(**kwargs)


def yukicoder_project(
    base_url: str = "https://yukicoder.me/api",
    rime_out_dir: str = "rime-out",
) -> None:
    _invoke("yukicoder_project", base_url=base_url, rime_out_dir=rime_out_dir)


def yukicoder_problem(
    *,
    problem_id: int,
    title: str,
    tags: str,
    level: float,
    time_limit_ms: int,
    memory_limit: int,
    eps_mode: str,
    eps: str | int | float,
    wip: bool,
    recruiting_tester: bool,
    problem_type: int,
    judge_type: int,
    show_ans: bool = False,
    enable_pure_judge: bool = False,
    force_single_server_judge: bool = False,
    allowed_langs: list[str] | tuple[str, ...] = (),
    rime_id: str,
    reference_solution: str | None = None,
    rime_options: dict[str, object] | None = None,
) -> None:
    _invoke(
        "yukicoder_problem",
        problem_id=problem_id,
        title=title,
        tags=tags,
        level=level,
        time_limit_ms=time_limit_ms,
        memory_limit=memory_limit,
        eps_mode=eps_mode,
        eps=eps,
        wip=wip,
        recruiting_tester=recruiting_tester,
        problem_type=problem_type,
        judge_type=judge_type,
        show_ans=show_ans,
        enable_pure_judge=enable_pure_judge,
        force_single_server_judge=force_single_server_judge,
        allowed_langs=allowed_langs,
        rime_id=rime_id,
        reference_solution=reference_solution,
        rime_options={} if rime_options is None else rime_options,
    )


def yukicoder_generator(
    *,
    lang_id: str,
    src: str,
    test_case_num: int,
    prefix: str | None = None,
    rime_kind: str | None = None,
    rime_options: dict[str, object] | None = None,
) -> None:
    _invoke(
        "yukicoder_generator",
        lang_id=lang_id,
        src=src,
        test_case_num=test_case_num,
        prefix=prefix,
        rime_kind=rime_kind,
        rime_options={} if rime_options is None else rime_options,
    )


def yukicoder_judge(
    *,
    lang_id: str,
    src: str,
    rime_kind: str | None = None,
    rime_options: dict[str, object] | None = None,
) -> None:
    _invoke(
        "yukicoder_judge",
        lang_id=lang_id,
        src=src,
        rime_kind=rime_kind,
        rime_options={} if rime_options is None else rime_options,
    )


def yukicoder_solution(
    *,
    lang_id: str,
    src: str,
    rime_kind: str | None = None,
    challenge_cases: list[str] | tuple[str, ...] = (),
    rime_options: dict[str, object] | None = None,
) -> None:
    _invoke(
        "yukicoder_solution",
        lang_id=lang_id,
        src=src,
        rime_kind=rime_kind,
        challenge_cases=challenge_cases,
        rime_options={} if rime_options is None else rime_options,
    )


def _is_within(root: str, candidate: str) -> bool:
    try:
        return os.path.commonpath(
            (os.path.realpath(root), os.path.realpath(candidate))
        ) == os.path.realpath(root)
    except ValueError:
        return False


def _validate_regular_source(target: Any, src: str) -> None:
    source_dir = getattr(target, "src_dir", None)
    if not isinstance(source_dir, str):
        return
    problem_dir = getattr(getattr(target, "problem", None), "base_dir", None)
    if os.path.islink(source_dir) or (
        isinstance(problem_dir, str) and not _is_within(problem_dir, source_dir)
    ):
        raise RuntimeError(f"Rime source directory is unsafe: {source_dir}")
    source = os.path.join(source_dir, src)
    if (
        os.path.islink(source)
        or not os.path.isfile(source)
        or os.path.dirname(os.path.realpath(source)) != os.path.realpath(source_dir)
    ):
        raise RuntimeError(f"source must be a direct regular file: {source}")


def _validated_problem_output(
    base_dir: str,
    config: ProjectConfig,
    project_dir: str | None = None,
) -> str:
    if os.path.islink(base_dir) or (
        project_dir is not None and not _is_within(project_dir, base_dir)
    ):
        raise RuntimeError(f"Rime problem directory is unsafe: {base_dir}")
    output = os.path.join(base_dir, config.rime_out_dir)
    if not _is_within(base_dir, output):
        raise RuntimeError(f"Rime output directory escapes problem root: {output}")
    if os.path.islink(output):
        raise RuntimeError(f"Rime output directory must not be a symlink: {output}")
    if os.path.lexists(output) and not os.path.isdir(output):
        raise RuntimeError(f"Rime output path must be a directory: {output}")
    for config_name in ("PROBLEM", "TESTSET", "SOLUTION"):
        if os.path.lexists(os.path.join(output, config_name)):
            raise RuntimeError(f"rime_out_dir collides with a Rime source directory: {output}")
    return output


def _validate_component_output(target: Any) -> None:
    output = getattr(target, "out_dir", None)
    root = getattr(getattr(target, "problem", None), "out_dir", None)
    if not isinstance(output, str) or not isinstance(root, str):
        return
    if os.path.islink(output) or not _is_within(root, output):
        raise RuntimeError(f"Rime component output directory is unsafe: {output}")
    if os.path.lexists(output) and not os.path.isdir(output):
        raise RuntimeError(f"Rime component output path must be a directory: {output}")


def _register_code(target: Any, config: Any, suffix: str, *, solution: bool = False) -> None:
    _validate_regular_source(target, config.src)
    if config.rime_kind is None:
        return
    name = f"{config.rime_kind}_{suffix}"
    directive = target.exports.get(name)
    if directive is None:
        raise RuntimeError(f"Rime code directive {name}() is not available")
    options = dict(config.rime_options)
    if solution:
        if "challenge_cases" in options:
            raise RuntimeError("rime_options must not contain challenge_cases")
        options["challenge_cases"] = (
            list(config.challenge_cases) if config.challenge_cases else None
        )
    directive(config.src, **options)


def _project_adapter(base: type[Any]) -> type[Any]:
    class Project(base):  # type: ignore[misc]
        _yukitools_rime_adapter = True

        def PreLoad(self, ui: Any) -> None:
            super().PreLoad(ui)
            self.yukicoder_config = None

            def define(**kwargs: object) -> None:
                if self.yukicoder_config is not None:
                    raise RuntimeError("multiple yukicoder_project() declarations")
                self.yukicoder_config = ProjectConfig(**kwargs)  # type: ignore[arg-type]

            self.exports["yukicoder_project"] = define
            _active["yukicoder_project"] = define

        def PostLoad(self, ui: Any) -> None:
            if self.yukicoder_config is None:
                raise RuntimeError("yukicoder_project() declaration is missing")
            try:
                super().PostLoad(ui)
            finally:
                _active.pop("yukicoder_project", None)

    Project.__name__ = "Project"
    Project.__qualname__ = "Project"
    return Project


def _problem_adapter(base: type[Any]) -> type[Any]:
    class Problem(base):  # type: ignore[misc]
        _yukitools_rime_adapter = True

        def PreLoad(self, ui: Any) -> None:
            super().PreLoad(ui)
            project = getattr(self, "project", None)
            project_config = getattr(project, "yukicoder_config", None)
            if project_config is not None:
                self.out_dir = _validated_problem_output(
                    self.base_dir, project_config, getattr(project, "base_dir", None)
                )
            self.yukicoder_config = None
            rime_problem = self.exports["problem"]

            def define(**kwargs: object) -> None:
                if self.yukicoder_config is not None:
                    raise RuntimeError("multiple yukicoder_problem() declarations")
                values = dict(kwargs)
                setting_names = {
                    "title",
                    "tags",
                    "level",
                    "time_limit_ms",
                    "memory_limit",
                    "eps_mode",
                    "eps",
                    "wip",
                    "recruiting_tester",
                    "problem_type",
                    "judge_type",
                    "show_ans",
                    "enable_pure_judge",
                    "force_single_server_judge",
                    "allowed_langs",
                }
                setting_values = {
                    key: values.pop(key) for key in tuple(values) if key in setting_names
                }
                settings = ProblemSettings(**setting_values)  # type: ignore[arg-type]
                config = ProblemConfig(settings=settings, **values)  # type: ignore[arg-type]
                options = dict(config.rime_options)
                collisions = {"time_limit", "reference_solution", "title", "id"} & options.keys()
                if collisions:
                    raise RuntimeError(
                        "rime_options conflicts with " + ", ".join(sorted(collisions))
                    )
                rime_problem(
                    time_limit=settings.time_limit_ms / 1000.0,
                    reference_solution=config.reference_solution,
                    title=settings.title,
                    id=config.rime_id,
                    **options,
                )
                self.yukicoder_config = config
                self.yukicoder_problem_id = config.problem_id
                self.judge_type = settings.judge_type
                self.custom_judge = settings.judge_type != 0

            self.exports["yukicoder_problem"] = define
            _active["yukicoder_problem"] = define

        def PostLoad(self, ui: Any) -> None:
            try:
                if self.yukicoder_config is None:
                    super().PostLoad(ui)
                    return
                super().PostLoad(ui)
                solutions = getattr(self, "solutions", None)
                if solutions is not None:
                    sync_only = tuple(
                        solution
                        for solution in solutions
                        if getattr(solution, "yukicoder_sync_only", False)
                    )
                    if getattr(self, "reference_solution", None) in sync_only:
                        ui.errors.Error(
                            self, "A sync-only solution cannot be the reference solution"
                        )
                        self.reference_solution = None
                    self.solutions = [
                        solution for solution in solutions if solution not in sync_only
                    ]
            finally:
                _active.pop("yukicoder_problem", None)

    Problem.__name__ = "Problem"
    Problem.__qualname__ = "Problem"
    return Problem


def _testset_adapter(base: type[Any]) -> type[Any]:
    class Testset(base):  # type: ignore[misc]
        _yukitools_rime_adapter = True

        def PreLoad(self, ui: Any) -> None:
            super().PreLoad(ui)
            _validate_component_output(self)
            self.yukicoder_generator_config = None
            self.yukicoder_judge_config = None

            def generator(**kwargs: object) -> None:
                if self.yukicoder_generator_config is not None:
                    raise RuntimeError("multiple yukicoder_generator() declarations")
                config = GeneratorConfig(**kwargs)  # type: ignore[arg-type]
                judge_config = self.yukicoder_judge_config
                if (
                    judge_config is not None
                    and judge_config.src.casefold() == config.src.casefold()
                ):
                    raise RuntimeError("generator and judge source files must be distinct")
                _register_code(self, config, "generator")
                self.yukicoder_generator_config = config

            def judge(**kwargs: object) -> None:
                if self.yukicoder_judge_config is not None:
                    raise RuntimeError("multiple yukicoder_judge() declarations")
                generator_config = self.yukicoder_generator_config
                config = JudgeConfig(**kwargs)  # type: ignore[arg-type]
                if (
                    generator_config is not None
                    and generator_config.src.casefold() == config.src.casefold()
                ):
                    raise RuntimeError("generator and judge source files must be distinct")
                problem = getattr(self, "problem", None)
                if getattr(problem, "judge_type", None) == 0:
                    # Keep the remote source available for synchronization, but
                    # do not change ordinary Rime output comparison semantics.
                    _validate_regular_source(self, config.src)
                else:
                    _register_code(self, config, "judge")
                self.yukicoder_judge_config = config

            self.exports["yukicoder_generator"] = generator
            self.exports["yukicoder_judge"] = judge
            _active["yukicoder_generator"] = generator
            _active["yukicoder_judge"] = judge

        def PostLoad(self, ui: Any) -> None:
            try:
                super().PostLoad(ui)
            finally:
                _active.pop("yukicoder_generator", None)
                _active.pop("yukicoder_judge", None)

    Testset.__name__ = "Testset"
    Testset.__qualname__ = "Testset"
    return Testset


def _solution_adapter(base: type[Any]) -> type[Any]:
    class Solution(base):  # type: ignore[misc]
        _yukitools_rime_adapter = True

        def PreLoad(self, ui: Any) -> None:
            super().PreLoad(ui)
            _validate_component_output(self)
            self.yukicoder_config = None
            self.yukicoder_sync_only = False

            def define(**kwargs: object) -> None:
                if self.yukicoder_config is not None:
                    raise RuntimeError("multiple yukicoder_solution() declarations")
                config = SolutionConfig(**kwargs)  # type: ignore[arg-type]
                _register_code(self, config, "solution", solution=True)
                self.yukicoder_config = config
                self.yukicoder_sync_only = config.rime_kind is None

            self.exports["yukicoder_solution"] = define
            _active["yukicoder_solution"] = define

        def PostLoad(self, ui: Any) -> None:
            try:
                if self.yukicoder_config is None:
                    super().PostLoad(ui)
                    return
                if self.yukicoder_sync_only:
                    codes = getattr(self, "_codes", None)
                    if isinstance(codes, list):
                        codes.append(object())
                    self.challenge_cases = (
                        list(self.yukicoder_config.challenge_cases)
                        if self.yukicoder_config.challenge_cases
                        else None
                    )
                super().PostLoad(ui)
            finally:
                _active.pop("yukicoder_solution", None)

        def IsCorrect(self) -> bool:
            if getattr(self, "yukicoder_sync_only", False):
                return False
            return bool(super().IsCorrect())

    Solution.__name__ = "Solution"
    Solution.__qualname__ = "Solution"

    return Solution


def install() -> None:
    """Layer adapters over whichever target classes earlier plugins installed."""

    targets = importlib.import_module("rime.core.targets")
    registry = targets.registry
    factories = {
        "Project": _project_adapter,
        "Problem": _problem_adapter,
        "Testset": _testset_adapter,
        "Solution": _solution_adapter,
    }
    changed = False
    for name, factory in factories.items():
        current = registry.Get(name)
        if current is None:
            raise RuntimeError(f"Rime target registry has no {name}; load rime.basic first")
        if getattr(current, "_yukitools_rime_adapter", False):
            continue
        registry.Override(name, factory(current))
        changed = True
    if changed:
        raise targets.ReloadConfiguration("installed yukitools-rime target adapters")


__all__ = [
    "install",
    "yukicoder_generator",
    "yukicoder_judge",
    "yukicoder_problem",
    "yukicoder_project",
    "yukicoder_solution",
]
