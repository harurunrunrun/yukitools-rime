"""Validated domain models shared by the CLI, API and Rime adapter."""

from __future__ import annotations

import math
import re
from contextlib import suppress
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from pathlib import PurePath
from typing import Any, TypeAlias, cast

from yukitools_rime.errors import ValidationError
from yukitools_rime.url_validation import normalize_http_base_url

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

DEFAULT_BASE_URL = "https://yukicoder.me/api"
DEFAULT_RIME_OUT_DIR = "rime-out"

PROBLEM_TYPE_LABELS: dict[int, str] = {
    0: "通常",
    1: "教育的",
    2: "スコア",
    3: "ネタ",
    4: "未証明",
    5: "数学要素が高い",
    6: "ショートコード",
}
JUDGE_TYPE_LABELS: dict[int, str] = {0: "通常", 1: "スペシャル", 2: "リアクティブ"}
EPS_MODE_LABELS: dict[str, str] = {
    "-": "なし",
    "abs": "絶対誤差",
    "rel": "相対誤差",
    "all": "両方",
}

_CAMEL_BOUNDARY = re.compile(r"_([a-zA-Z0-9])")
_SAFE_CASE_NAME = re.compile(r"[A-Za-z0-9._]+\Z")
_SAFE_RIME_OUT_DIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)}
)
_RESERVED_RIME_OUT_DIRS = frozenset(
    {"problem", "tests", "statement.md", "statement.html", "editorial.md", "editorial.html"}
)
_MISSING = object()


def snake_to_camel(name: str) -> str:
    """Convert a Python field name to the API's lower-camel spelling."""

    return _CAMEL_BOUNDARY.sub(lambda match: match.group(1).upper(), name)


def _json_value(value: object, *, context: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"{context} must contain only finite numbers")
        return value
    if isinstance(value, Enum):
        return _json_value(value.value, context=context)
    if is_dataclass(value) and not isinstance(value, type):
        return cast(JsonValue, to_camel_dict(value))
    if isinstance(value, (list, tuple)):
        return [_json_value(item, context=context) for item in value]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValidationError(f"{context} keys must be non-empty strings")
            result[key] = _json_value(item, context=f"{context}.{key}")
        return result
    raise ValidationError(f"{context} contains unsupported value {type(value).__name__}")


def to_camel_dict(
    value: object,
    *,
    exclude: frozenset[str] = frozenset(),
    omit_none: bool = False,
) -> dict[str, JsonValue]:
    """Serialize a dataclass using camelCase keys and JSON-compatible values."""

    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError("to_camel_dict() expects a dataclass instance")
    result: dict[str, JsonValue] = {}
    for item in fields(value):
        if item.name in exclude:
            continue
        field_value = getattr(value, item.name)
        if omit_none and field_value is None:
            continue
        result[snake_to_camel(item.name)] = _json_value(
            field_value, context=snake_to_camel(item.name)
        )
    return result


