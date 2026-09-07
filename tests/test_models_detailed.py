from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest

from yukitools_rime.errors import ValidationError
from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    SolutionConfig,
    Statement,
    Which,
    normalize_eps,
    snake_to_camel,
    to_camel_dict,
    validate_basename,
    validate_testcase_name,
)


class SampleEnum(Enum):
    VALUE = "value"


@dataclass
class Payload:
    snake_name: object
    optional_value: object = None


def make_settings(**changes: object) -> ProblemSettings:
    values: dict[str, Any] = {
        "title": "Title",
        "tags": "",
        "level": 0,
        "time_limit_ms": 1,
        "memory_limit": 1,
        "eps_mode": "-",
        "eps": "0",
        "wip": False,
        "recruiting_tester": False,
        "problem_type": 0,
        "judge_type": 0,
    }
    values.update(changes)
    return ProblemSettings(**values)


def test_camel_serialization_handles_all_json_shapes() -> None:
    payload = Payload(
        snake_name={
            "items": [None, True, 1, 1.5, SampleEnum.VALUE, ("nested",)],
        }
    )
    assert snake_to_camel("one_two_3") == "oneTwo3"
    assert to_camel_dict(payload) == {
        "snakeName": {
            "items": [None, True, 1, 1.5, "value", ["nested"]],
        },
        "optionalValue": None,
    }
    assert to_camel_dict(payload, omit_none=True) == {
        "snakeName": {
            "items": [None, True, 1, 1.5, "value", ["nested"]],
        }
    }
    assert to_camel_dict(payload, exclude=frozenset({"snake_name"})) == {"optionalValue": None}


@pytest.mark.parametrize("value", [Payload, object(), 3])
def test_camel_serialization_requires_dataclass_instance(value: object) -> None:
    with pytest.raises(TypeError):
        to_camel_dict(value)


@pytest.mark.parametrize(
    "value",
    [
        object(),
        float("inf"),
        {"": "bad"},
        {1: "bad"},
        {"nested": float("nan")},
    ],
)
def test_camel_serialization_rejects_non_json_values(value: object) -> None:
    with pytest.raises(ValidationError):
        to_camel_dict(Payload(value))


@pytest.mark.parametrize(
    "value",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        "not-a-number",
        "NaN",
        "-Infinity",
        None,
    ],
)
def test_eps_rejects_every_nonfinite_or_unsupported_form(value: object) -> None:
    with pytest.raises(ValidationError):
        normalize_eps(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    [
        1,
        "",
        ".",
        "..",
        "a\x00b",
        "a/b",
        "a\\b",
        "name.",
        "name ",
        "a<b",
        "a>b",
        'a"b',
        "a:b",
        "a|b",
        "a?b",
        "a*b",
        "line\nbreak",
        "con",
        "PrN.log",
        "AUX",
        "NUL.txt",
        "COM1",
        "com9.bin",
        "LPT1",
        "lpt9.out",
    ],
)
def test_basename_rejects_all_portability_hazards(name: object) -> None:
    with pytest.raises(ValidationError):
        validate_basename(name)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["日本 語.txt", "file-name", "name..part", "emoji-😀"])
def test_basename_allows_safe_single_components(name: str) -> None:
    assert validate_basename(name, label="source") == name


