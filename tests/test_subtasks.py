from __future__ import annotations

import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from yukitools_rime.api import (
    ResponseFormatError,
    Subtask,
    SubtaskSaveResponse,
    SubtaskSet,
    YukicoderClient,
)
from yukitools_rime.errors import FileOperationError, ValidationError
from yukitools_rime.subtasks import fetch_subtasks, read_subtasks, render_subtasks


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        {"subtasks": None},
        {"subtasks": {}},
        {"subtasks": [None]},
        {"subtasks": [{}]},
        {"subtasks": [{"prefixes": "all", "score": 100}]},
        {"subtasks": [{"prefixes": [1], "score": 100}]},
        {"subtasks": [{"prefixes": [], "score": True}]},
        {"subtasks": [{"prefixes": [], "score": 100.0}]},
        {"subtasks": [{"prefixes": []}]},
        {"subtasks": [{"prefixes": [], "score": -1}]},
        {"subtasks": [{"prefixes": [], "score": 101}]},
        {"subtasks": [{"prefixes": [], "score": 99}]},
        {"subtasks": [{"prefixes": [], "score": 100, "name": None}]},
        {"subtasks": [{"prefixes": [], "score": 100, "description": 1}]},
    ],
)
def test_subtask_response_validation(raw: object) -> None:
    with pytest.raises(ResponseFormatError):
        SubtaskSet.from_api_dict(raw)


def test_subtask_round_trip_and_defaults(tmp_path: Path) -> None:
    tasks = SubtaskSet((Subtask(("small",), 30, "小", "小さい入力"), Subtask(("large",), 70)))
    encoded = render_subtasks(tasks)
    assert "小さい入力" in encoded
    assert encoded.endswith("\n")
    assert tasks.to_api_dict() == {
        "subtasks": [
            {"prefixes": ["small"], "score": 30, "name": "小", "description": "小さい入力"},
            {"prefixes": ["large"], "score": 70},
        ]
    }
    assert read_subtasks(tmp_path) is None
    path = tmp_path / "subtask.json"
    path.write_bytes(b"\xef\xbb\xbf" + encoded.replace("\n", "\r\n").encode("utf-8"))
    assert read_subtasks(tmp_path) == tasks
    assert SubtaskSet.from_api_dict(json.loads(encoded)) == tasks
    path.write_text('{"subtasks": []}', encoding="utf-8")
    assert read_subtasks(tmp_path) == SubtaskSet()
    assert SubtaskSet.from_api_dict(
        {"subtasks": [{"prefixes": [], "score": 100, "name": "", "description": ""}]}
    ).to_api_dict() == {"subtasks": [{"prefixes": [], "score": 100}]}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        "null",
        "[]",
        "{}",
        '{"subtasks": [], "typo": true}',
        '{"subtasks": [], "subtasks": []}',
        '{"subtasks": [{"prefixes": [], "score": 100, "typo": 1}]}',
        '{"subtasks": [{"prefixes": [], "score": 100, "score": 100}]}',
        "[" * 2000,
    ],
)
def test_local_invalid_json_is_application_error(tmp_path: Path, text: str) -> None:
    (tmp_path / "subtask.json").write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid subtask JSON") as error:
        read_subtasks(tmp_path)
    assert error.value.__context__ is None


@pytest.mark.parametrize("symlink", [False, True])
def test_local_subtask_requires_regular_file(tmp_path: Path, symlink: bool) -> None:
    path = tmp_path / "subtask.json"
    if symlink:
        try:
            path.symlink_to(tmp_path / "missing")
        except OSError:
            pytest.skip("symlink creation is unavailable")
    else:
        path.mkdir()
    with pytest.raises(FileOperationError):
        read_subtasks(tmp_path)


def test_fetch_with_injected_clients() -> None:
    assert fetch_subtasks(object(), 42) == SubtaskSet()
    client = SimpleNamespace(get_subtask=lambda _pid: SubtaskSet())
    assert fetch_subtasks(client, 42) == SubtaskSet()
    with pytest.raises(ValidationError, match="unexpected response"):
        fetch_subtasks(SimpleNamespace(get_subtask=lambda _pid: {}), 42)


@pytest.mark.parametrize("description", ["", "x" * 1500])
def test_subtask_http_contract(description: str) -> None:
    tasks = SubtaskSet((Subtask(("all",), 100, "full", description),))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        assert str(request.url) == "https://mock.example/api/v1/problems/42/subtask"
        assert request.headers["authorization"] == "Bearer test-token"
        if request.method == "GET":
            return httpx.Response(200, json=tasks.to_api_dict())
        assert request.method == "PUT"
        assert request.headers["content-type"] == "application/json"
        body = request.content
        if description:
            assert request.headers["content-encoding"] == "gzip"
            body = gzip.decompress(body)
        else:
            assert "content-encoding" not in request.headers
        assert json.loads(body) == tasks.to_api_dict()
        return httpx.Response(200, json={"Message": "saved", "Warning": "no matching cases"})

    with YukicoderClient(
        "test-token", "https://mock.example/api", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.get_subtask(42) == tasks
        assert client.save_subtask(42, tasks) == SubtaskSaveResponse("saved", "no matching cases")
    assert seen == ["GET", "PUT"]


@pytest.mark.parametrize("raw", [None, {}, {"Message": "", "Warning": ""}])
def test_empty_save_response(raw: object) -> None:
    assert SubtaskSaveResponse.from_api_dict(raw) == SubtaskSaveResponse()


@pytest.mark.parametrize("raw", [[], {"Message": 1}, {"Warning": None}])
def test_invalid_save_response(raw: object) -> None:
    with pytest.raises(ResponseFormatError):
        SubtaskSaveResponse.from_api_dict(raw)


def test_invalid_request_never_sends_http() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid subtask request must be rejected before HTTP")

    with (
        YukicoderClient("test-token", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ResponseFormatError),
    ):
        client.save_subtask(42, SubtaskSet((Subtask(("all",), 99),)))
