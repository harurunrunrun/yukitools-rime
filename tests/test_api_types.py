from __future__ import annotations

from collections.abc import Callable

import pytest

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
    UploadResponse,
    judge_status_is_final,
)
from yukitools_rime.models import ProblemSettings, Statement


def _problem_payload() -> dict[str, object]:
    return {
        "problemId": 7,
        "title": "title",
        "tags": "",
        "level": 1.0,
        "timeLimitMs": 2000,
        "memoryLimit": 512,
        "epsMode": "-",
        "eps": "0",
        "wip": True,
        "recruitingTester": False,
        "problemType": 0,
        "judgeType": 0,
        "content": "body",
        "isMarkdown": True,
        "showable": False,
    }


def _problem_settings() -> ProblemSettings:
    return ProblemSettings(
        title="title",
        tags="",
        level=1.0,
        time_limit_ms=2000,
        memory_limit=512,
        eps_mode="-",
        eps="0",
        wip=True,
        recruiting_tester=False,
        problem_type=0,
        judge_type=0,
    )


def test_problem_response_requires_problem_id() -> None:
    payload = _problem_payload()
    del payload["problemId"]

    with pytest.raises(ResponseFormatError, match="problemId"):
        ProblemEditContent.from_api_dict(payload)


@pytest.mark.parametrize("problem_id", [True, "7", 7.0, None])
def test_problem_response_rejects_non_integer_problem_id(problem_id: object) -> None:
    payload = _problem_payload()
    payload["problemId"] = problem_id

    with pytest.raises(ResponseFormatError, match="problemId"):
        ProblemEditContent.from_api_dict(payload)


def test_problem_settings_validation_is_wrapped_as_response_format_error() -> None:
    payload = _problem_payload()
    payload["title"] = 123

    with pytest.raises(ResponseFormatError, match="問題設定") as caught:
        ProblemEditContent.from_api_dict(payload)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("missing", ["Id", "Name", "Ver"])
def test_language_response_requires_identity_fields(missing: str) -> None:
    payload = {"Id": "cpp23", "Name": "C++", "Ver": "23"}
    del payload[missing]

    with pytest.raises(ResponseFormatError, match=missing):
        Language.from_api_dict(payload)


@pytest.mark.parametrize("key", ["Id", "Name", "Ver", "Status"])
def test_language_response_rejects_non_string_fields(key: str) -> None:
    payload: dict[str, object] = {
        "Id": "cpp23",
        "Name": "C++",
        "Ver": "23",
        "Status": "",
    }
    payload[key] = 1

    with pytest.raises(ResponseFormatError, match=key):
        Language.from_api_dict(payload)


def test_optional_resource_responses_retain_reference_defaults() -> None:
    assert GeneratorContent.from_api_dict({}) == GeneratorContent()
    assert JudgeCodeContent.from_api_dict({}) == JudgeCodeContent()
    assert EditorialContent.from_api_dict({}) == EditorialContent()
    assert SaveResponse.from_api_dict({}) == SaveResponse()
    assert JudgeCodeSaveResponse.from_api_dict({}) == JudgeCodeSaveResponse()
    assert UploadResponse.from_api_dict({}) == UploadResponse()


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (GeneratorContent.from_api_dict, {"enable": 1}),
        (GeneratorContent.from_api_dict, {"testCaseNum": True}),
        (JudgeCodeContent.from_api_dict, {"status": 1}),
        (EditorialContent.from_api_dict, {"isMarkdown": 1}),
        (SaveResponse.from_api_dict, {"Message": 1}),
        (JudgeCodeSaveResponse.from_api_dict, {"status": 1}),
        (UploadResponse.from_api_dict, {"FileNames": [1]}),
        (UploadResponse.from_api_dict, {"Warning": 1}),
    ],
)
def test_resource_responses_reject_wrong_scalar_shapes(
    parser: Callable[[object], object],
    payload: object,
) -> None:
    with pytest.raises(ResponseFormatError):
        parser(payload)


@pytest.mark.parametrize(
    "parser",
    [
        GeneratorContent.from_api_dict,
        JudgeCodeContent.from_api_dict,
        EditorialContent.from_api_dict,
        SaveResponse.from_api_dict,
        JudgeCodeSaveResponse.from_api_dict,
        UploadResponse.from_api_dict,
        Language.from_api_dict,
    ],
)
def test_resource_responses_require_json_objects(
    parser: Callable[[object], object],
) -> None:
    with pytest.raises(ResponseFormatError, match="JSONオブジェクト"):
        parser([])


def test_request_models_emit_only_selected_optional_fields() -> None:
    assert GeneratorRequest("cpp23", "source", 3).to_api_dict() == {
        "langId": "cpp23",
        "source": "source",
        "testCaseNum": 3,
    }
    assert GeneratorRequest(
        "cpp23",
        "source",
        3,
        generate=False,
        prefix="sample",
    ).to_api_dict() == {
        "langId": "cpp23",
        "source": "source",
        "testCaseNum": 3,
        "generate": False,
        "prefix": "sample",
    }
    assert JudgeCodeRequest("cpp23", "judge").to_api_dict() == {
        "langId": "cpp23",
        "source": "judge",
    }
    assert SolutionRequest().to_api_dict() == {}
    assert SolutionRequest(summary="official", delete=False).to_api_dict() == {
        "summary": "official",
        "delete": False,
    }


def test_statement_request_models_use_exclusive_content_fields() -> None:
    problem = ProblemEditRequest(
        _problem_settings(),
        Statement.markdown("# statement\n"),
    ).to_api_dict()
    editorial = EditorialRequest(Statement.html("<p>editorial</p>")).to_api_dict()

    assert problem["markdown"] == "# statement\n"
    assert problem["html"] == ""
    assert editorial == {"html": "<p>editorial</p>"}


@pytest.mark.parametrize(
    ("status", "expected"),
    [("AC", True), ("CE", True), ("WJ", False), ("Judge", False), ("", False)],
)
def test_judge_final_statuses_are_exact(status: str, expected: bool) -> None:
    assert judge_status_is_final(status) is expected
