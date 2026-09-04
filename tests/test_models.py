from __future__ import annotations

from decimal import Decimal

import pytest

from yukitools_rime.errors import ValidationError
from yukitools_rime.models import (
    GeneratorConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    SolutionConfig,
    Statement,
    Which,
    judge_status_is_final,
    normalize_eps,
    validate_basename,
    validate_testcase_name,
)


def settings(**changes: object) -> ProblemSettings:
    values: dict[str, object] = {
        "title": "Title",
        "tags": "graph",
        "level": 2.5,
        "time_limit_ms": 2000,
        "memory_limit": 512,
        "eps_mode": "-",
        "eps": "0",
        "wip": True,
        "recruiting_tester": False,
        "problem_type": 0,
        "judge_type": 0,
        "show_ans": True,
        "allowed_langs": ["cpp23"],
    }
    values.update(changes)
    return ProblemSettings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" 0.001 ", "0.001"), (0, "0"), (0.001, "0.001"), (Decimal("1E-9"), "1E-9")],
)
def test_eps_accepts_string_or_number(raw: object, expected: str) -> None:
    assert normalize_eps(raw) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["", "nan", float("inf"), True, object()])
def test_eps_rejects_invalid_values(raw: object) -> None:
    with pytest.raises(ValidationError):
        normalize_eps(raw)  # type: ignore[arg-type]


def test_settings_api_round_trip_and_defaults() -> None:
    payload = settings(eps=0.0001).to_api_dict()
    assert payload["timeLimitMs"] == 2000
    assert payload["allowedLangs"] == ["cpp23"]
    assert "problemId" not in payload
    del payload["showAns"]
    parsed = ProblemSettings.from_api_dict(payload)
    assert parsed.eps == "0.0001"
    assert parsed.show_ans is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("level", float("nan")),
        ("time_limit_ms", 0),
        ("memory_limit", True),
        ("eps_mode", "bad"),
        ("wip", 1),
        ("allowed_langs", [""]),
    ],
)
def test_settings_reject_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        settings(**{field: value})


def test_problem_config_omits_local_fields_from_api() -> None:
    config = ProblemConfig(42, settings(), "A", "correct", {"timeout": 3})
    assert config.to_api_dict() == config.settings.to_api_dict()
    assert "rimeId" not in config.to_api_dict()


def test_statement_fields_are_exclusive_and_protected() -> None:
    assert Statement.markdown("# x").to_api_fields(require_nonempty=True) == {
        "html": "",
        "markdown": "# x",
    }
    assert Statement.html("<p>x</p>").to_api_fields(require_nonempty=True) == {"html": "<p>x</p>"}
    with pytest.raises(ValidationError):
        Statement.markdown(" \n").to_api_fields(require_nonempty=True)


def test_tool_configs_canonicalize_sequences() -> None:
    generator = GeneratorConfig("cpp23", "gen.cpp", 10, "random.1", "cxx")
    assert generator.to_api_dict(source="x", generate=True)["generate"] is True
    solution = SolutionConfig("cpp23", "main.cpp", challenge_cases=["sample.1"])
    assert solution.challenge_cases == ("sample.1",)


@pytest.mark.parametrize("name", ["", ".", "..", "../x", "a/b", "a\\b", "CON", "nul.txt"])
def test_basename_rejects_paths(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_basename(name)


@pytest.mark.parametrize("name", [".hidden", "space name", "日本語", "a-b"])
def test_testcase_name_rejects_unsafe_alphabet(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_testcase_name(name)


@pytest.mark.parametrize(
    "rime_out_dir",
    [
        "#comment",
        "!negated",
        "glob*",
        "glob?",
        "group[0]",
        "line\nbreak",
        "tab\tname",
        "tests.",
        "CON",
        "com1.cache",
        "LPT9",
        "space name",
    ],
)
def test_project_rejects_rime_output_names_unsafe_for_gitignore(
    rime_out_dir: str,
) -> None:
    with pytest.raises(ValidationError, match="rime_out_dir"):
        ProjectConfig(rime_out_dir=rime_out_dir)


@pytest.mark.parametrize("rime_out_dir", ["rime-out", "generated_2", "out.v1"])
def test_project_accepts_portable_rime_output_names(rime_out_dir: str) -> None:
    assert ProjectConfig(rime_out_dir=rime_out_dir).rime_out_dir == rime_out_dir


def test_project_and_enums() -> None:
    assert ProjectConfig(base_url="https://example.test/api/").base_url.endswith("/api")
    assert str(Which.OUT) == "out"
    assert judge_status_is_final("AC") and not judge_status_is_final("Judge")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.test/api?token=secret",
        "https://example.test/api#section",
        "https://user@example.test/api",
        "https://user:password@example.test/api",
        "https://example.test:not-a-port/api",
        "https://example.test:99999/api",
        "https://[::1/api",
        "https://example.test/api?",
        "https://example.test/api#",
        " https://example.test/api",
        "https://example.test/api ",
        "https://example.test/api path",
        "https://example.test/api\tpath",
        "https://example.test/api\x7fpath",
        "https://example.test\\other/api",
        "https://example.test:/api",
        "https://[::1]:/api",
    ],
)
def test_project_rejects_unsafe_or_malformed_base_urls(base_url: str) -> None:
    with pytest.raises(ValidationError, match="base_url") as caught:
        ProjectConfig(base_url=base_url)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
