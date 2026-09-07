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
    StatusInfo,
    SubmissionInfo,
    UploadResponse,
    ValidatorCase,
    ValidatorContent,
    ValidatorRequest,
    judge_status_is_final,
    judging_ids,
)
from yukitools_rime.api.types import (
    TestcaseInfo as APITestcaseInfo,
)
from yukitools_rime.api.types import (
    TestcaseNameRule as APITestcaseNameRule,
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
    [("AC", True), ("CE", True), ("WA", True), ("WJ", False), ("Judge", False), ("", False)],
)
def test_judge_final_statuses_follow_server_categories(status: str, expected: bool) -> None:
    assert judge_status_is_final(status, {"WJ", "Judge"}) is expected


def test_validator_models_parse_status_and_failure_details() -> None:
    content = ValidatorContent.from_api_dict(
        {
            "langId": "cpp23",
            "source": "validator source",
            "status": "WA",
            "compileMessage": "warning",
            "cases": [
                {"name": "sample-01.txt", "status": "AC"},
                {"name": "secret.txt", "status": "RE"},
            ],
        }
    )

    assert content == ValidatorContent(
        lang_id="cpp23",
        source="validator source",
        status="WA",
        compile_message="warning",
        cases=(
            ValidatorCase("sample-01.txt", "AC"),
            ValidatorCase("secret.txt", "RE"),
        ),
    )
    assert content.is_up_to_date({"WJ", "Judge"})
    assert not ValidatorContent(status="WJ").is_up_to_date({"WJ", "Judge"})
    assert not ValidatorContent().is_up_to_date(set())
    assert content.failure_details() == "\n通らなかったケース: secret.txt (RE)"
    assert ValidatorRequest("cpp23", "source").to_api_dict() == {
        "langId": "cpp23",
        "source": "source",
    }


def test_validator_compile_and_bounded_case_failure_details() -> None:
    compile_error = ValidatorContent(status="CE", compile_message=" error: expected ';' \n")
    cases = tuple(ValidatorCase(f"{index}.txt", "WA") for index in range(12))

    assert "error: expected ';'" in compile_error.failure_details()
    assert ValidatorContent(status="CE").failure_details() == ""
    details = ValidatorContent(status="WA", cases=cases).failure_details()
    assert "0.txt (WA)" in details
    assert "9.txt (WA)" in details
    assert "10.txt (WA)" not in details
    assert details.endswith("他 2 件")
    assert (
        ValidatorContent(status="WA", cases=(ValidatorCase("ok.txt", "AC"),)).failure_details()
        == ""
    )
    assert ValidatorContent(status="WA", cases=None).failure_details() == ""


def test_statuses_derive_judging_ids_by_category() -> None:
    statuses = [
        StatusInfo.from_api_dict({"id": "WJ", "category": "judging"}),
        StatusInfo.from_api_dict({"id": "Pending", "category": "judging"}),
        StatusInfo.from_api_dict({"id": "AC", "category": "success"}),
        StatusInfo.from_api_dict({"id": "WA", "category": "wrong"}),
    ]

    assert judging_ids(statuses) == frozenset({"WJ", "Pending"})


def test_submission_testcase_and_name_rule_models_parse() -> None:
    assert SubmissionInfo.from_api_dict({"status": "AC", "runTimeMs": 123}) == SubmissionInfo(
        status="AC",
        run_time_ms=123,
    )
    assert SubmissionInfo.from_api_dict({}) == SubmissionInfo()
    assert APITestcaseInfo.from_api_dict(
        {"name": "case-01.txt", "sha256": "a" * 64, "size": 12}
    ) == APITestcaseInfo("case-01.txt", "a" * 64)
    assert APITestcaseNameRule.from_api_dict({"allowedChars": "abc.-"}).allowed_chars == "abc.-"


@pytest.mark.parametrize(
    ("parser", "payload", "key"),
    [
        (ValidatorCase.from_api_dict, {"status": "AC"}, "name"),
        (ValidatorCase.from_api_dict, {"name": "case.txt"}, "status"),
        (ValidatorContent.from_api_dict, {"cases": {}}, "cases"),
        (ValidatorContent.from_api_dict, {"cases": [1]}, "JSONオブジェクト"),
        (StatusInfo.from_api_dict, {"category": "judging"}, "id"),
        (StatusInfo.from_api_dict, {"id": "WJ"}, "category"),
        (SubmissionInfo.from_api_dict, {"runTimeMs": True}, "runTimeMs"),
        (APITestcaseInfo.from_api_dict, {"sha256": "abc"}, "name"),
        (APITestcaseInfo.from_api_dict, {"name": "case.txt"}, "sha256"),
        (APITestcaseNameRule.from_api_dict, {}, "allowedChars"),
        (APITestcaseNameRule.from_api_dict, {"allowedChars": ""}, "allowedChars"),
    ],
)
def test_new_api_models_reject_malformed_shapes(
    parser: Callable[[object], object],
    payload: object,
    key: str,
) -> None:
    with pytest.raises(ResponseFormatError, match=key):
        parser(payload)
