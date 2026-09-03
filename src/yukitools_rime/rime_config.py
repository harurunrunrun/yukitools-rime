"""Static parsing and deterministic rendering of managed Rime configuration.

Rime configuration files are Python programs. The command line application must
not execute them, so this module deliberately accepts only top-level calls whose
arguments can be handled by ast.literal_eval.
"""

from __future__ import annotations

import ast
import os
import pprint
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from yukitools_rime.errors import ConfigError, ValidationError
from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    SolutionConfig,
)

BEGIN_MARKER = "# BEGIN YUKITOOLS-RIME"
END_MARKER = "# END YUKITOOLS-RIME"

_PROJECT_KEYS = ("base_url", "rime_out_dir")
_PROBLEM_KEYS = (
    "problem_id",
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
    "rime_id",
    "reference_solution",
    "rime_options",
)
_GENERATOR_KEYS = (
    "lang_id",
    "src",
    "test_case_num",
    "prefix",
    "rime_kind",
    "rime_options",
)
_JUDGE_KEYS = ("lang_id", "src", "rime_kind", "rime_options")
_SOLUTION_KEYS = (
    "lang_id",
    "src",
    "rime_kind",
    "challenge_cases",
    "rime_options",
)


@dataclass(slots=True)
class TestsetConfig:
    """The two optional yukicoder declarations allowed in a TESTSET."""

    generator: GeneratorConfig | None = None
    judge: JudgeConfig | None = None


def _syntax_error(exc: SyntaxError) -> ConfigError:
    location = f" at line {exc.lineno}" if exc.lineno is not None else ""
    return ConfigError(f"invalid Rime configuration syntax{location}: {exc.msg}")


def _parse_tree(source: str) -> ast.Module:
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise _syntax_error(exc) from exc


def _function_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _literal(node: ast.expr, *, function: str, keyword: str) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        line = getattr(node, "lineno", "?")
        raise ConfigError(
            f"{function}() argument {keyword!r} must be a literal (line {line})"
        ) from exc


def _find_call(
    source: str,
    function: str,
    allowed_keys: tuple[str, ...],
    *,
    required: bool = True,
) -> dict[str, object] | None:
    tree = _parse_tree(source)
    top_level: list[ast.Call] = []
    nested_lines: list[int] = []
    for statement in tree.body:
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and _function_name(statement.value) == function
        ):
            top_level.append(statement.value)
    top_level_ids = {id(call) for call in top_level}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _function_name(node) == function
            and id(node) not in top_level_ids
        ):
            nested_lines.append(node.lineno)
    if nested_lines:
        raise ConfigError(
            f"{function}() must be a top-level statement (line {nested_lines[0]})"
        )
    if len(top_level) > 1:
        raise ConfigError(f"multiple {function}() declarations are not allowed")
    if not top_level:
        if required:
            raise ConfigError(f"{function}() declaration is missing")
        return None
    call = top_level[0]
    if call.args:
        raise ConfigError(f"{function}() accepts keyword arguments only")
    values: dict[str, object] = {}
    allowed = set(allowed_keys)
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ConfigError(f"{function}() does not allow **kwargs expansion")
        if keyword.arg not in allowed:
            raise ConfigError(f"unknown {function}() argument: {keyword.arg}")
        if keyword.arg in values:
            raise ConfigError(f"duplicate {function}() argument: {keyword.arg}")
        values[keyword.arg] = _literal(
            keyword.value,
            function=function,
            keyword=keyword.arg,
        )
    return values


_ModelT = TypeVar("_ModelT")


def _construct(model: type[_ModelT], function: str, values: dict[str, object]) -> _ModelT:
    try:
        return model(**values)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ConfigError(f"invalid {function}() declaration: {exc}") from exc


def parse_project_config(source: str) -> ProjectConfig:
    """Parse the single yukicoder_project call in a PROJECT file."""

    values = _find_call(source, "yukicoder_project", _PROJECT_KEYS)
    assert values is not None
    return _construct(ProjectConfig, "yukicoder_project", values)


