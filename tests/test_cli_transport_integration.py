from __future__ import annotations

import gzip
import hashlib
import io
import json
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from functools import partial
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
    ValidatorConfig,
)
from yukitools_rime.rime_config import TestsetConfig as RimeTestsetConfig
from yukitools_rime.rime_config import (
    parse_problem_config,
    parse_solution_config,
    parse_testset_config,
    render_problem_block,
    render_project_block,
    render_solution_block,
    render_testset_block,
)
from yukitools_rime.testcase_sync import DEFAULT_SERVER_CASE_CHARS

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
    validator_source: str = ""
    validator_requests: list[dict[str, object]] = field(default_factory=list)

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
        if request.method == "PUT" and path == "/api/v1/problems/42/validator":
            payload = decode_json(request)
            self.validator_requests.append(payload)
            self.validator_source = str(payload["source"])
            return httpx.Response(200, json={"status": "WJ"})
        if request.method == "GET" and path == "/api/v1/problems/42/validator":
            if not self.validator_source:
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    "langId": "py",
                    "source": self.validator_source,
                    "status": "AC",
                    "compileMessage": "",
                    "cases": [],
                },
            )
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
        if request.method == "GET" and path == "/api/v1/testcase_name_rule":
            return httpx.Response(200, json={"allowedChars": DEFAULT_SERVER_CASE_CHARS})
        if request.method == "GET" and path == "/api/v1/statuses":
            return httpx.Response(200, json=[{"id": "WJ", "category": "judging"}])
        if request.method == "GET" and path == "/api/v1/submissions/321":
            return httpx.Response(200, json={"status": "AC", "runTimeMs": 7})
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
    testset = project / "tests"
    testset.mkdir()
    (testset / "TESTSET").write_text(
        render_testset_block(
            RimeTestsetConfig(validator=ValidatorConfig("py", "validator.py", "script"))
        ),
        encoding="utf-8",
    )
    (testset / "validator.py").write_text("print('validate')\n", encoding="utf-8")
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
    assert "完了: problem 42 validator" in stdout
    assert remote.validator_source == "print('validate')\n"
    assert remote.validator_requests == [{"langId": "py", "source": "print('validate')\n"}]

    code, stdout, stderr = invoke(["testcases", str(project), "--which", "in"])
    assert code == 0
    assert "問題 42 in:" in stdout
    assert "sample.01" in stdout
    assert stderr == ""

    code, stdout, stderr = invoke(["submit", str(solution)])
    assert code == 0
    assert "提出ID 321" in stdout
    assert "結果: AC (7 ms)" in stdout
    assert stderr == ""
    assert any(b'print("hello")\n' in body for body in remote.submitted_bodies)
    stored_solution = parse_solution_config((solution / "SOLUTION").read_text(encoding="utf-8"))
    assert stored_solution.submission_id == 321
    assert len(remote.submitted_bodies) == 1

    code, stdout, stderr = invoke(["submit", str(solution)])
    assert code == 0
    assert "提出済みです: 問題 42, 提出ID 321" in stdout
    assert "--force" in stdout
    assert stderr == ""
    assert len(remote.submitted_bodies) == 1

    code, stdout, stderr = invoke(["submit", str(solution), "--force", "--no-wait"])
    assert code == 0
    assert "提出しました: 問題 42, 提出ID 321" in stdout
    assert "提出済みです" not in stdout
    assert stderr == ""
    assert len(remote.submitted_bodies) == 2
    forced_solution = parse_solution_config((solution / "SOLUTION").read_text(encoding="utf-8"))
    assert forced_solution.submission_id == 321

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
        request
        for request in remote.requests
        if request.url.path
        not in {"/api/v1/languages", "/api/v1/statuses", "/api/v1/testcase_name_rule"}
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
    status_requests = [
        request for request in remote.requests if request.url.path == "/api/v1/statuses"
    ]
    assert len(status_requests) == 2
    assert status_requests[0].headers.get("authorization") is None
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


def install_authenticated_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.MockTransport,
) -> list[YukicoderClient]:
    clients: list[YukicoderClient] = []

    def authenticated(token: str, base_url: str) -> YukicoderClient:
        client = YukicoderClient(token, base_url, transport=transport)
        clients.append(client)
        return client

    monkeypatch.setattr(cli, "YukicoderClient", authenticated)
    return clients


