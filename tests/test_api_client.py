from __future__ import annotations

import gzip
import json
import traceback

import httpx
import pytest

from yukitools_rime.api import (
    USER_AGENT,
    GeneratorRequest,
    JudgeCodeRequest,
    ProblemEditRequest,
    ResponseFormatError,
    SolutionRequest,
    ValidatorRequest,
    YukicoderClient,
    YukicoderHTTPError,
    YukicoderTransportError,
)
from yukitools_rime.models import ProblemSettings, Statement, Which


def test_all_routes_use_the_documented_methods_and_paths() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        path = request.url.path
        if request.method == "GET" and path.endswith("/generator"):
            return httpx.Response(
                200,
                json={"langId": "cpp17", "source": "x", "enable": True, "testCaseNum": 3},
            )
        if request.method == "GET" and path.endswith("/code"):
            return httpx.Response(200, json={"langId": "cpp17", "source": "x", "status": "AC"})
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


@pytest.mark.parametrize("token", ["tökén", "token with space", "token\r\nheader"])
def test_client_rejects_tokens_unsafe_for_authorization_headers(token: str) -> None:
    with pytest.raises(ValueError) as caught:
        YukicoderClient(token)

    assert token not in str(caught.value)


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

    with (
        YukicoderClient(
            "top-secret", "https://example.test/api", transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(YukicoderHTTPError) as caught,
    ):
        client.save_generator(1, {"langId": "x", "source": "", "testCaseNum": 0})
    assert count == 1
    assert "403" in str(caught.value)
    assert "ヒント" in str(caught.value)
    assert "top-secret" not in str(caught.value)
    assert "top-secret" not in repr(client)


def test_response_gzip_is_decoded_by_httpx() -> None:
    compressed = gzip.compress(b"raw testcase\n")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=compressed, headers={"Content-Encoding": "gzip"})
    )
    with YukicoderClient("t", "https://example.test/api", transport=transport) as client:
        assert client.get_testcase(1, "out", "x.txt") == b"raw testcase\n"


def test_problem_get_and_save_use_edit_route_and_exclude_read_only_fields() -> None:
    requests: list[httpx.Request] = []
    remote = {
        "problemId": 7,
        "title": "remote",
        "tags": "tag",
        "level": 2.0,
        "timeLimitMs": 2000,
        "memoryLimit": 512,
        "epsMode": "-",
        "eps": "0",
        "wip": True,
        "recruitingTester": False,
        "problemType": 0,
        "judgeType": 0,
        "showAns": False,
        "enablePureJudge": False,
        "forceSingleServerJudge": False,
        "allowedLangs": [],
        "content": "remote body",
        "isMarkdown": True,
        "showable": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=remote)
        body = json.loads(request.content)
        assert body["title"] == "local"
        assert body["markdown"] == "# local\n"
        assert body["html"] == ""
        assert not {"problemId", "content", "isMarkdown", "showable"} & body.keys()
        return httpx.Response(200, json={"Message": "saved"})

    settings = ProblemSettings(
        title="local",
        tags="tag",
        level=2.0,
        time_limit_ms=2000,
        memory_limit=512,
        eps_mode="-",
        eps="0",
        wip=True,
        recruiting_tester=False,
        problem_type=0,
        judge_type=0,
        show_ans=False,
        enable_pure_judge=False,
        force_single_server_judge=False,
        allowed_langs=(),
    )
    transport = httpx.MockTransport(handler)
    with YukicoderClient("token", "https://example.test/api", transport=transport) as client:
        assert client.get_problem_edit(7).problem_id == 7
        result = client.save_problem_edit(
            7, ProblemEditRequest(settings, Statement.markdown("# local\n"))
        )

    assert result.message == "saved"
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v1/problems/7/edit"),
        ("PUT", "/api/v1/problems/7/edit"),
    ]
    assert all(request.headers["authorization"] == "Bearer token" for request in requests)


@pytest.mark.parametrize("status_code", [401, 404, 500])
def test_problem_http_errors_do_not_retry(status_code: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text="request failed")

    transport = httpx.MockTransport(handler)
    with (
        YukicoderClient("token", "https://example.test/api", transport=transport) as client,
        pytest.raises(YukicoderHTTPError) as caught,
    ):
        client.get_problem_edit(7)

    assert caught.value.status_code == status_code
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v1/problems/7/edit")
    ]


