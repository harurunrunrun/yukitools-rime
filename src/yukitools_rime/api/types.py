"""Typed request and response objects for the yukicoder API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from yukitools_rime.models import ProblemSettings, Statement, Which

JUDGE_STATUS_OK = "AC"
JUDGE_STATUS_COMPILE_ERROR = "CE"


class ResponseFormatError(ValueError):
    """Raised when a successful HTTP response has an unexpected JSON shape."""


def _mapping(data: object, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ResponseFormatError(f"{what} はJSONオブジェクトではありません")
    return data


def _string(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ResponseFormatError(f"{key} は文字列ではありません")
    return value


def _integer(data: dict[str, Any], key: str, default: int = 0) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResponseFormatError(f"{key} は整数ではありません")
    return value


def _boolean(data: dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ResponseFormatError(f"{key} は真偽値ではありません")
    return value


@dataclass(frozen=True, slots=True)
class ProblemEditContent:
    problem_id: int
    content: str
    is_markdown: bool
    showable: bool
    settings: ProblemSettings

    @classmethod
    def from_api_dict(cls, raw: object) -> ProblemEditContent:
        data = _mapping(raw, "問題")
        return cls(
            problem_id=_integer(data, "problemId"),
            content=_string(data, "content"),
            is_markdown=_boolean(data, "isMarkdown"),
            showable=_boolean(data, "showable"),
            settings=ProblemSettings.from_api_dict(data),
        )


@dataclass(frozen=True, slots=True)
class ProblemEditRequest:
    settings: ProblemSettings
    statement: Statement

    def to_api_dict(self) -> dict[str, object]:
        body = cast(dict[str, object], self.settings.to_api_dict())
        body.update(
            self.statement.to_api_fields(require_nonempty=True, label="問題文")
        )
        return body


@dataclass(frozen=True, slots=True)
class GeneratorContent:
    lang_id: str = ""
    source: str = ""
    enable: bool = False
    test_case_num: int = 0

    @classmethod
    def from_api_dict(cls, raw: object) -> GeneratorContent:
        data = _mapping(raw, "ジェネレータ")
        return cls(
            lang_id=_string(data, "langId"),
            source=_string(data, "source"),
            enable=_boolean(data, "enable"),
            test_case_num=_integer(data, "testCaseNum"),
        )


@dataclass(frozen=True, slots=True)
class GeneratorRequest:
    lang_id: str
    source: str
    test_case_num: int
    generate: bool | None = None
    prefix: str | None = None

    def to_api_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "langId": self.lang_id,
            "source": self.source,
            "testCaseNum": self.test_case_num,
        }
        if self.generate is not None:
            body["generate"] = self.generate
        if self.prefix is not None:
            body["prefix"] = self.prefix
        return body


@dataclass(frozen=True, slots=True)
class JudgeCodeContent:
    lang_id: str = ""
    source: str = ""
    status: str = ""

    @classmethod
    def from_api_dict(cls, raw: object) -> JudgeCodeContent:
        data = _mapping(raw, "ジャッジコード")
        return cls(
            lang_id=_string(data, "langId"),
            source=_string(data, "source"),
            status=_string(data, "status"),
        )


@dataclass(frozen=True, slots=True)
class JudgeCodeRequest:
    lang_id: str
    source: str

    def to_api_dict(self) -> dict[str, object]:
        return {"langId": self.lang_id, "source": self.source}


@dataclass(frozen=True, slots=True)
class EditorialContent:
    content: str = ""
    is_markdown: bool = False

    @classmethod
    def from_api_dict(cls, raw: object) -> EditorialContent:
        data = _mapping(raw, "解説")
        return cls(
            content=_string(data, "content"),
            is_markdown=_boolean(data, "isMarkdown"),
        )


@dataclass(frozen=True, slots=True)
class EditorialRequest:
    statement: Statement

    def to_api_dict(self) -> dict[str, object]:
        return cast(
            dict[str, object],
            self.statement.to_api_fields(require_nonempty=True, label="解説"),
        )


@dataclass(frozen=True, slots=True)
class SaveResponse:
    message: str = ""

    @classmethod
    def from_api_dict(cls, raw: object) -> SaveResponse:
        return cls(message=_string(_mapping(raw, "保存レスポンス"), "Message"))


@dataclass(frozen=True, slots=True)
class JudgeCodeSaveResponse:
    message: str = ""
    status: str = ""

    @classmethod
    def from_api_dict(cls, raw: object) -> JudgeCodeSaveResponse:
        data = _mapping(raw, "ジャッジコード保存レスポンス")
        return cls(message=_string(data, "Message"), status=_string(data, "status"))


@dataclass(frozen=True, slots=True)
class UploadResponse:
    file_names: tuple[str, ...] = ()
    warning: str = ""

    @classmethod
    def from_api_dict(cls, raw: object) -> UploadResponse:
        data = _mapping(raw, "アップロードレスポンス")
        names = data.get("FileNames", [])
        if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
            raise ResponseFormatError("FileNames は文字列の配列ではありません")
        return cls(file_names=tuple(names), warning=_string(data, "Warning"))


@dataclass(frozen=True, slots=True)
class SolutionRequest:
    summary: str | None = None
    delete: bool | None = None

    def to_api_dict(self) -> dict[str, object]:
        body: dict[str, object] = {}
        if self.summary is not None:
            body["summary"] = self.summary
        if self.delete is not None:
            body["delete"] = self.delete
        return body


@dataclass(frozen=True, slots=True)
class Language:
    id: str
    name: str
    ver: str
    status: str = ""

    @classmethod
    def from_api_dict(cls, raw: object) -> Language:
        data = _mapping(raw, "言語")
        return cls(
            id=_string(data, "Id"),
            name=_string(data, "Name"),
            ver=_string(data, "Ver"),
            status=_string(data, "Status"),
        )


def judge_status_is_final(status: str) -> bool:
    return status in {JUDGE_STATUS_OK, JUDGE_STATUS_COMPILE_ERROR}


__all__ = [
    "JUDGE_STATUS_COMPILE_ERROR",
    "JUDGE_STATUS_OK",
    "EditorialContent",
    "EditorialRequest",
    "GeneratorContent",
    "GeneratorRequest",
    "JudgeCodeContent",
    "JudgeCodeRequest",
    "JudgeCodeSaveResponse",
    "Language",
    "ProblemEditContent",
    "ProblemEditRequest",
    "ResponseFormatError",
    "SaveResponse",
    "SolutionRequest",
    "UploadResponse",
    "Which",
    "judge_status_is_final",
]
