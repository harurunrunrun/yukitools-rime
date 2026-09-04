from __future__ import annotations

import gzip
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

import yukitools_rime.cli as cli
import yukitools_rime.commands.query as query_module
from yukitools_rime.api import YukicoderClient
from yukitools_rime.models import (
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    SolutionConfig,
)
from yukitools_rime.rime_config import (
    render_problem_block,
    render_project_block,
    render_solution_block,
)

BASE_URL = "https://mock.example/api"
TOKEN = "integration-token"


def settings() -> ProblemSettings:
    return ProblemSettings(
        title="title",
        tags="tag",
        level=1.0,
        time_limit_ms=1000,
        memory_limit=256,
        eps_mode="-",
        eps="0",
        wip=False,
        recruiting_tester=False,
        problem_type=0,
        judge_type=0,
    )


def make_project(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    (root / "PROJECT").write_text(
        render_project_block(ProjectConfig(base_url=BASE_URL, rime_out_dir="generated")),
        encoding="utf-8",
    )
    problem = root / "problem"
    problem.mkdir()
    (problem / "PROBLEM").write_text(
        render_problem_block(ProblemConfig(42, settings(), "A")),
        encoding="utf-8",
    )
    (problem / "statement.md").write_text("local statement\n", encoding="utf-8")
    solution = problem / "solution"
    solution.mkdir()
    (solution / "SOLUTION").write_text(
        render_solution_block(SolutionConfig("py", "main.py", "script")),
        encoding="utf-8",
    )
    (solution / "main.py").write_bytes(b'\xef\xbb\xbfprint("hello")\r\n')
    return problem, solution


def decode_json(request: httpx.Request) -> dict[str, object]:
    body = request.content
    if request.headers.get("content-encoding") == "gzip":
        body = gzip.decompress(body)
    decoded = json.loads(body)
    assert isinstance(decoded, dict)
    return decoded


@dataclass
class RemoteState:
    statement: str = "remote statement\n"
    is_markdown: bool = True
    requests: list[httpx.Request] = field(default_factory=list)
    solution_requests: list[dict[str, object]] = field(default_factory=list)
    submitted_bodies: list[bytes] = field(default_factory=list)

    def problem_payload(self) -> dict[str, object]:
        return {
            "problemId": 42,
            "title": "title",
            "tags": "tag",
            "level": 1.0,
            "timeLimitMs": 1000,
            "memoryLimit": 256,
            "epsMode": "-",
            "eps": "0",
            "wip": False,
            "recruitingTester": False,
            "problemType": 0,
            "judgeType": 0,
            "showAns": False,
            "enablePureJudge": False,
            "forceSingleServerJudge": False,
            "allowedLangs": [],
            "content": self.statement,
            "isMarkdown": self.is_markdown,
            "showable": True,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/problems/42/edit":
            return httpx.Response(200, json=self.problem_payload())
        if request.method == "PUT" and path == "/api/v1/problems/42/edit":
            payload = decode_json(request)
            if "markdown" in payload:
                self.statement = str(payload["markdown"])
                self.is_markdown = True
            else:
                self.statement = str(payload["html"])
                self.is_markdown = False
            return httpx.Response(200, json={"Message": "saved"})
        if request.method == "GET" and path == "/api/v1/problems/42/generator":
            return httpx.Response(200, json={})
        if request.method == "GET" and path == "/api/v1/problems/42/code":
            return httpx.Response(404, text="not configured")
        if request.method == "GET" and path == "/api/v1/problems/42/editorial":
            return httpx.Response(200, json={})
        if request.method == "GET" and path == "/api/v1/problems/42/file/in":
            return httpx.Response(200, json=["sample.01"])
        if request.method == "GET" and path == "/api/v1/problems/42/file/out":
            return httpx.Response(200, json=["sample.01"])
        if request.method == "POST" and path == "/api/v1/problems/42/submit":
            self.submitted_bodies.append(request.content)
            return httpx.Response(200, text="321")
        if request.method == "PUT" and path == "/api/v1/submissions/321/solution":
            self.solution_requests.append(decode_json(request))
            return httpx.Response(200, json={"Message": "saved"})
        if request.method == "GET" and path == "/api/v1/languages":
            return httpx.Response(
                200,
                json=[
                    {"Id": "py", "Name": "Python", "Ver": "3.14", "Status": ""},
                    {"Id": "old", "Name": "Legacy", "Ver": "1", "Status": "disable"},
                ],
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")


def invoke(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = cli.main(argv, stdin=io.StringIO(), stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def test_stateful_transport_round_trip_across_cli_api_and_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, solution = make_project(tmp_path / "project")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("YUKICODER_TOKEN_42", TOKEN)
    remote = RemoteState()
    transport = httpx.MockTransport(remote)
    authenticated_clients: list[YukicoderClient] = []
    anonymous_clients: list[YukicoderClient] = []

    def authenticated(token: str, base_url: str) -> YukicoderClient:
        client = YukicoderClient(token, base_url, transport=transport)
        authenticated_clients.append(client)
        return client

    class Anonymous:
        @staticmethod
        def anonymous(_base_url: str) -> YukicoderClient:
            client = YukicoderClient.anonymous(BASE_URL, transport=transport)
            anonymous_clients.append(client)
            return client

    monkeypatch.setattr(cli, "YukicoderClient", authenticated)
    monkeypatch.setattr(query_module, "YukicoderClient", Anonymous)

    code, stdout, stderr = invoke(["diff", str(project), "--exit-code"])
    assert code == 3
    assert "statement" in stdout
    assert "remote generator is unavailable or empty" in stderr

    code, stdout, stderr = invoke(["pull", str(project)])
    assert code == 0
    assert "更新:" in stdout
    assert "remote generator is unavailable or empty" in stderr
    assert (project / "statement.md").read_text(encoding="utf-8") == remote.statement

    (project / "statement.md").write_text("pushed statement\n", encoding="utf-8")
    code, stdout, stderr = invoke(["push", str(project)])
    assert code == 0
    assert "完了: problem 42 settings/statement" in stdout
    assert stderr == ""
    assert remote.statement == "pushed statement\n"

    code, stdout, stderr = invoke(["testcases", str(project), "--which", "in"])
    assert code == 0
    assert "問題 42 in:" in stdout
    assert "sample.01" in stdout
    assert stderr == ""

    code, stdout, stderr = invoke(["submit", str(solution)])
    assert code == 0
    assert "提出ID 321" in stdout
    assert stderr == ""
    assert any(b'print("hello")\n' in body for body in remote.submitted_bodies)

    code, stdout, stderr = invoke(["solution", "321", str(project), "--summary", "official"])
    assert code == 0
    assert "登録しました" in stdout
    assert stderr == ""
    assert remote.solution_requests == [{"summary": "official"}]

    code, stdout, stderr = invoke(["languages", "--include-disabled"])
    assert code == 0
    assert "Python" in stdout
    assert "Legacy" in stdout
    assert "[disable]" in stdout
    assert stderr == ""

    code, stdout, stderr = invoke(["diff", str(project), "--exit-code"])
    assert code == 0
    assert "settings." not in stdout
    assert "statement:" not in stdout
    assert "remote judge API is unavailable" in stderr

    authenticated_requests = [
        request for request in remote.requests if request.url.path != "/api/v1/languages"
    ]
    assert authenticated_requests
    assert all(
        request.headers.get("authorization") == f"Bearer {TOKEN}"
        for request in authenticated_requests
    )
    language_requests = [
        request for request in remote.requests if request.url.path == "/api/v1/languages"
    ]
    assert len(language_requests) == 1
    assert language_requests[0].headers.get("authorization") is None
    assert all(client._http.is_closed for client in authenticated_clients)
    assert all(client._http.is_closed for client in anonymous_clients)


@pytest.mark.parametrize(
    ("arguments", "empty_statement", "expected_code"),
    [
        (["push", "--prune"], False, 2),
        (["push"], True, 1),
    ],
)
def test_cli_push_preflight_failure_performs_no_http_or_client_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    empty_statement: bool,
    expected_code: int,
) -> None:
    project, _ = make_project(tmp_path / "project")
    if empty_statement:
        (project / "statement.md").write_text(" \n", encoding="utf-8")
    created = False

    def forbidden_client(_token: str, _base_url: str) -> object:
        nonlocal created
        created = True
        raise AssertionError("client must not be created")

    monkeypatch.setattr(cli, "YukicoderClient", forbidden_client)
    code, stdout, stderr = invoke([*arguments, str(project)])

    assert code == expected_code
    assert stdout == ""
    assert "エラー:" in stderr
    assert not created