def test_success_with_invalid_json_raises_response_format_error() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"not json")

    transport = httpx.MockTransport(handler)
    with (
        YukicoderClient("token", "https://example.test/api", transport=transport) as client,
        pytest.raises(ResponseFormatError),
    ):
        client.get_problem_edit(7)

    assert len(requests) == 1


def test_transport_error_is_wrapped_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("connection failed", request=request)

    transport = httpx.MockTransport(handler)
    with (
        YukicoderClient("token", "https://example.test/api", transport=transport) as client,
        pytest.raises(YukicoderTransportError),
    ):
        client.get_problem_edit(7)

    assert len(requests) == 1


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.test/api",
        "https://user:password@example.test/api",
    ],
)
def test_authenticated_client_rejects_insecure_or_credentialed_base_url(
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match=r"API|https|ユーザー"):
        YukicoderClient("top-secret", base_url)


def test_anonymous_client_allows_http_without_authorization() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[{"Id": "cpp23", "Name": "C++", "Ver": "23", "Status": ""}],
        )

    with YukicoderClient.anonymous(
        "http://example.test/api",
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.languages()[0].id == "cpp23"

    assert len(requests) == 1
    assert requests[0].url.scheme == "http"
    assert "authorization" not in requests[0].headers


def test_remote_unsafe_testcase_names_are_generic_response_errors() -> None:
    unsafe_name = "../server-secret-name"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[unsafe_name])
        return httpx.Response(
            200,
            json={"FileNames": [unsafe_name], "Warning": ""},
        )

    transport = httpx.MockTransport(handler)
    with YukicoderClient(
        "token",
        "https://example.test/api",
        transport=transport,
    ) as client:
        with pytest.raises(ResponseFormatError) as list_error:
            client.list_testcases(1, Which.IN)
        with pytest.raises(ResponseFormatError) as upload_error:
            client.upload_testcases(1, Which.IN, {"safe.txt": b"input"})

    assert unsafe_name not in str(list_error.value)
    assert unsafe_name not in str(upload_error.value)
    assert "適用結果は不明" in str(upload_error.value)


def test_upload_warning_and_submit_response_redact_token() -> None:
    token = "top-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            return httpx.Response(200, text=f"Bearer {token}; value={token}")
        return httpx.Response(
            200,
            json={
                "FileNames": ["safe.txt"],
                "Warning": f"Bearer {token}; value={token}",
            },
        )

    with YukicoderClient(
        token,
        "https://example.test/api",
        transport=httpx.MockTransport(handler),
    ) as client:
        warning = client.upload_testcases(
            1,
            Which.IN,
            {"safe.txt": b"input"},
        ).warning
        response = client.submit(1, "cpp23", "int main() {}")

    assert token not in warning
    assert token not in response
    assert "<redacted>" in warning
    assert "<redacted>" in response


def test_non_get_transport_error_reports_unknown_remote_outcome() -> None:
    token = "transport-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Bearer {token}", request=request)

    with (
        YukicoderClient(
            token,
            "https://example.test/api",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(YukicoderTransportError) as caught,
    ):
        client.save_generator(1, GeneratorRequest("cpp23", "source", 1))

    message = str(caught.value)
    assert "適用結果は不明" in message
    assert token not in message


def test_invalid_write_json_reports_unknown_outcome_and_redacts_token() -> None:
    token = "response-secret"
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text=f"not-json Bearer {token}"))

    with (
        YukicoderClient(
            token,
            "https://example.test/api",
            transport=transport,
        ) as client,
        pytest.raises(ResponseFormatError) as caught,
    ):
        client.save_generator(1, GeneratorRequest("cpp23", "source", 1))

    message = str(caught.value)
    assert "適用結果は不明" in message
    assert token not in message
    assert "not-json" not in message


def _exception_rendering(error: BaseException) -> str:
    return "\n".join(
        [
            str(error),
            repr(error),
            "".join(traceback.format_exception(error)),
        ]
    )