def parse_problem_config(source: str) -> ProblemConfig:
    """Parse the single yukicoder_problem call in a PROBLEM file."""

    values = _find_call(source, "yukicoder_problem", _PROBLEM_KEYS)
    assert values is not None
    settings_keys = {
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
    settings_values = {key: values.pop(key) for key in tuple(values) if key in settings_keys}
    settings = _construct(ProblemSettings, "yukicoder_problem", settings_values)
    values["settings"] = settings
    return _construct(ProblemConfig, "yukicoder_problem", values)


def parse_generator_config(source: str, *, required: bool = False) -> GeneratorConfig | None:
    """Parse an optional yukicoder_generator declaration."""

    values = _find_call(
        source,
        "yukicoder_generator",
        _GENERATOR_KEYS,
        required=required,
    )
    if values is None:
        return None
    return _construct(GeneratorConfig, "yukicoder_generator", values)


def parse_judge_config(source: str, *, required: bool = False) -> JudgeConfig | None:
    """Parse an optional yukicoder_judge declaration."""

    values = _find_call(source, "yukicoder_judge", _JUDGE_KEYS, required=required)
    if values is None:
        return None
    return _construct(JudgeConfig, "yukicoder_judge", values)


def parse_testset_config(source: str) -> TestsetConfig:
    """Parse the optional generator and judge declarations in a TESTSET."""

    return TestsetConfig(
        generator=parse_generator_config(source),
        judge=parse_judge_config(source),
    )


def parse_solution_config(source: str) -> SolutionConfig:
    """Parse the single yukicoder_solution call in a SOLUTION file."""

    values = _find_call(source, "yukicoder_solution", _SOLUTION_KEYS)
    assert values is not None
    return _construct(SolutionConfig, "yukicoder_solution", values)


def _format_literal(value: object) -> str:
    if isinstance(value, dict):
        value = {key: value[key] for key in sorted(value)}
    return pprint.pformat(value, width=88, sort_dicts=True)


def _render_call(function: str, pairs: list[tuple[str, object]]) -> str:
    lines = [f"{function}("]
    for key, value in pairs:
        rendered = _format_literal(value).replace("\n", "\n    ")
        lines.append(f"    {key}={rendered},")
    lines.append(")")
    return "\n".join(lines)


def _managed(body: str) -> str:
    return f"{BEGIN_MARKER}\n{body.rstrip()}\n{END_MARKER}\n"


def render_project_block(config: ProjectConfig) -> str:
    """Render the complete managed block installed in PROJECT."""

    call = _render_call(
        "yukicoder_project",
        [("base_url", config.base_url), ("rime_out_dir", config.rime_out_dir)],
    )
    body = (
        "from yukitools_rime.rime_plugin import install, yukicoder_project\n\n"
        "install()\n"
        f"{call}"
    )
    return _managed(body)


def render_problem_block(config: ProblemConfig) -> str:
    """Render a deterministic managed PROBLEM block."""

    settings = config.settings
    pairs = [
        ("problem_id", config.problem_id),
        ("title", settings.title),
        ("tags", settings.tags),
        ("level", settings.level),
        ("time_limit_ms", settings.time_limit_ms),
        ("memory_limit", settings.memory_limit),
        ("eps_mode", settings.eps_mode),
        ("eps", settings.eps),
        ("wip", settings.wip),
        ("recruiting_tester", settings.recruiting_tester),
        ("problem_type", settings.problem_type),
        ("judge_type", settings.judge_type),
        ("show_ans", settings.show_ans),
        ("enable_pure_judge", settings.enable_pure_judge),
        ("force_single_server_judge", settings.force_single_server_judge),
        ("allowed_langs", list(settings.allowed_langs)),
        ("rime_id", config.rime_id),
        ("reference_solution", config.reference_solution),
        ("rime_options", config.rime_options),
    ]
    return _managed(_render_call("yukicoder_problem", pairs))


def render_testset_block(config: TestsetConfig) -> str:
    """Render generator and judge declarations in their stable order."""

    calls: list[str] = []
    if config.generator is not None:
        generator = config.generator
        calls.append(
            _render_call(
                "yukicoder_generator",
                [
                    ("lang_id", generator.lang_id),
                    ("src", generator.src),
                    ("test_case_num", generator.test_case_num),
                    ("prefix", generator.prefix),
                    ("rime_kind", generator.rime_kind),
                    ("rime_options", generator.rime_options),
                ],
            )
        )
    if config.judge is not None:
        judge = config.judge
        calls.append(
            _render_call(
                "yukicoder_judge",
                [
                    ("lang_id", judge.lang_id),
                    ("src", judge.src),
                    ("rime_kind", judge.rime_kind),
                    ("rime_options", judge.rime_options),
                ],
            )
        )
    return _managed("\n\n".join(calls))


def render_solution_block(config: SolutionConfig) -> str:
    """Render a deterministic managed SOLUTION block."""

    return _managed(
        _render_call(
            "yukicoder_solution",
            [
                ("lang_id", config.lang_id),
                ("src", config.src),
                ("rime_kind", config.rime_kind),
                ("challenge_cases", list(config.challenge_cases)),
                ("rime_options", config.rime_options),
            ],
        )
    )


def extract_managed_block(source: str) -> str | None:
    """Return a complete managed block, or None when it is absent."""

    begin_count = source.count(BEGIN_MARKER)
    end_count = source.count(END_MARKER)
    if begin_count == end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise ConfigError("managed Rime configuration markers are unbalanced or duplicated")
    begin = source.index(BEGIN_MARKER)
    end = source.index(END_MARKER, begin) + len(END_MARKER)
    if source.find(END_MARKER) < begin:
        raise ConfigError("managed Rime configuration end marker precedes its begin marker")
    return source[begin:end]


def upsert_managed_block(source: str, rendered_block: str) -> str:
    """Insert or replace a managed block while preserving surrounding content."""

    newline = "\r\n" if "\r\n" in source else "\n"
    normalized = rendered_block.replace("\r\n", "\n").strip("\n").replace("\n", newline)
    old = extract_managed_block(source)
    if old is not None:
        start = source.index(BEGIN_MARKER)
        end = source.index(END_MARKER, start) + len(END_MARKER)
        return f"{source[:start]}{normalized}{source[end:]}"
    if not source:
        return f"{normalized}{newline}"
    separator = newline if source.endswith(("\n", "\r")) else newline * 2
    if source.endswith(newline * 2):
        separator = ""
    elif source.endswith(newline):
        separator = newline
    return f"{source}{separator}{normalized}{newline}"


def merge_remote_problem(local: ProblemConfig, remote: ProblemConfig) -> ProblemConfig:
    """Use remote server fields while retaining all local-only Rime fields."""

    return ProblemConfig(
        problem_id=remote.problem_id,
        settings=remote.settings,
        rime_id=local.rime_id,
        reference_solution=local.reference_solution,
        rime_options=dict(local.rime_options),
    )


def update_problem_block(source: str, remote: ProblemConfig) -> str:
    """Update a PROBLEM block from remote data without losing local-only fields."""

    local = parse_problem_config(source)
    merged = merge_remote_problem(local, remote)
    return upsert_managed_block(source, render_problem_block(merged))


_T = TypeVar("_T")


def read_config(path: Path, parser: Callable[[str], _T]) -> _T:
    """Read a UTF-8 config and feed it to a parser."""

    try:
        source = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    try:
        return parser(source)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def write_config_atomic(path: Path, source: str) -> None:
    """Atomically replace a UTF-8 config in the same directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(source)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise ConfigError(f"cannot write configuration {path}: {exc}") from exc


render_project_config = render_project_block
render_problem_config = render_problem_block
render_testset_config = render_testset_block
render_solution_config = render_solution_block