@pytest.mark.parametrize(
    "name",
    ["sample", "sample01", "random.1", "A_B", "name.with.many.parts"],
)
def test_testcase_name_accepts_portable_names(name: str) -> None:
    assert validate_testcase_name(name) == name


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "ftp://example.test/api",
        "https:///api",
        "relative/path",
        123,
    ],
)
def test_project_rejects_missing_origin_or_wrong_scheme(base_url: object) -> None:
    with pytest.raises(ValidationError, match="base_url"):
        ProjectConfig(base_url=base_url)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "rime_out_dir",
    [
        "problem",
        "PROBLEM",
        "tests",
        "statement.md",
        "statement.html",
        "editorial.md",
        "editorial.html",
    ],
)
def test_project_rejects_reserved_problem_paths(rime_out_dir: str) -> None:
    with pytest.raises(ValidationError, match="conflicts"):
        ProjectConfig(rime_out_dir=rime_out_dir)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "   "),
        ("tags", None),
        ("level", True),
        ("level", "1"),
        ("level", -0.1),
        ("level", float("inf")),
        ("time_limit_ms", True),
        ("time_limit_ms", 0),
        ("memory_limit", True),
        ("memory_limit", 0),
        ("eps_mode", 1),
        ("eps_mode", ""),
        ("wip", 0),
        ("recruiting_tester", 0),
        ("problem_type", True),
        ("problem_type", -1),
        ("judge_type", True),
        ("judge_type", -1),
        ("show_ans", 0),
        ("enable_pure_judge", 0),
        ("force_single_server_judge", 0),
        ("allowed_langs", "cpp"),
        ("allowed_langs", [1]),
        ("allowed_langs", [" "]),
    ],
)
def test_settings_validate_every_field(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_settings_accept_tuple_and_serialize_all_flags() -> None:
    result = make_settings(
        allowed_langs=("cpp23", "python3"),
        show_ans=True,
        enable_pure_judge=True,
        force_single_server_judge=True,
    )
    assert result.allowed_langs == ("cpp23", "python3")
    assert result.to_api_dict()["forceSingleServerJudge"] is True


@pytest.mark.parametrize(
    "missing",
    [
        "title",
        "level",
        "timeLimitMs",
        "memoryLimit",
        "epsMode",
        "eps",
        "wip",
        "recruitingTester",
        "problemType",
        "judgeType",
    ],
)
def test_settings_remote_requires_all_reference_required_fields(missing: str) -> None:
    payload = make_settings().to_api_dict()
    del payload[missing]
    with pytest.raises(ValidationError, match="missing"):
        ProblemSettings.from_api_dict(payload)


def test_settings_remote_rejects_non_mapping() -> None:
    with pytest.raises(ValidationError):
        ProblemSettings.from_api_dict([])


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ProblemConfig(0, make_settings(), "A"),
        lambda: ProblemConfig(1, object(), "A"),  # type: ignore[arg-type]
        lambda: ProblemConfig(1, make_settings(), " "),
        lambda: ProblemConfig(1, make_settings(), "A", "../solution"),
        lambda: ProblemConfig(1, make_settings(), "A", rime_options=[]),  # type: ignore[arg-type]
        lambda: ProblemConfig(1, make_settings(), "A", rime_options={"x": object()}),
        lambda: GeneratorConfig("", "gen.cpp", 1),
        lambda: GeneratorConfig("cpp", "TESTSET", 1),
        lambda: GeneratorConfig("cpp", "gen.cpp", -1),
        lambda: GeneratorConfig("cpp", "gen.cpp", True),
        lambda: GeneratorConfig("cpp", "gen.cpp", 1, prefix=".hidden"),
        lambda: GeneratorConfig("cpp", "gen.cpp", 1, rime_kind=""),
        lambda: JudgeConfig("", "judge.cpp"),
        lambda: JudgeConfig("cpp", "TESTSET"),
        lambda: JudgeConfig("cpp", "judge.cpp", rime_kind=""),
        lambda: SolutionConfig("", "main.cpp"),
        lambda: SolutionConfig("cpp", "SOLUTION"),
        lambda: SolutionConfig("cpp", "main.cpp", challenge_cases="sample"),  # type: ignore[arg-type]
        lambda: SolutionConfig("cpp", "main.cpp", challenge_cases=["bad name"]),
    ],
)
def test_problem_tool_models_reject_invalid_configuration(factory: object) -> None:
    with pytest.raises(ValidationError):
        factory()  # type: ignore[operator]


def test_generator_and_judge_api_payload_validation() -> None:
    generator = GeneratorConfig("cpp", "gen.cpp", 2, prefix="random", rime_kind=None)
    assert generator.to_api_dict(source="", generate=False) == {
        "langId": "cpp",
        "source": "",
        "testCaseNum": 2,
        "generate": False,
        "prefix": "random",
    }
    with pytest.raises(ValidationError):
        generator.to_api_dict(source=1)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        generator.to_api_dict(source="", generate=1)  # type: ignore[arg-type]

    judge = JudgeConfig("cpp", "judge.cpp")
    assert judge.to_api_dict(source="code") == {"langId": "cpp", "source": "code"}
    with pytest.raises(ValidationError):
        judge.to_api_dict(source=None)  # type: ignore[arg-type]


def test_statement_html_properties_and_validation() -> None:
    statement = Statement.from_remote("<p>x</p>", False)
    assert statement.suffix == ".html"
    assert statement.format == "html"
    assert statement.to_api_fields() == {"html": "<p>x</p>"}
    statement.validate_nonempty()

    markdown = Statement.markdown("# x")
    assert markdown.suffix == ".md"
    assert markdown.format == "markdown"
    assert markdown.to_api_fields(require_nonempty=True) == {
        "html": "",
        "markdown": "# x",
    }


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Statement(1, True),  # type: ignore[arg-type]
        lambda: Statement("x", 1),  # type: ignore[arg-type]
    ],
)
def test_statement_rejects_invalid_scalar_types(factory: object) -> None:
    with pytest.raises(ValidationError):
        factory()  # type: ignore[operator]


def test_statement_nonempty_error_uses_requested_label() -> None:
    with pytest.raises(ValidationError, match="解説"):
        Statement.html(" \n").validate_nonempty(label="解説")


def test_which_string_values_are_stable() -> None:
    assert str(Which.IN) == "in"
    assert str(Which.OUT) == "out"