def test_injected_client_defaults_are_not_inherited_or_closed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/languages"):
            return httpx.Response(
                200,
                json=[{"Id": "cpp23", "Name": "C++", "Ver": "23"}],
            )
        if request.method == "PUT":
            return httpx.Response(200, json={"Message": "saved"})
        return httpx.Response(200, json={})

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer inherited",
            "Cookie": "header-cookie=secret",
            "Content-Type": "application/inherited",
            "Content-Encoding": "br",
            "User-Agent": "inherited-agent",
        },
        cookies={"jar-cookie": "secret"},
        auth=("default-user", "default-password"),
        timeout=9.0,
        follow_redirects=True,
    ) as injected:
        with YukicoderClient(
            "selected-token",
            "https://example.test/api",
            http_client=injected,
        ) as client:
            client.get_generator(1)
            client.languages()
            client.save_generator(1, GeneratorRequest("cpp23", "source", 1))
        assert not injected.is_closed

    assert len(requests) == 3
    assert all(request.headers["user-agent"] == USER_AGENT for request in requests)
    assert all("cookie" not in request.headers for request in requests)
    assert requests[0].headers["authorization"] == "Bearer selected-token"
    assert "content-type" not in requests[0].headers
    assert "content-encoding" not in requests[0].headers
    assert "authorization" not in requests[1].headers
    assert "content-type" not in requests[1].headers
    assert requests[2].headers["authorization"] == "Bearer selected-token"
    assert requests[2].headers["content-type"] == "application/json"
    assert "content-encoding" not in requests[2].headers
    assert requests[0].extensions["timeout"] == {
        "connect": 9.0,
        "read": 9.0,
        "write": 9.0,
        "pool": 9.0,
    }


def test_injected_client_redirect_setting_is_overridden() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "/credential-target"})

    with (
        httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as injected,
        YukicoderClient(
            "token",
            "https://example.test/api",
            http_client=injected,
        ) as client,
        pytest.raises(YukicoderHTTPError) as caught,
    ):
        client.get_generator(1)

    assert caught.value.status_code == 302
    assert len(requests) == 1


def test_transport_exception_has_no_secret_bearing_chain() -> None:
    token = "transport-chain-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"Bearer {token}; token={token}",
            request=request,
        )

    with (
        YukicoderClient(
            token,
            "https://example.test/api",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(YukicoderTransportError) as caught,
    ):
        client.get_generator(1)

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert token not in _exception_rendering(error)
    assert "<redacted>" in str(error)


def test_json_exception_has_no_secret_bearing_chain() -> None:
    token = "json-chain-secret"
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, text=f"not-json Bearer {token} token={token}")
    )

    with (
        YukicoderClient(token, "https://example.test/api", transport=transport) as client,
        pytest.raises(ResponseFormatError) as caught,
    ):
        client.get_generator(1)

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert token not in _exception_rendering(error)
    assert "<redacted>" in str(error)


def test_invalid_write_response_has_no_exception_chain() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"Message": 1}))

    with (
        YukicoderClient("token", "https://example.test/api", transport=transport) as client,
        pytest.raises(ResponseFormatError) as caught,
    ):
        client.save_generator(1, GeneratorRequest("cpp23", "source", 1))

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "適用結果は不明" in str(caught.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://[::1",
        "https://example.test:not-a-port/api",
        "https://example.test:65536/api",
        "https://user@example.test/api",
        "https://user:password@example.test/api",
        "https://example.test/api?query=1",
        "https://example.test/api#fragment",
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
        "ftp://example.test/api",
        "example.test/api",
        "",
    ],
)
def test_invalid_base_urls_use_public_value_error(base_url: str) -> None:
    token = "url-validation-secret"
    with pytest.raises(ValueError, match="APIベースURL") as caught:
        YukicoderClient(token, base_url)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert token not in _exception_rendering(caught.value)


def test_non_string_base_url_uses_public_value_error() -> None:
    with pytest.raises(ValueError, match="APIベースURL"):
        YukicoderClient.anonymous(123)  # type: ignore[arg-type]


