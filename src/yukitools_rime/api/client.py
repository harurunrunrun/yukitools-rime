"""Synchronous, typed client for the yukicoder problem-management API."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from typing import TypeVar

import httpx

from yukitools_rime import __version__
from yukitools_rime.api.body import Part, multipart, prepare_body, validate_testcase_name
from yukitools_rime.api.types import (
    EditorialContent,
    EditorialRequest,
    GeneratorContent,
    GeneratorRequest,
    JudgeCodeContent,
    JudgeCodeRequest,
    JudgeCodeSaveResponse,
    Language,
    ProblemEditContent,
    ProblemEditRequest,
    ResponseFormatError,
    SaveResponse,
    SolutionRequest,
    StatusInfo,
    SubmissionInfo,
    SubtaskSaveResponse,
    SubtaskSet,
    TestcaseInfo,
    TestcaseNameRule,
    UploadResponse,
    ValidatorContent,
    ValidatorRequest,
    Which,
    judging_ids,
)
from yukitools_rime.auth import validate_token
from yukitools_rime.models import ProblemSettings, Statement
from yukitools_rime.url_validation import normalize_http_base_url

DEFAULT_BASE_URL = "https://yukicoder.me/api"
USER_AGENT = f"yukitools-rime/{__version__}"
_HINTS = {
    401: "トークンが無い・不正・失効、または別の問題のトークンです。",
    403: "編集権限が無いか、入力の検証エラーです。",
    404: "問題IDまたはファイル名を確認してください。",
}
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_ResponseT = TypeVar("_ResponseT")


class YukicoderAPIError(RuntimeError):
    """Base class for failures at the HTTP boundary."""


class YukicoderTransportError(YukicoderAPIError):
    """A request could not be sent."""


class YukicoderHTTPError(YukicoderAPIError):
    """A non-success HTTP status returned by yukicoder."""

    def __init__(
        self, message: str, *, status_code: int, operation: str, response_text: str
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.operation = operation
        self.response_text = response_text


APIError = YukicoderAPIError
HTTPError = YukicoderHTTPError


def _path_id(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label}は0以上の整数で指定してください")
    return value


def _normalize_base_url(base_url: str) -> tuple[str, str]:
    result: tuple[str, str] | None = None
    with suppress(ValueError):
        result = normalize_http_base_url(base_url)
    if result is None:
        raise ValueError("APIベースURLが不正です")
    return result


def _poll_seconds(value: float, *, label: str, allow_zero: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}は有限の数値で指定してください")
    normalized: float | None = None
    with suppress(OverflowError, TypeError, ValueError):
        normalized = float(value)
    if normalized is None:
        raise ValueError(f"{label}は有限の数値で指定してください")
    minimum_ok = normalized >= 0 if allow_zero else normalized > 0
    if not math.isfinite(normalized) or not minimum_ok:
        comparator = "0以上" if allow_zero else "0より大きい値"
        raise ValueError(f"{label}は有限な{comparator}で指定してください")
    return normalized


def _which_value(which: Which | str) -> str:
    value = which.value if isinstance(which, Which) else which
    if value not in {"in", "out"}:
        raise ValueError("which は 'in' または 'out' で指定してください")
    return value


def _api_payload(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("APIリクエストのキーは文字列でなければなりません")
        return dict(value)
    converter = getattr(value, "to_api_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, dict) and all(isinstance(key, str) for key in converted):
            return converted
    raise TypeError("APIリクエストはMappingまたはto_api_dict()対応型で指定してください")


def _parse_write_response(
    raw: object,
    operation: str,
    parser: Callable[[object], _ResponseT],
) -> _ResponseT:
    error: ResponseFormatError | None = None
    try:
        result = parser(raw)
    except ResponseFormatError:
        error = ResponseFormatError(
            f"{operation} のレスポンス形式が不正です。 サーバー側の適用結果は不明です。"
        )
    if error is not None:
        raise error
    return result


class YukicoderClient:
    """Typed client with no automatic retries or HTTP-method fallback."""

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float | httpx.Timeout = 120.0,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._initialize(validate_token(token), base_url, timeout, transport, http_client)

    @classmethod
    def anonymous(
        cls,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float | httpx.Timeout = 60.0,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
    ) -> YukicoderClient:
        instance = cls.__new__(cls)
        instance._initialize(None, base_url, timeout, transport, http_client)
        return instance

    def _initialize(
        self,
        token: str | None,
        base_url: str,
        timeout: float | httpx.Timeout,
        transport: httpx.BaseTransport | None,
        http_client: httpx.Client | None,
    ) -> None:
        normalized, scheme = _normalize_base_url(base_url)
        if token is not None and scheme != "https":
            raise ValueError("認証付きAPI通信にはhttpsのベースURLが必要です")
        if http_client is not None and transport is not None:
            raise ValueError("http_client と transport は同時に指定できません")
        self.base_url = normalized
        self._token = token
        self._owns_client = http_client is None
        if http_client is None:
            timeout_config = httpx.Timeout(timeout)
            self._http = httpx.Client(
                timeout=timeout_config,
                transport=transport,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=False,
            )
        else:
            timeout_config = http_client.timeout
            self._http = http_client
        self._timeout = timeout_config

    def __enter__(self) -> YukicoderClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self.base_url!r}, "
            f"authenticated={self._token is not None})"
        )

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def _redact(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, "<redacted>")
        return _BEARER_RE.sub("Bearer <redacted>", text)

    def _error(self, response: httpx.Response, operation: str) -> None:
        response_text = self._redact(response.text.strip())[:4096]
        hint = _HINTS.get(response.status_code, "")
        if response.status_code >= 500:
            hint = (
                "サーバ側のエラーです。保存や削除は完了していない可能性があります。"
                "時間をおいて同じコマンドを実行してください。"
            )
        message = f"{operation} に失敗しました (HTTP {response.status_code})"
        if hint:
            message += f"\nヒント: {hint}"
        if response_text:
            message += f"\nレスポンス: {response_text}"
        raise YukicoderHTTPError(
            message,
            status_code=response.status_code,
            operation=operation,
            response_text=response_text,
        )

    def _request(
        self,
        method: str,
        path: str,
        operation: str,
        *,
        content: bytes | None = None,
        content_type: str | None = None,
        authenticated: bool = True,
        allow_not_found: bool = False,
        content_encoding: str | None = None,
    ) -> httpx.Response | None:
        headers = {"User-Agent": USER_AGENT}
        if authenticated and self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        if content_type:
            headers["Content-Type"] = content_type
        if content_encoding:
            headers["Content-Encoding"] = content_encoding
        request = httpx.Request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            content=content,
            extensions={"timeout": self._timeout.as_dict()},
        )
        transport_error: YukicoderTransportError | None = None
        response: httpx.Response | None = None
        try:
            response = self._http.send(
                request,
                auth=None,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            outcome = " サーバー側の適用結果は不明です。" if method.upper() != "GET" else ""
            detail = self._redact(str(exc))
            transport_error = YukicoderTransportError(
                f"{operation} のリクエストを送信できませんでした: {detail}{outcome}"
            )
        if transport_error is not None:
            raise transport_error
        assert response is not None
        if allow_not_found and response.status_code == 404:
            return None
        if not response.is_success:
            self._error(response, operation)
        return response

    def _json_response(
        self, response: httpx.Response, operation: str, *, allow_empty: bool = False
    ) -> object:
        if allow_empty and not response.content.strip():
            return {}
        error: ResponseFormatError | None = None
        try:
            result = response.json()
        except (json.JSONDecodeError, UnicodeError, ValueError):
            text = self._redact(response.text.strip())[:4096]
            suffix = f": {text}" if text else ""
            error = ResponseFormatError(
                f"{operation} のレスポンスをJSONとして解釈できませんでした{suffix}"
            )
        if error is not None:
            raise error
        return result

    def _get_json(self, path: str, operation: str, *, authenticated: bool = True) -> object:
        response = self._request("GET", path, operation, authenticated=authenticated)
        assert response is not None
        return self._json_response(response, operation)

    def _send_body(
        self, method: str, path: str, operation: str, raw: bytes, content_type: str
    ) -> httpx.Response:
        body = prepare_body(raw)
        response = self._request(
            method,
            path,
            operation,
            content=body.data,
            content_type=content_type,
            content_encoding="gzip" if body.gzipped else None,
        )
        assert response is not None
        return response

    def _put_json(self, path: str, request: object, operation: str) -> object:
        serialization_error: ValueError | None = None
        try:
            raw = json.dumps(
                _api_payload(request),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            serialization_error = ValueError(
                f"{operation} のリクエストをJSONにできませんでした: {self._redact(str(exc))}"
            )
        if serialization_error is not None:
            raise serialization_error
        response = self._send_body("PUT", path, operation, raw, "application/json")
        response_error: ResponseFormatError | None = None
        try:
            result = self._json_response(response, operation, allow_empty=True)
        except ResponseFormatError:
            response_error = ResponseFormatError(
                f"{operation} のレスポンスJSONが不正です。 サーバー側の適用結果は不明です。"
            )
        if response_error is not None:
            raise response_error

        return result

    def get_problem_edit(self, problem_id: int) -> ProblemEditContent:
        pid = _path_id(problem_id, "問題ID")
        return ProblemEditContent.from_api_dict(
            self._get_json(f"/v1/problems/{pid}/edit", "問題の取得")
        )

    get_problem = get_problem_edit

    def get_subtask(self, problem_id: int) -> SubtaskSet:
        pid = _path_id(problem_id, "問題ID")
        return SubtaskSet.from_api_dict(
            self._get_json(f"/v1/problems/{pid}/subtask", "部分点の取得")
        )

    def save_subtask(self, problem_id: int, request: SubtaskSet) -> SubtaskSaveResponse:
        pid = _path_id(problem_id, "問題ID")
        validated = SubtaskSet.from_api_dict(request.to_api_dict())
        operation = "部分点の保存"
        return _parse_write_response(
            self._put_json(f"/v1/problems/{pid}/subtask", validated, operation),
            operation,
            SubtaskSaveResponse.from_api_dict,
        )

    def save_problem_edit(
        self,
        problem_id: int,
        request: ProblemEditRequest | ProblemSettings | Mapping[str, object],
        statement: Statement | None = None,
    ) -> SaveResponse:
        pid = _path_id(problem_id, "問題ID")
        if isinstance(request, ProblemSettings):
            if statement is None:
                raise TypeError("ProblemSettings と一緒に Statement を指定してください")
            request = ProblemEditRequest(request, statement)
        operation = "問題の保存"
        return _parse_write_response(
            self._put_json(f"/v1/problems/{pid}/edit", request, operation),
            operation,
            SaveResponse.from_api_dict,
        )

    save_problem = save_problem_edit

    def get_generator(self, problem_id: int) -> GeneratorContent:
        pid = _path_id(problem_id, "問題ID")
        return GeneratorContent.from_api_dict(
            self._get_json(f"/v1/problems/{pid}/generator", "ジェネレータの取得")
        )

    def save_generator(
        self, problem_id: int, request: GeneratorRequest | Mapping[str, object]
    ) -> SaveResponse:
        pid = _path_id(problem_id, "問題ID")
        operation = "ジェネレータの保存"
        return _parse_write_response(
            self._put_json(f"/v1/problems/{pid}/generator", request, operation),
            operation,
            SaveResponse.from_api_dict,
        )

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent | None:
        pid = _path_id(problem_id, "問題ID")
        response = self._request(
            "GET", f"/v1/problems/{pid}/code", "ジャッジコードの取得", allow_not_found=True
        )
        if response is None:
            return None
        return JudgeCodeContent.from_api_dict(self._json_response(response, "ジャッジコードの取得"))

    def save_judge_code(
        self, problem_id: int, request: JudgeCodeRequest | Mapping[str, object]
    ) -> JudgeCodeSaveResponse:
        pid = _path_id(problem_id, "問題ID")
        operation = "ジャッジコードの保存"
        return _parse_write_response(
            self._put_json(f"/v1/problems/{pid}/code", request, operation),
            operation,
            JudgeCodeSaveResponse.from_api_dict,
        )

    def wait_for_judge_code(
        self,
        problem_id: int,
        *,
        interval: float = 3.0,
        timeout: float = 180.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> JudgeCodeContent | None:
        interval_seconds = _poll_seconds(interval, label="interval", allow_zero=False)
        timeout_seconds = _poll_seconds(timeout, label="timeout", allow_zero=True)
        judging = judging_ids(self.statuses())
        deadline = monotonic() + timeout_seconds
        last_result: JudgeCodeContent | None = None
        while True:
            result = self.get_judge_code(problem_id)
            if result is not None:
                last_result = result
            if result is not None and result.status and result.status not in judging:
                return result
            remaining = deadline - monotonic()
            if remaining <= 0:
                return last_result
            sleep(min(interval_seconds, remaining))

    def get_validator(self, problem_id: int) -> ValidatorContent:
        pid = _path_id(problem_id, "問題ID")
        return ValidatorContent.from_api_dict(
            self._get_json(
                f"/v1/problems/{pid}/validator",
                "validator の取得",
            )
        )

    def save_validator(
        self, problem_id: int, request: ValidatorRequest | Mapping[str, object]
    ) -> JudgeCodeSaveResponse:
        pid = _path_id(problem_id, "問題ID")
        operation = "validator の保存"
        return _parse_write_response(
            self._put_json(f"/v1/problems/{pid}/validator", request, operation),
            operation,
            JudgeCodeSaveResponse.from_api_dict,
        )

    put_validator = save_validator

    def get_editorial(self, problem_id: int) -> EditorialContent:
        pid = _path_id(problem_id, "問題ID")
        return EditorialContent.from_api_dict(
            self._get_json(f"/v1/problems/{pid}/editorial", "解説の取得")
        )

    def save_editorial(
        self, problem_id: int, request: EditorialRequest | Statement | Mapping[str, object]
    ) -> SaveResponse:
        pid = _path_id(problem_id, "問題ID")
        if isinstance(request, Statement):
            request = EditorialRequest(request)
        operation = "解説の保存"
        return _parse_write_response(
            self._put_json(f"/v1/problems/{pid}/editorial", request, operation),
            operation,
            SaveResponse.from_api_dict,
        )

    def list_testcases(self, problem_id: int, which: Which | str) -> list[str]:
        pid = _path_id(problem_id, "問題ID")
        side = _which_value(which)
        raw = self._get_json(f"/v1/problems/{pid}/file/{side}", "テストケース一覧の取得")
        if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
            raise ResponseFormatError("テストケース一覧は文字列の配列ではありません")
        names: list[str] | None = None
        try:
            names = [validate_testcase_name(name) for name in raw]
        except ValueError:
            names = None
        if names is None:
            raise ResponseFormatError("テストケース一覧に安全でないファイル名があります")
        return names

    def list_testcases_detail(
        self,
        problem_id: int,
        which: Which | str,
    ) -> list[TestcaseInfo]:
        pid = _path_id(problem_id, "問題ID")
        side = _which_value(which)
        raw = self._get_json(f"/v1/problems/{pid}/file/{side}?detail=1", "テストケース一覧の取得")
        if not isinstance(raw, list):
            raise ResponseFormatError("テストケース詳細一覧は配列ではありません")
        details = [TestcaseInfo.from_api_dict(item) for item in raw]
        safe = True
        try:
            for detail in details:
                validate_testcase_name(detail.name)
        except ValueError:
            safe = False
        if not safe:
            raise ResponseFormatError("テストケース一覧に安全でないファイル名があります")
        return details

    def testcase_name_rule(self) -> str:
        rule = TestcaseNameRule.from_api_dict(
            self._get_json(
                "/v1/testcase_name_rule",
                "テストケース名の規則の取得",
                authenticated=False,
            )
        )
        return rule.allowed_chars

    def get_testcase(self, problem_id: int, which: Which | str, name: str) -> bytes:
        pid = _path_id(problem_id, "問題ID")
        side = _which_value(which)
        safe_name = validate_testcase_name(name)
        response = self._request(
            "GET", f"/v1/problems/{pid}/file/{side}/{safe_name}", "テストケースの取得"
        )
        assert response is not None
        return response.content

    def upload_testcases(
        self,
        problem_id: int,
        which: Which | str,
        files: Mapping[str, bytes] | Iterable[tuple[str, bytes]],
    ) -> UploadResponse:
        pid = _path_id(problem_id, "問題ID")
        side = _which_value(which)
        items = files.items() if isinstance(files, Mapping) else files
        parts = [
            Part.file("newfiles", validate_testcase_name(name), content) for name, content in items
        ]
        boundary, raw = multipart(parts)
        response = self._send_body(
            "POST",
            f"/v1/problems/{pid}/file/{side}",
            "テストケースのアップロード",
            raw,
            f"multipart/form-data; boundary={boundary}",
        )
        result: UploadResponse | None = None
        try:
            result = UploadResponse.from_api_dict(
                self._json_response(response, "テストケースのアップロード")
            )
            for name in result.file_names:
                validate_testcase_name(name)
        except (ResponseFormatError, ValueError):
            result = None
        if result is None:
            raise ResponseFormatError(
                "テストケースのアップロード応答が不正です。 サーバー側の適用結果は不明です。"
            )
        return UploadResponse(result.file_names, self._redact(result.warning))

    def delete_testcase(self, problem_id: int, which: Which | str, name: str) -> None:
        pid = _path_id(problem_id, "問題ID")
        side = _which_value(which)
        safe_name = validate_testcase_name(name)
        self._request(
            "DELETE",
            f"/v1/problems/{pid}/file/{side}/{safe_name}",
            "テストケースの削除",
        )

    def submit(self, problem_id: int, lang: str, source: str) -> str:
        pid = _path_id(problem_id, "問題ID")
        boundary, raw = multipart([Part.text("lang", lang), Part.text("source", source)])
        response = self._send_body(
            "POST",
            f"/v1/problems/{pid}/submit",
            "提出",
            raw,
            f"multipart/form-data; boundary={boundary}",
        )
        return self._redact(response.text)

    def get_submission(self, submission_id: int) -> SubmissionInfo:
        sid = _path_id(submission_id, "提出ID")
        return SubmissionInfo.from_api_dict(self._get_json(f"/v1/submissions/{sid}", "提出の取得"))

    def set_solution(
        self, submission_id: int, request: SolutionRequest | Mapping[str, object]
    ) -> SaveResponse:
        sid = _path_id(submission_id, "提出ID")
        operation = "想定解の登録"
        return _parse_write_response(
            self._put_json(f"/v1/submissions/{sid}/solution", request, operation),
            operation,
            SaveResponse.from_api_dict,
        )

    save_solution = set_solution

    def statuses(self) -> list[StatusInfo]:
        raw = self._get_json("/v1/statuses", "ステータス一覧の取得", authenticated=False)
        if not isinstance(raw, list):
            raise ResponseFormatError("ステータス一覧は配列ではありません")
        return [StatusInfo.from_api_dict(item) for item in raw]

    get_statuses = statuses

    def languages(self) -> list[Language]:
        raw = self._get_json("/v1/languages", "言語一覧の取得", authenticated=False)
        if not isinstance(raw, list):
            raise ResponseFormatError("言語一覧は配列ではありません")
        return [Language.from_api_dict(item) for item in raw]

    get_languages = languages


__all__ = [
    "DEFAULT_BASE_URL",
    "USER_AGENT",
    "APIError",
    "HTTPError",
    "YukicoderAPIError",
    "YukicoderClient",
    "YukicoderHTTPError",
    "YukicoderTransportError",
]