def decode_uploaded_files(request: httpx.Request) -> dict[str, bytes]:
    body = request.content
    if request.headers.get("content-encoding") == "gzip":
        body = gzip.decompress(body)
    content_type = request.headers["content-type"]
    message = BytesParser(policy=policy.default).parsebytes(
        (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode("ascii") + body
    )
    assert message.is_multipart()
    files: dict[str, bytes] = {}
    for part in message.iter_parts():
        filename = part.get_filename()
        content = part.get_payload(decode=True)
        assert isinstance(filename, str)
        assert isinstance(content, bytes)
        files[filename] = content
    return files


@dataclass
class NewProblemRemote:
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/problems/77/edit":
            return httpx.Response(
                200,
                json={
                    "problemId": 77,
                    "title": "Remote title",
                    "tags": "integration",
                    "level": 2.5,
                    "timeLimitMs": 2500,
                    "memoryLimit": 512,
                    "epsMode": "-",
                    "eps": "0",
                    "wip": True,
                    "recruitingTester": False,
                    "problemType": 0,
                    "judgeType": 1,
                    "showAns": False,
                    "enablePureJudge": False,
                    "forceSingleServerJudge": False,
                    "allowedLangs": ["python3"],
                    "content": "# Remote statement\r\n",
                    "isMarkdown": True,
                    "showable": True,
                },
            )
        if request.method == "GET" and path == "/api/v1/problems/77/generator":
            return httpx.Response(
                200,
                json={
                    "langId": "python3",
                    "source": "print('generate')\r\n",
                    "enable": True,
                    "testCaseNum": 2,
                },
            )
        if request.method == "GET" and path == "/api/v1/problems/77/code":
            return httpx.Response(
                200,
                json={
                    "langId": "cpp20",
                    "source": "int main() {}\r\n",
                    "status": "AC",
                },
            )
        if request.method == "GET" and path == "/api/v1/problems/77/validator":
            return httpx.Response(
                200,
                json={
                    "langId": "python3",
                    "source": "print('validate')\r\n",
                    "status": "AC",
                    "compileMessage": "",
                    "cases": [],
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")


def test_cli_init_then_new_crosses_real_client_serialization_and_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "new integration"
    remote = NewProblemRemote()
    transport = httpx.MockTransport(remote)
    clients = install_authenticated_transport(monkeypatch, transport)

    code, stdout, stderr = invoke(["init", str(root)])

    assert code == 0
    assert "初期化しました" in stdout
    assert stderr == ""
    assert remote.requests == []
    assert clients == []
    assert (root / ".env.example").is_file()
    assert "/.env\n" in (root / ".gitignore").read_text(encoding="utf-8")

    (root / "PROJECT").write_text(
        render_project_block(ProjectConfig(base_url=BASE_URL)),
        encoding="utf-8",
    )
    monkeypatch.setenv("YUKICODER_TOKEN_77", TOKEN)

    code, stdout, stderr = invoke(["new", "77", "--project", str(root), "--dir", "a"])

    assert code == 0
    assert "問題 77 を作成しました" in stdout
    assert stderr == ""
    problem = root / "a"
    config = parse_problem_config((problem / "PROBLEM").read_text(encoding="utf-8"))
    testset = parse_testset_config((problem / "tests" / "TESTSET").read_text(encoding="utf-8"))
    assert config.problem_id == 77
    assert config.rime_id == "a"
    assert config.settings.title == "Remote title"
    assert config.settings.time_limit_ms == 2500
    assert (problem / "statement.md").read_bytes() == b"# Remote statement\n"
    assert testset.generator is not None
    assert testset.generator.lang_id == "python3"
    assert testset.generator.src == "generator.py"
    assert testset.generator.rime_kind == "script"
    assert testset.judge is not None
    assert testset.judge.lang_id == "cpp20"
    assert testset.judge.src == "judge.cpp"
    assert testset.judge.rime_kind == "cxx"
    assert testset.validator is not None
    assert testset.validator.lang_id == "python3"
    assert testset.validator.src == "validator.py"
    assert testset.validator.rime_kind == "script"
    assert (problem / "tests" / "generator.py").read_bytes() == b"print('generate')\n"
    assert (problem / "tests" / "judge.cpp").read_bytes() == b"int main() {}\n"
    assert (problem / "tests" / "validator.py").read_bytes() == b"print('validate')\n"
    assert [(request.method, request.url.path) for request in remote.requests] == [
        ("GET", "/api/v1/problems/77/edit"),
        ("GET", "/api/v1/problems/77/generator"),
        ("GET", "/api/v1/problems/77/code"),
        ("GET", "/api/v1/problems/77/validator"),
    ]
    assert all(
        request.headers.get("authorization") == f"Bearer {TOKEN}" for request in remote.requests
    )
    assert len(clients) == 1
    assert clients[0]._http.is_closed


@dataclass
class _TestcaseRemote(RemoteState):
    cases: dict[tuple[str, str], bytes] = field(default_factory=dict)
    fail_output_uploads: int = 0
    fail_output_deletes: int = 0
    file_attempts: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        prefix = "/api/v1/problems/42/file/"
        path = request.url.path
        if not path.startswith(prefix):
            return super().__call__(request)
        self.requests.append(request)
        parts = path.removeprefix(prefix).split("/")
        side = parts[0]
        assert side in {"in", "out"}
        if request.method == "GET" and len(parts) == 1:
            names = sorted(name for candidate_side, name in self.cases if candidate_side == side)
            if request.url.params.get("detail") == "1":
                return httpx.Response(
                    200,
                    json=[
                        {"name": name, "sha256": hashlib.sha256(self.cases[side, name]).hexdigest()}
                        for name in names
                    ],
                )
            return httpx.Response(200, json=names)
        if request.method == "GET" and len(parts) == 2:
            content = self.cases.get((side, parts[1]))
            return httpx.Response(404) if content is None else httpx.Response(200, content=content)
        if request.method == "POST" and len(parts) == 1:
            files = decode_uploaded_files(request)
            names = tuple(sorted(files))
            self.file_attempts.append(("upload", side, names))
            if side == "out" and self.fail_output_uploads:
                self.fail_output_uploads -= 1
                return httpx.Response(500, text="injected output upload failure")
            self.cases.update({(side, name): content for name, content in files.items()})
            return httpx.Response(200, json={"FileNames": list(names), "Warning": ""})
        if request.method == "DELETE" and len(parts) == 2:
            name = parts[1]
            self.file_attempts.append(("delete", side, (name,)))
            if side == "out" and self.fail_output_deletes:
                self.fail_output_deletes -= 1
                return httpx.Response(500, text="injected output delete failure")
            self.cases.pop((side, name), None)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {path}")


def make_testcase_project(root: Path) -> tuple[Path, Path]:
    problem, _ = make_project(root)
    testset = problem / "tests"
    testset.mkdir()
    (testset / "TESTSET").write_text(
        render_testset_block(RimeTestsetConfig()),
        encoding="utf-8",
    )
    cases = problem / "generated" / "tests"
    cases.mkdir(parents=True)
    (cases / "sample.in").write_bytes(b"input")
    (cases / "sample.diff").write_bytes(b"output")
    return problem, cases


def test_cli_push_uploads_zero_byte_input_through_multipart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem, cases = make_testcase_project(tmp_path / "project")
    (cases / "sample.in").write_bytes(b"")
    remote = _TestcaseRemote(statement="local statement\n")
    clients = install_authenticated_transport(
        monkeypatch,
        httpx.MockTransport(remote),
    )
    monkeypatch.setenv("YUKICODER_TOKEN_42", TOKEN)
    monkeypatch.setattr(cli, "push", partial(cli.push, testcase_refresh_delay=0.0))

    code, stdout, stderr = invoke(["push", str(problem), "--testcases"])

    assert code == 0
    assert stderr == ""
    assert "完了: problem 42 testcase inputs [sample]" in stdout
    assert "完了: problem 42 testcase outputs [sample]" in stdout
    assert remote.file_attempts == [
        ("upload", "in", ("sample",)),
        ("upload", "out", ("sample",)),
    ]
    assert remote.cases == {
        ("in", "sample"): b"",
        ("out", "sample"): b"output",
    }
    assert (cases / "sample.in").read_bytes() == b""
    assert (cases / "sample.diff").read_bytes() == b"output"
    assert len(clients) == 1
    assert clients[0]._http.is_closed


def test_cli_push_repairs_only_output_after_partial_http_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem, cases = make_testcase_project(tmp_path / "project")
    remote = _TestcaseRemote(
        statement="local statement\n",
        fail_output_uploads=1,
    )
    clients = install_authenticated_transport(
        monkeypatch,
        httpx.MockTransport(remote),
    )
    monkeypatch.setenv("YUKICODER_TOKEN_42", TOKEN)
    monkeypatch.setattr(cli, "push", partial(cli.push, testcase_refresh_delay=0.0))

    first_code, first_stdout, first_stderr = invoke(["push", str(problem), "--testcases"])

    assert first_code == 1
    assert first_stdout == ""
    assert "testcase outputs [sample] failed" in first_stderr
    assert "completed items: problem 42 testcase inputs [sample]" in first_stderr
    assert remote.cases == {("in", "sample"): b"input"}

    second_code, second_stdout, second_stderr = invoke(["push", str(problem), "--testcases"])

    assert second_code == 0
    assert second_stderr == ""
    assert "完了: problem 42 testcase outputs [sample]" in second_stdout
    assert "testcase inputs" not in second_stdout
    assert remote.file_attempts == [
        ("upload", "in", ("sample",)),
        ("upload", "out", ("sample",)),
        ("upload", "out", ("sample",)),
    ]
    assert remote.cases == {
        ("in", "sample"): b"input",
        ("out", "sample"): b"output",
    }
    assert (cases / "sample.in").read_bytes() == b"input"
    assert (cases / "sample.diff").read_bytes() == b"output"
    rule_requests = [
        request for request in remote.requests if request.url.path == "/api/v1/testcase_name_rule"
    ]
    detail_requests = [
        request
        for request in remote.requests
        if request.url.path in {"/api/v1/problems/42/file/in", "/api/v1/problems/42/file/out"}
        and request.url.params.get("detail") == "1"
    ]
    body_downloads = [
        request for request in remote.requests if request.url.path.endswith("/sample")
    ]
    assert len(rule_requests) == 2
    assert len(detail_requests) >= 4
    assert body_downloads == []
    assert len(clients) == 2
    assert all(client._http.is_closed for client in clients)


def test_cli_push_prune_retries_only_remote_side_left_after_http_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem, _ = make_testcase_project(tmp_path / "project")
    remote = _TestcaseRemote(
        statement="local statement\n",
        cases={
            ("in", "sample"): b"input",
            ("out", "sample"): b"output",
            ("in", "stale"): b"stale-input",
            ("out", "stale"): b"stale-output",
        },
        fail_output_deletes=1,
    )
    clients = install_authenticated_transport(
        monkeypatch,
        httpx.MockTransport(remote),
    )
    monkeypatch.setenv("YUKICODER_TOKEN_42", TOKEN)
    monkeypatch.setattr(cli, "push", partial(cli.push, testcase_refresh_delay=0.0))

    first_code, first_stdout, first_stderr = invoke(
        ["push", str(problem), "--testcases", "--prune"]
    )

    assert first_code == 1
    assert first_stdout == ""
    assert "delete testcase output stale failed" in first_stderr
    assert "completed items: problem 42 delete testcase input stale" in first_stderr
    assert ("in", "stale") not in remote.cases
    assert remote.cases[("out", "stale")] == b"stale-output"

    second_code, second_stdout, second_stderr = invoke(
        ["push", str(problem), "--testcases", "--prune"]
    )

    assert second_code == 0
    assert second_stderr == ""
    assert "完了: problem 42 delete testcase output stale" in second_stdout
    assert "delete testcase input stale" not in second_stdout
    assert remote.file_attempts == [
        ("delete", "in", ("stale",)),
        ("delete", "out", ("stale",)),
        ("delete", "out", ("stale",)),
    ]
    assert remote.cases == {
        ("in", "sample"): b"input",
        ("out", "sample"): b"output",
    }
    assert len(clients) == 2
    assert all(client._http.is_closed for client in clients)