def test_valid_ipv6_base_url_keeps_port_and_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[{"Id": "cpp23", "Name": "C++", "Ver": "23"}],
        )

    with YukicoderClient.anonymous(
        "http://[::1]:8080/api/",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.languages()

    assert seen[0].url.host == "::1"
    assert seen[0].url.port == 8080
    assert seen[0].url.path == "/api/v1/languages"


def test_get_empty_success_response_is_a_format_error() -> None:
    with (
        YukicoderClient(
            "token",
            "https://example.test/api",
            transport=httpx.MockTransport(lambda _: httpx.Response(204)),
        ) as client,
        pytest.raises(ResponseFormatError),
    ):
        client.get_generator(1)


def test_empty_write_success_response_uses_response_defaults() -> None:
    with YukicoderClient(
        "token",
        "https://example.test/api",
        transport=httpx.MockTransport(lambda _: httpx.Response(204)),
    ) as client:
        result = client.save_generator(1, GeneratorRequest("cpp23", "source", 1))

    assert result.message == ""


def test_problem_and_language_required_fields_are_checked_by_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/languages"):
            return httpx.Response(200, json=[{"Id": "cpp23", "Name": "C++"}])
        return httpx.Response(200, json={"title": "missing settings and id"})

    with YukicoderClient(
        "token",
        "https://example.test/api",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ResponseFormatError, match="problemId"):
            client.get_problem_edit(1)
        with pytest.raises(ResponseFormatError, match="Ver"):
            client.languages()


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


def _judging_response(*ids: str) -> httpx.Response:
    return httpx.Response(
        200,
        json=[{"id": status, "category": "judging"} for status in ids],
    )


def test_judge_polling_uses_server_categories_until_ac() -> None:
    statuses = iter([None, "QueuedByServer", "Judge", "AC"])
    clock = _FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/statuses"):
            return _judging_response("QueuedByServer", "Judge")
        status = next(statuses)
        if status is None:
            return httpx.Response(404)
        return httpx.Response(200, json={"status": status})

    with YukicoderClient(
        "token",
        "https://example.test/api",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.wait_for_judge_code(
            1,
            interval=3.0,
            timeout=10.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert result is not None
    assert result.status == "AC"
    assert clock.sleeps == [3.0, 3.0, 3.0]


def test_judge_polling_timeout_uses_exact_remaining_delay() -> None:
    clock = _FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        if request.url.path.endswith("/statuses"):
            return _judging_response("WJ")
        requests += 1
        return httpx.Response(404)

    with YukicoderClient(
        "token",
        "https://example.test/api",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.wait_for_judge_code(
            1,
            interval=3.0,
            timeout=5.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert result is None
    assert requests == 3
    assert clock.sleeps == [3.0, 2.0]


def test_judge_polling_zero_timeout_returns_latest_result_without_sleep() -> None:
    clock = _FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/statuses"):
            return _judging_response("WJ")
        return httpx.Response(200, json={"status": "WJ"})

    with YukicoderClient(
        "token",
        "https://example.test/api",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.wait_for_judge_code(
            1,
            timeout=0.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert result is not None
    assert result.status == "WJ"
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("interval", "timeout"),
    [
        (0.0, 1.0),
        (-1.0, 1.0),
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        (True, 1.0),
        (1.0, -1.0),
        (1.0, float("nan")),
        (1.0, float("inf")),
        (1.0, True),
        (10**1000, 1.0),
        (1.0, 10**1000),
    ],
)
def test_judge_polling_rejects_invalid_durations_before_request(
    interval: float,
    timeout: float,
) -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"status": "AC"})

    with (
        YukicoderClient(
            "token",
            "https://example.test/api",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ValueError),
    ):
        client.wait_for_judge_code(1, interval=interval, timeout=timeout)

    assert requests == 0


@pytest.mark.parametrize(
    ("problem_id", "which", "name"),
    [
        (-1, "in", "safe.txt"),
        (True, "in", "safe.txt"),
        (1, "invalid", "safe.txt"),
        (1, "in", "CON.txt"),
        (1, "in", "../unsafe"),
    ],
)
def test_testcase_preflight_errors_send_no_request(
    problem_id: int,
    which: str,
    name: str,
) -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200)

    with (
        YukicoderClient(
            "token",
            "https://example.test/api",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ValueError),
    ):
        client.get_testcase(problem_id, which, name)

    assert requests == 0


def test_remote_testcase_name_error_does_not_retain_unsafe_name() -> None:
    secret = "remote-name-secret"
    unsafe_name = f"../{secret}"
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=[unsafe_name]))

    with (
        YukicoderClient("token", "https://example.test/api", transport=transport) as client,
        pytest.raises(ResponseFormatError) as caught,
    ):
        client.list_testcases(1, "in")

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in _exception_rendering(caught.value)


def _minimal_problem_settings() -> ProblemSettings:
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


def test_http_client_and_transport_are_mutually_exclusive() -> None:
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as injected,
        pytest.raises(ValueError, match="同時"),
    ):
        YukicoderClient(
            "token",
            "https://example.test/api",
            http_client=injected,
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        )


