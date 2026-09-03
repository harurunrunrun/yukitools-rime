from __future__ import annotations

import gzip
import json

import httpx
import pytest

from yukitools_rime.api import (
    GeneratorRequest,
    JudgeCodeRequest,
    SolutionRequest,
    YukicoderClient,
    YukicoderHTTPError,
)
from yukitools_rime.models import Which


def test_all_routes_use_the_documented_methods_and_paths() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.method, request.url.path, request.headers.get("authorization"))
        )
        path = request.url.path
        if request.method == "GET" and path.endswith("/generator"):
            return httpx.Response(
                200,
                json={"langId": "cpp17", "source": "x", "enable": True, "testCaseNum": 3},
            )
        if request.method == "GET" and path.endswith("/code"):
            return httpx.Response(
                200, json={"langId": "cpp17", "source": "x", "status": "AC"}
            )
        if request.method == "GET" and path.endswith("/editorial"):
            return httpx.Response(200, json={"content": "e", "isMarkdown": True})
        if request.method == "GET" and path.endswith("/file/in"):
            return httpx.Response(200, json=["sample.01.txt"])
        if request.method == "GET" and path.endswith("/sample.01.txt"):
            return httpx.Response(200, content=b"input\n")
        if path == "/api/v1/languages":
            return httpx.Response(
                200, json=[{"Id": "cpp23", "Name": "C++", "Ver": "23", "Status": ""}]
            )
        if request.method == "POST" and path.endswith("/file/out"):
            return httpx.Response(200, json={"FileNames": ["sample.01.txt"], "Warning": ""})
        if request.method == "POST" and path.endswith("/submit"):
            return httpx.Response(200, text="12345")
        if request.method == "PUT" and path.endswith("/code"):
            return httpx.Response(200, json={"Message": "saved", "status": "WJ"})
        return httpx.Response(200, json={"Message": "saved"})

    with YukicoderClient(
        "secret", "https://example.test/api/", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.get_generator(7).lang_id == "cpp17"
        assert client.save_generator(7, GeneratorRequest("cpp17", "x", 3)).message == "saved"
        assert client.get_judge_code(7).status == "AC"  # type: ignore[union-attr]
        assert client.save_judge_code(7, JudgeCodeRequest("cpp17", "x")).status == "WJ"
        assert client.get_editorial(7).content == "e"
        assert client.save_editorial(7, {"html": "e"}).message == "saved"
        assert client.list_testcases(7, Which.IN) == ["sample.01.txt"]
        assert client.get_testcase(7, "in", "sample.01.txt") == b"input\n"
        assert client.upload_testcases(7, "out", {"sample.01.txt": b"answer\n"}).warning == ""
        client.delete_testcase(7, "out", "sample.01.txt")
        assert client.submit(7, "cpp23", "int main() {}") == "12345"
        assert client.set_solution(99, SolutionRequest(summary="official")).message == "saved"
        assert client.languages()[0].id == "cpp23"

    assert [(method, path) for method, path, _ in seen] == [
        ("GET", "/api/v1/problems/7/generator"),
        ("PUT", "/api/v1/problems/7/generator"),
        ("GET", "/api/v1/problems/7/code"),
        ("PUT", "/api/v1/problems/7/code"),
        ("GET", "/api/v1/problems/7/editorial"),
        ("PUT", "/api/v1/problems/7/editorial"),
        ("GET", "/api/v1/problems/7/file/in"),
        ("GET", "/api/v1/problems/7/file/in/sample.01.txt"),
        ("POST", "/api/v1/problems/7/file/out"),
        ("DELETE", "/api/v1/problems/7/file/out/sample.01.txt"),
        ("POST", "/api/v1/problems/7/submit"),
        ("PUT", "/api/v1/submissions/99/solution"),
        ("GET", "/api/v1/languages"),
    ]
    assert all(auth == "Bearer secret" for _, _, auth in seen[:-1])
    assert seen[-1][2] is None


def test_problem_response_parses_eps_from_a_number() -> None:
    payload = {
        "problemId": 7,
        "title": "title",
        "tags": "",
        "level": 1.0,
        "timeLimitMs": 2000,
        "memoryLimit": 512,
        "epsMode": "-",
        "eps": 0.001,
        "wip": True,
        "recruitingTester": False,
        "problemType": 0,
        "judgeType": 0,
        "showAns": False,
        "enablePureJudge": False,
        "forceSingleServerJudge": False,
        "allowedLangs": [],
        "content": "body",
        "isMarkdown": True,
        "showable": False,
    }
    client = YukicoderClient(
        "token",
        "https://example.test/api",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    with client:
        result = client.get_problem_edit(7)
    assert result.problem_id == 7
    assert result.settings.eps == "0.001"


def test_judge_code_404_is_none_without_fallback() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, text="missing")

    with YukicoderClient(
        "token", "https://example.test/api", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.get_judge_code(1) is None
    assert len(requests) == 1
    assert requests[0].method == "GET"


def test_large_json_is_gzipped_once_and_uses_put() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["content-encoding"] == "gzip"
        payload = json.loads(gzip.decompress(request.content))
        assert payload["source"] == "x" * 5000
        return httpx.Response(200, json={"Message": "ok"})

    with YukicoderClient(
        "token", "https://example.test/api", transport=httpx.MockTransport(handler)
    ) as client:
        client.save_generator(1, GeneratorRequest("cpp17", "x" * 5000, 10))
    assert [request.method for request in requests] == ["PUT"]


def test_multipart_uses_required_names_and_can_be_gzipped() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = (
            gzip.decompress(request.content)
            if request.headers.get("content-encoding") == "gzip"
            else request.content
        )
        bodies.append(raw)
        if request.url.path.endswith("/submit"):
            return httpx.Response(200, text="1")
        return httpx.Response(200, json={"FileNames": ["large.txt"]})

    with YukicoderClient(
        "token", "https://example.test/api", transport=httpx.MockTransport(handler)
    ) as client:
        client.upload_testcases(1, "in", {"large.txt": b"x" * 5000})
        client.submit(1, "cpp23", "x" * 5000)
    assert b'name="newfiles"; filename="large.txt"' in bodies[0]
    assert b'name="lang"' in bodies[1]
    assert b'name="source"' in bodies[1]


def test_http_error_has_hint_and_redacts_token_without_retry() -> None:
    count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(403, text="Bearer top-secret was rejected")

    with YukicoderClient(
        "top-secret", "https://example.test/api", transport=httpx.MockTransport(handler)
    ) as client, pytest.raises(YukicoderHTTPError) as caught:
        client.save_generator(1, {"langId": "x", "source": "", "testCaseNum": 0})
    assert count == 1
    assert "403" in str(caught.value)
    assert "ヒント" in str(caught.value)
    assert "top-secret" not in str(caught.value)
    assert "top-secret" not in repr(client)


def test_response_gzip_is_decoded_by_httpx() -> None:
    compressed = gzip.compress(b"raw testcase\n")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, content=compressed, headers={"Content-Encoding": "gzip"}
        )
    )
    with YukicoderClient("t", "https://example.test/api", transport=transport) as client:
        assert client.get_testcase(1, "out", "x.txt") == b"raw testcase\n"
