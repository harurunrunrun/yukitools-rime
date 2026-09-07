"""Typed request and response objects for the yukicoder API."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any, cast

from yukitools_rime.errors import ValidationError
from yukitools_rime.models import ProblemSettings, Statement, Which

JUDGE_STATUS_OK = "AC"
JUDGE_STATUS_COMPILE_ERROR = "CE"
STATUS_CATEGORY_JUDGING = "judging"
_FAILED_CASES_SHOWN = 10


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


def _required_string(data: dict[str, Any], key: str) -> str:
    if key not in data:
        raise ResponseFormatError(f"{key} がありません")
    return _string(data, key)


def _required_integer(data: dict[str, Any], key: str) -> int:
    if key not in data:
        raise ResponseFormatError(f"{key} がありません")
    return _integer(data, key)


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
        problem_id = _required_integer(data, "problemId")
        content = _string(data, "content")
        is_markdown = _boolean(data, "isMarkdown")
        showable = _boolean(data, "showable")
        settings_error: ResponseFormatError | None = None
        try:
            settings = ProblemSettings.from_api_dict(data)
        except ValidationError:
            settings_error = ResponseFormatError("問題設定の形式が不正です")
        if settings_error is not None:
            raise settings_error
        return cls(
            problem_id=problem_id,
            content=content,
            is_markdown=is_markdown,
            showable=showable,
            settings=settings,
        )


@dataclass(frozen=True, slots=True)
class ProblemEditRequest:
    settings: ProblemSettings
    statement: Statement

    def to_api_dict(self) -> dict[str, object]:
        body = cast(dict[str, object], self.settings.to_api_dict())
        body.update(self.statement.to_api_fields(require_nonempty=True, label="問題文"))
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
    compile_message: str = ""

    @classmethod
    def from_api_dict(cls, raw: object) -> JudgeCodeContent:
        data = _mapping(raw, "ジャッジコード")
        return cls(
            lang_id=_string(data, "langId"),
            source=_string(data, "source"),
            status=_string(data, "status"),
            compile_message=_string(data, "compileMessage"),
        )


@dataclass(frozen=True, slots=True)
class JudgeCodeRequest:
    lang_id: str
    source: str

    def to_api_dict(self) -> dict[str, object]:
        return {"langId": self.lang_id, "source": self.source}


@dataclass(frozen=True, slots=True)
class ValidatorCase:
    name: str
    status: str

    @classmethod
    def from_api_dict(cls, raw: object) -> ValidatorCase:
        data = _mapping(raw, "validatorのケース")
        return cls(
            name=_required_string(data, "name"),
            status=_required_string(data, "status"),
        )


@dataclass(frozen=True, slots=True)
class ValidatorContent:
    lang_id: str = ""
    source: str = ""
    status: str = ""
    compile_message: str = ""
    cases: tuple[ValidatorCase, ...] | None = None

    @classmethod
    def from_api_dict(cls, raw: object) -> ValidatorContent:
        data = _mapping(raw, "validator")
        raw_cases = data.get("cases")
        if raw_cases is not None and not isinstance(raw_cases, list):
            raise ResponseFormatError("cases は配列またはnullではありません")
        cases = (
            None
            if raw_cases is None
            else tuple(ValidatorCase.from_api_dict(item) for item in raw_cases)
        )
        return cls(
            lang_id=_string(data, "langId"),
            source=_string(data, "source"),
            status=_string(data, "status"),
            compile_message=_string(data, "compileMessage"),
            cases=cases,
        )

    def is_up_to_date(self, judging: Collection[str]) -> bool:
        """Return whether validation has reached any terminal result."""

        return bool(self.status) and self.status not in judging

    def failure_details(self) -> str:
        """Render the bounded failure details returned by the validator API."""

        if self.status == JUDGE_STATUS_COMPILE_ERROR:
            message = self.compile_message.strip()
            if not message:
                return ""
            return f"\nコンパイルメッセージ (長いと途中で切れます):\n{message}"
        failed = [
            f"{case.name} ({case.status})"
            for case in self.cases or ()
            if case.status != JUDGE_STATUS_OK
        ]
        if not failed:
            return ""
        more = (
            f" 他 {len(failed) - _FAILED_CASES_SHOWN} 件"
            if len(failed) > _FAILED_CASES_SHOWN
            else ""
        )
        return f"\n通らなかったケース: {', '.join(failed[:_FAILED_CASES_SHOWN])}{more}"


@dataclass(frozen=True, slots=True)
class ValidatorRequest:
    lang_id: str
    source: str

    def to_api_dict(self) -> dict[str, object]:
        return {"langId": self.lang_id, "source": self.source}


@dataclass(frozen=True, slots=True)
class StatusInfo:
    id: str
    category: str

    @classmethod
    def from_api_dict(cls, raw: object) -> StatusInfo:
        data = _mapping(raw, "ジャッジステータス")
        return cls(
            id=_required_string(data, "id"),
            category=_required_string(data, "category"),
        )


def judging_ids(statuses: Iterable[StatusInfo]) -> frozenset[str]:
    """Derive the non-terminal status IDs from the server's categories."""

    return frozenset(status.id for status in statuses if status.category == STATUS_CATEGORY_JUDGING)


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
            id=_required_string(data, "Id"),
            name=_required_string(data, "Name"),
            ver=_required_string(data, "Ver"),
            status=_string(data, "Status"),
        )


@dataclass(frozen=True, slots=True)
class SubmissionInfo:
    status: str = ""
    run_time_ms: int = 0

    @classmethod
    def from_api_dict(cls, raw: object) -> SubmissionInfo:
        data = _mapping(raw, "提出")
        return cls(
            status=_string(data, "status"),
            run_time_ms=_integer(data, "runTimeMs"),
        )


@dataclass(frozen=True, slots=True)
class TestcaseNameRule:
    allowed_chars: str

    @classmethod
    def from_api_dict(cls, raw: object) -> TestcaseNameRule:
        data = _mapping(raw, "テストケース名の規則")
        allowed_chars = _required_string(data, "allowedChars")
        if not allowed_chars:
            raise ResponseFormatError("allowedChars が空です")
        return cls(allowed_chars=allowed_chars)


@dataclass(frozen=True, slots=True)
class TestcaseInfo:
    name: str
    sha256: str

    @classmethod
    def from_api_dict(cls, raw: object) -> TestcaseInfo:
        data = _mapping(raw, "テストケース情報")
        return cls(
            name=_required_string(data, "name"),
            sha256=_required_string(data, "sha256"),
        )


def judge_status_is_final(status: str, judging_statuses: Collection[str]) -> bool:
    """Return whether a non-empty status is outside the server's judging category."""

    return bool(status) and status not in judging_statuses


__all__ = [
    "JUDGE_STATUS_COMPILE_ERROR",
    "JUDGE_STATUS_OK",
    "STATUS_CATEGORY_JUDGING",
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
    "StatusInfo",
    "SubmissionInfo",
    "TestcaseInfo",
    "TestcaseNameRule",
    "UploadResponse",
    "ValidatorCase",
    "ValidatorContent",
    "ValidatorRequest",
    "Which",
    "judge_status_is_final",
    "judging_ids",
]