@pytest.mark.parametrize("payload", [{1: "value"}, object()])
def test_invalid_request_objects_fail_before_network(payload: object) -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200)

    with (
        YukicoderClient(
            "token",
            "https://example.test/api",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ValueError, match="JSON"),
    ):
        client.save_generator(1, payload)  # type: ignore[arg-type]

    assert requests == 0


def test_nonfinite_json_value_fails_before_network() -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200)

    with (
        YukicoderClient(
            "token",
            "https://example.test/api",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ValueError, match="JSON"),
    ):
        client.save_generator(1, {"value": float("nan")})

    assert requests == 0


def test_serialization_exception_is_redacted_without_chain() -> None:
    token = "serialization-chain-secret"

    class ExplodingRequest:
        def to_api_dict(self) -> dict[str, object]:
            raise ValueError(f"Bearer {token}; token={token}")

    with (
        YukicoderClient(
            token,
            "https://example.test/api",
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        ) as client,
        pytest.raises(ValueError) as caught,
    ):
        client.save_generator(1, ExplodingRequest())  # type: ignore[arg-type]

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert token not in _exception_rendering(caught.value)
    assert "<redacted>" in str(caught.value)


def test_problem_settings_require_statement_before_network() -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200)

    with (
        YukicoderClient(
            "token",
            "https://example.test/api",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(TypeError, match="Statement"),
    ):
        client.save_problem_edit(1, _minimal_problem_settings())

    assert requests == 0


def test_statement_editorial_request_is_serialized() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"Message": "saved"})

    with YukicoderClient(
        "token",
        "https://example.test/api",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.save_editorial(1, Statement.html("<p>editorial</p>"))

    assert result.message == "saved"
    assert bodies == [{"html": "<p>editorial</p>"}]


def test_languages_rejects_non_array_response() -> None:
    with (
        YukicoderClient.anonymous(
            "https://example.test/api",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"Id": "cpp23"})),
        ) as client,
        pytest.raises(ResponseFormatError, match="配列"),
    ):
        client.languages()


def test_anonymous_http_error_redacts_bearer_text() -> None:
    secret = "anonymous-response-secret"
    with (
        YukicoderClient.anonymous(
            "https://example.test/api",
            transport=httpx.MockTransport(lambda _: httpx.Response(403, text=f"Bearer {secret}")),
        ) as client,
        pytest.raises(YukicoderHTTPError) as caught,
    ):
        client.languages()

    assert secret not in _exception_rendering(caught.value)
    assert "Bearer <redacted>" in str(caught.value)