def normalize_eps(value: str | int | float | Decimal) -> str:
    """Accept the API's string-or-number epsilon and retain a readable spelling."""

    if isinstance(value, bool):
        raise ValidationError("eps must be a string or number, not bool")
    if isinstance(value, str):
        result = value.strip()
        if not result:
            raise ValidationError("eps must not be empty")
    elif isinstance(value, int):
        result = str(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValidationError("eps must be finite")
        result = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("eps must be finite")
        result = repr(value)
    else:
        raise ValidationError("eps must be a string or number")
    try:
        parsed = Decimal(result)
    except InvalidOperation as exc:
        raise ValidationError("eps must be numeric") from exc
    if not parsed.is_finite():
        raise ValidationError("eps must be finite")
    return result


def validate_basename(name: str, *, label: str = "path") -> str:
    """Validate a single relative path component without restricting its alphabet."""

    if not isinstance(name, str):
        raise ValidationError(f"{label} must be a string")
    if not name or name in {".", ".."} or "\x00" in name:
        raise ValidationError(f"{label} must be a non-empty file name")
    if PurePath(name).name != name or "/" in name or "\\" in name:
        raise ValidationError(f"{label} must be a file name, not a path: {name!r}")
    if name.endswith((".", " ")):
        raise ValidationError(f"{label} must not end with a dot or space")
    if any(ord(character) < 32 or character in '<>:"|?*' for character in name):
        raise ValidationError(f"{label} contains a character unsupported on Windows")
    device_stem = name.split(".", 1)[0].upper()
    if device_stem in _WINDOWS_DEVICE_NAMES:
        raise ValidationError(f"{label} is a reserved Windows device name")
    return name


def validate_testcase_name(name: str, *, label: str = "testcase name") -> str:
    """Validate a yukicoder testcase name without its local suffix."""

    validate_basename(name, label=label)
    if name.startswith(".") or _SAFE_CASE_NAME.fullmatch(name) is None:
        raise ValidationError(
            f"{label} may contain only ASCII letters, digits, '.', and '_': {name!r}"
        )
    return name


def _string(value: object, *, label: str, empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    if not empty and not value.strip():
        raise ValidationError(f"{label} must not be empty")
    return value


def _integer(value: object, *, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{label} must be at least {minimum}")
    return value


def _boolean(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{label} must be true or false")
    return value


def _literal_mapping(value: object, *, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a dictionary")
    return cast(dict[str, JsonValue], _json_value(value, context=label))


@dataclass(slots=True)
class ProjectConfig:
    """Project-level settings stored in PROJECT."""

    base_url: str = DEFAULT_BASE_URL
    rime_out_dir: str = DEFAULT_RIME_OUT_DIR

    def __post_init__(self) -> None:
        base_url: str | None = None
        with suppress(ValueError):
            base_url, _ = normalize_http_base_url(self.base_url)
        if base_url is None:
            raise ValidationError("base_url must be a valid http:// or https:// URL")
        self.base_url = base_url
        self.rime_out_dir = validate_basename(self.rime_out_dir, label="rime_out_dir")
        if _SAFE_RIME_OUT_DIR.fullmatch(self.rime_out_dir) is None:
            raise ValidationError(
                "rime_out_dir must start with an ASCII letter or digit and contain "
                "only ASCII letters, digits, '.', '_', and '-'"
            )

        if self.rime_out_dir.casefold() in _RESERVED_RIME_OUT_DIRS:
            raise ValidationError(
                f"rime_out_dir conflicts with a reserved problem path: {self.rime_out_dir}"
            )


@dataclass(slots=True)
class ProblemSettings:
    """Editable fields accepted by PUT /problems/{id}/edit."""

    title: str
    tags: str
    level: float
    time_limit_ms: int
    memory_limit: int
    eps_mode: str
    eps: str | int | float | Decimal
    wip: bool
    recruiting_tester: bool
    problem_type: int
    judge_type: int
    show_ans: bool = False
    enable_pure_judge: bool = False
    force_single_server_judge: bool = False
    allowed_langs: tuple[str, ...] | list[str] = ()

    def __post_init__(self) -> None:
        self.title = _string(self.title, label="title", empty=False)
        self.tags = _string(self.tags, label="tags")
        if isinstance(self.level, bool) or not isinstance(self.level, (int, float)):
            raise ValidationError("level must be a number")
        self.level = float(self.level)
        if not math.isfinite(self.level) or self.level < 0:
            raise ValidationError("level must be a finite non-negative number")
        self.time_limit_ms = _integer(self.time_limit_ms, label="time_limit_ms", minimum=1)
        self.memory_limit = _integer(self.memory_limit, label="memory_limit", minimum=1)
        self.eps_mode = _string(self.eps_mode, label="eps_mode", empty=False)
        if self.eps_mode not in EPS_MODE_LABELS:
            allowed = ", ".join(repr(item) for item in EPS_MODE_LABELS)
            raise ValidationError(f"eps_mode must be one of {allowed}")
        self.eps = normalize_eps(self.eps)
        self.wip = _boolean(self.wip, label="wip")
        self.recruiting_tester = _boolean(self.recruiting_tester, label="recruiting_tester")
        self.problem_type = _integer(self.problem_type, label="problem_type", minimum=0)
        self.judge_type = _integer(self.judge_type, label="judge_type", minimum=0)
        self.show_ans = _boolean(self.show_ans, label="show_ans")
        self.enable_pure_judge = _boolean(self.enable_pure_judge, label="enable_pure_judge")
        self.force_single_server_judge = _boolean(
            self.force_single_server_judge, label="force_single_server_judge"
        )
        if not isinstance(self.allowed_langs, (list, tuple)):
            raise ValidationError("allowed_langs must be a list of strings")
        normalized_langs: list[str] = []
        for index, lang in enumerate(self.allowed_langs):
            normalized_langs.append(_string(lang, label=f"allowed_langs[{index}]", empty=False))
        self.allowed_langs = tuple(normalized_langs)

    def to_api_dict(self) -> dict[str, JsonValue]:
        """Return only editable settings, using yukicoder camelCase keys."""

        return to_camel_dict(self)

    @classmethod
    def from_api_dict(cls, value: object) -> ProblemSettings:
        """Parse settings from an API mapping, with strict scalar types."""

        if not isinstance(value, dict):
            raise ValidationError("problem settings must be an object")

        def get(name: str, default: object = _MISSING) -> Any:
            key = snake_to_camel(name)
            if key in value:
                return value[key]
            if default is not _MISSING:
                return default
            raise ValidationError(f"problem settings is missing {key!r}")

        return cls(
            title=get("title"),
            tags=get("tags", ""),
            level=get("level"),
            time_limit_ms=get("time_limit_ms"),
            memory_limit=get("memory_limit"),
            eps_mode=get("eps_mode"),
            eps=get("eps"),
            wip=get("wip"),
            recruiting_tester=get("recruiting_tester"),
            problem_type=get("problem_type"),
            judge_type=get("judge_type"),
            show_ans=get("show_ans", False),
            enable_pure_judge=get("enable_pure_judge", False),
            force_single_server_judge=get("force_single_server_judge", False),
            allowed_langs=get("allowed_langs", []),
        )


@dataclass(slots=True)
class ProblemConfig:
    """A problem's remote identity, editable settings, and local-only Rime data."""

    problem_id: int
    settings: ProblemSettings
    rime_id: str
    reference_solution: str | None = None
    rime_options: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.problem_id = _integer(self.problem_id, label="problem_id", minimum=1)
        if not isinstance(self.settings, ProblemSettings):
            raise ValidationError("settings must be ProblemSettings")
        self.rime_id = _string(self.rime_id, label="rime_id", empty=False)
        if self.reference_solution is not None:
            self.reference_solution = validate_basename(
                self.reference_solution, label="reference_solution"
            )
        self.rime_options = cast(
            dict[str, object], _literal_mapping(self.rime_options, label="rime_options")
        )

    def to_api_dict(self) -> dict[str, JsonValue]:
        """Serialize only remote-editable fields; omit identity and Rime metadata."""

        return self.settings.to_api_dict()


def _validate_rime_kind(value: str | None) -> str | None:
    if value is None:
        return None
    return _string(value, label="rime_kind", empty=False)


@dataclass(slots=True)
class GeneratorConfig:
    """Generator declaration stored in a Rime TESTSET."""

    lang_id: str
    src: str
    test_case_num: int
    prefix: str | None = None
    rime_kind: str | None = None
    rime_options: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.lang_id = _string(self.lang_id, label="lang_id", empty=False)
        self.src = validate_basename(self.src, label="generator src")
        if self.src.casefold() == "testset":
            raise ValidationError("generator src must not name TESTSET")
        self.test_case_num = _integer(self.test_case_num, label="test_case_num", minimum=0)
        if self.prefix is not None:
            self.prefix = validate_testcase_name(self.prefix, label="generator prefix")
        self.rime_kind = _validate_rime_kind(self.rime_kind)
        self.rime_options = cast(
            dict[str, object], _literal_mapping(self.rime_options, label="rime_options")
        )

    def to_api_dict(self, *, source: str, generate: bool | None = None) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "langId": self.lang_id,
            "source": _string(source, label="generator source"),
            "testCaseNum": self.test_case_num,
        }
        if generate is not None:
            result["generate"] = _boolean(generate, label="generate")
        if self.prefix is not None:
            result["prefix"] = self.prefix
        return result


@dataclass(slots=True)
class JudgeConfig:
    """Custom judge declaration stored in a Rime TESTSET."""

    lang_id: str
    src: str
    rime_kind: str | None = None
    rime_options: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.lang_id = _string(self.lang_id, label="lang_id", empty=False)
        self.src = validate_basename(self.src, label="judge src")
        if self.src.casefold() == "testset":
            raise ValidationError("judge src must not name TESTSET")
        self.rime_kind = _validate_rime_kind(self.rime_kind)
        self.rime_options = cast(
            dict[str, object], _literal_mapping(self.rime_options, label="rime_options")
        )

    def to_api_dict(self, *, source: str) -> dict[str, JsonValue]:
        return {"langId": self.lang_id, "source": _string(source, label="judge source")}


@dataclass(slots=True)
class SolutionConfig:
    """Submission declaration stored in a Rime SOLUTION."""

    lang_id: str
    src: str
    rime_kind: str | None = None
    challenge_cases: tuple[str, ...] | list[str] = ()
    rime_options: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.lang_id = _string(self.lang_id, label="lang_id", empty=False)
        self.src = validate_basename(self.src, label="solution src")
        if self.src.casefold() == "solution":
            raise ValidationError("solution src must not name SOLUTION")
        self.rime_kind = _validate_rime_kind(self.rime_kind)
        if not isinstance(self.challenge_cases, (list, tuple)):
            raise ValidationError("challenge_cases must be a list of testcase names")
        self.challenge_cases = tuple(
            validate_testcase_name(name, label=f"challenge_cases[{index}]")
            for index, name in enumerate(self.challenge_cases)
        )
        self.rime_options = cast(
            dict[str, object], _literal_mapping(self.rime_options, label="rime_options")
        )


@dataclass(slots=True)
class Statement:
    """A Markdown or HTML statement/editorial, never both at once."""

    text: str
    is_markdown: bool

    def __post_init__(self) -> None:
        self.text = _string(self.text, label="statement text")
        self.is_markdown = _boolean(self.is_markdown, label="is_markdown")

    @classmethod
    def markdown(cls, text: str) -> Statement:
        return cls(text=text, is_markdown=True)

    @classmethod
    def html(cls, text: str) -> Statement:
        return cls(text=text, is_markdown=False)

    @classmethod
    def from_remote(cls, text: str, is_markdown: bool) -> Statement:
        return cls(text=text, is_markdown=is_markdown)

    @property
    def suffix(self) -> str:
        return ".md" if self.is_markdown else ".html"

    @property
    def format(self) -> str:
        return "markdown" if self.is_markdown else "html"

    def validate_nonempty(self, *, label: str = "本文") -> None:
        if not self.text.strip():
            raise ValidationError(f"{label} must not be empty")

    def to_api_fields(
        self, *, require_nonempty: bool = False, label: str = "本文"
    ) -> dict[str, str]:
        if require_nonempty:
            self.validate_nonempty(label=label)
        if self.is_markdown:
            return {"html": "", "markdown": self.text}
        return {"html": self.text}


class Which(StrEnum):
    """One side of a testcase pair."""

    IN = "in"
    OUT = "out"


def judge_status_is_final(status: str) -> bool:
    """Return whether a custom-judge compilation reached AC or CE."""

    return status in {"AC", "CE"}


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_RIME_OUT_DIR",
    "EPS_MODE_LABELS",
    "JUDGE_TYPE_LABELS",
    "PROBLEM_TYPE_LABELS",
    "GeneratorConfig",
    "JsonScalar",
    "JsonValue",
    "JudgeConfig",
    "ProblemConfig",
    "ProblemSettings",
    "ProjectConfig",
    "SolutionConfig",
    "Statement",
    "Which",
    "judge_status_is_final",
    "normalize_eps",
    "snake_to_camel",
    "to_camel_dict",
    "validate_basename",
    "validate_testcase_name",
]