@pytest.mark.parametrize("terminal", ["CE", "IE"])
def test_judge_polling_returns_any_server_terminal_immediately(terminal: str) -> None:
    clock = _FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/statuses"):
            return _judging_response("WJ", "Judge")
        return httpx.Response(200, json={"status": terminal})

    with YukicoderClient(
        "token",
        "https://example.test/api",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.wait_for_judge_code(
            1,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert result is not None
    assert result.status == terminal
    assert clock.sleeps == []


def test_v040_routes_parse_models_and_isolate_anonymous_auth() -> None:
    seen: list[tuple[str, str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                request.url.query.decode(),
                request.headers.get("authorization"),
            )
        )
        if request.url.path.endswith("/validator") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "langId": "cpp23",
                    "source": "validator",
                    "status": "WA",
                    "compileMessage": "warning",
                    "cases": [{"name": "case-01.txt", "status": "WA"}],
                },
            )
        if request.url.path.endswith("/validator") and request.method == "PUT":
            assert json.loads(request.content) == {
                "langId": "cpp23",
                "source": "validator",
            }
            return httpx.Response(200, json={"Message": "saved", "status": "WJ"})
        if request.url.path.endswith("/file/in"):
            return httpx.Response(
                200,
                json=[{"name": "case-01.txt", "sha256": "a" * 64, "size": 12}],
            )
        if request.url.path.endswith("/testcase_name_rule"):
            return httpx.Response(200, json={"allowedChars": "abc.-"})
        if request.url.path.endswith("/statuses"):
            return httpx.Response(
                200,
                json=[
                    {"id": "WJ", "category": "judging", "description": "waiting"},
                    {"id": "AC", "category": "success", "description": "accepted"},
                ],
            )
        if request.url.path.endswith("/submissions/99"):
            return httpx.Response(200, json={"status": "AC", "runTimeMs": 42})
        raise AssertionError(f"unexpected route: {request.method} {request.url}")

    with YukicoderClient(
        "secret",
        "https://example.test/api",
        transport=httpx.MockTransport(handler),
    ) as client:
        validator = client.get_validator(7)
        assert validator.compile_message == "warning"
        assert validator.cases is not None
        assert validator.cases[0].name == "case-01.txt"
        assert client.put_validator(7, ValidatorRequest("cpp23", "validator")).status == "WJ"
        details = client.list_testcases_detail(7, Which.IN)
        assert details[0].sha256 == "a" * 64
        assert client.testcase_name_rule() == "abc.-"
        assert client.statuses()[0].category == "judging"
        assert client.get_submission(99).run_time_ms == 42

    assert [(method, path, query) for method, path, query, _ in seen] == [
        ("GET", "/api/v1/problems/7/validator", ""),
        ("PUT", "/api/v1/problems/7/validator", ""),
        ("GET", "/api/v1/problems/7/file/in", "detail=1"),
        ("GET", "/api/v1/testcase_name_rule", ""),
        ("GET", "/api/v1/statuses", ""),
        ("GET", "/api/v1/submissions/99", ""),
    ]
    assert [auth for *_, auth in seen] == [
        "Bearer secret",
        "Bearer secret",
        "Bearer secret",
        None,
        None,
        "Bearer secret",
    ]


@pytest.mark.parametrize(
    ("method_name", "response"),
    [
        ("list_testcases_detail", {}),
        ("statuses", {}),
    ],
)
def test_v040_list_routes_require_json_arrays(method_name: str, response: object) -> None:
    with (
        YukicoderClient(
            "token",
            "https://example.test/api",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        ) as client,
        pytest.raises(ResponseFormatError),
    ):
        method = getattr(client, method_name)
        if method_name == "list_testcases_detail":
            method(1, Which.IN)
        else:
            method()


@pytest.mark.parametrize("payload", [{}, {"allowedChars": ""}, {"allowedChars": 1}])
def test_testcase_name_rule_requires_a_nonempty_string(payload: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    with (
        YukicoderClient(
            "token",
            "https://example.test/api",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ResponseFormatError, match="allowedChars"),
    ):
        client.testcase_name_rule()

    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize(
    "payload",
    [
        [{"name": "../unsafe", "sha256": "a" * 64}],
        [{"name": "safe.txt"}],
        [{"name": "safe.txt", "sha256": 1}],
    ],
)
def test_testcase_detail_rejects_unsafe_names_and_bad_shapes(payload: object) -> None:
    with (
        YukicoderClient(
            "token",
            "https://example.test/api",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        ) as client,
        pytest.raises(ResponseFormatError),
    ):
        client.list_testcases_detail(1, Which.OUT)


def test_validator_bad_write_response_reports_unknown_remote_outcome() -> None:
    with (
        YukicoderClient(
            "token",
            "https://example.test/api",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"Message": 1})),
        ) as client,
        pytest.raises(ResponseFormatError, match="適用結果は不明"),
    ):
        client.save_validator(1, ValidatorRequest("cpp23", "source"))
