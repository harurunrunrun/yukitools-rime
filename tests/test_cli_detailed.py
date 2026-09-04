from __future__ import annotations

import argparse
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import yukitools_rime.cli as cli
from yukitools_rime.api import Language, ResponseFormatError, YukicoderAPIError
from yukitools_rime.auth import AuthError
from yukitools_rime.commands.push import PushInterrupted
from yukitools_rime.errors import UsageError
from yukitools_rime.layout import ProblemLayout
from yukitools_rime.models import ProblemConfig, ProblemSettings, ProjectConfig
from yukitools_rime.testcase_sync import SnapshotChanges


class Input(io.StringIO):
    def __init__(self, value: str = "", *, tty: bool = False) -> None:
        super().__init__(value)
        self.tty = tty
        self.read_calls = 0

    def isatty(self) -> bool:
        return self.tty

    def readline(self, *args: object, **kwargs: object) -> str:
        self.read_calls += 1
        return super().readline(*args, **kwargs)


def invoke(
    argv: list[str],
    *,
    input_text: str = "",
    tty: bool = False,
) -> tuple[int, Input, io.StringIO, io.StringIO]:
    stdin = Input(input_text, tty=tty)
    stdout = io.StringIO()
    stderr = io.StringIO()
    return cli.main(argv, stdin=stdin, stdout=stdout, stderr=stderr), stdin, stdout, stderr


def test_standard_output_is_reconfigured_to_utf8() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")

    cli._configure_standard_output(stream)
    cli._line(stream, "初期化しました: project with spaces 雪")
    stream.flush()

    assert stream.encoding.casefold() == "utf-8"
    assert raw.getvalue().decode("utf-8").splitlines() == ["初期化しました: project with spaces 雪"]


def test_non_reconfigurable_output_is_left_untouched() -> None:
    stream = io.StringIO()

    cli._configure_standard_output(stream)
    cli._line(stream, "日本語")

    assert stream.getvalue() == "日本語\n"


def test_detached_standard_output_is_ignored() -> None:
    stream = io.TextIOWrapper(io.BytesIO())
    stream.detach()

    cli._configure_standard_output(stream)


def pull_result(
    decisions: list[bool],
    confirm: Any,
    *,
    problem_ids: tuple[int, ...] = (1,),
) -> SimpleNamespace:
    problems: list[SimpleNamespace] = []
    for problem_id in problem_ids:
        changes = SnapshotChanges(
            added=(f"added-{problem_id}",),
            changed=(f"changed-{problem_id}",),
            removed=(f"removed-{problem_id}",),
        )
        accepted = confirm(SimpleNamespace(problem_id=problem_id), changes)
        decisions.append(accepted)
        problems.append(
            SimpleNamespace(
                problem_id=problem_id,
                path=Path(f"problem-{problem_id}"),
                changed_paths=(),
                testcase_changes=changes,
                testcases_applied=accepted,
                warnings=(f"problem {problem_id} warning",),
            )
        )
    return SimpleNamespace(problems=tuple(problems))


@pytest.mark.parametrize("answer", ["y\n", "YES\n"])
def test_pull_interactive_affirmative_answers_replace(
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    decisions: list[bool] = []
    monkeypatch.setattr(cli, "resolve_target", lambda _target: object())
    monkeypatch.setattr(
        cli,
        "pull",
        lambda _target, _factory, **kwargs: pull_result(decisions, kwargs["confirm"]),
    )

    code, stdin, stdout, stderr = invoke(
        ["pull", "--testcases"],
        input_text=answer,
        tty=True,
    )

    assert code == 0
    assert decisions == [True]
    assert stdin.read_calls == 1
    assert "(置換)" in stdout.getvalue()
    assert "problem 1 warning" in stderr.getvalue()
    assert "[y/N]" in stderr.getvalue()


@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_pull_interactive_negative_empty_and_eof_decline(
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    decisions: list[bool] = []
    monkeypatch.setattr(cli, "resolve_target", lambda _target: object())
    monkeypatch.setattr(
        cli,
        "pull",
        lambda _target, _factory, **kwargs: pull_result(decisions, kwargs["confirm"]),
    )

    code, stdin, stdout, stderr = invoke(
        ["pull", "--testcases"],
        input_text=answer,
        tty=True,
    )

    assert code == 0
    assert decisions == [False]
    assert stdin.read_calls == 1
    assert "(未変更)" in stdout.getvalue()
    assert "非対話環境" not in stderr.getvalue()


@pytest.mark.parametrize(
    ("arguments", "expected", "expected_code"),
    [
        (["pull", "--testcases", "--yes"], [True, True], 0),
        (["pull", "--testcases"], [False, False], 1),
    ],
)
def test_pull_multiple_problem_yes_and_noninteractive_modes(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    expected: list[bool],
    expected_code: int,
) -> None:
    decisions: list[bool] = []
    monkeypatch.setattr(cli, "resolve_target", lambda _target: object())
    monkeypatch.setattr(
        cli,
        "pull",
        lambda _target, _factory, **kwargs: pull_result(
            decisions,
            kwargs["confirm"],
            problem_ids=(1, 2),
        ),
    )

    code, stdin, stdout, stderr = invoke(arguments)

    assert code == expected_code
    assert decisions == expected
    assert stdin.read_calls == 0
    assert stdout.getvalue().count("テストケース:") == 2
    if expected_code == 1:
        assert stderr.getvalue().count("非対話環境") == 2


def test_client_pool_creates_discovers_and_closes_every_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[FakeClient] = []
    token_calls: list[tuple[Path, int]] = []

    class FakeClient:
        def __init__(self, token: str, base_url: str) -> None:
            self.token = token
            self.base_url = base_url
            self.closed = False
            created.append(self)

        def close(self) -> None:
            self.closed = True

    config = ProjectConfig(base_url="https://example.test/api")
    settings = ProblemSettings("title", "", 1, 1000, 256, "-", "0", False, False, 0, 0)
    problem = ProblemLayout(
        tmp_path / "problem",
        ProblemConfig(9, settings, "A"),
        None,
        (),
    )

    def token(root: Path, problem_id: int) -> str:
        token_calls.append((root, problem_id))
        return f"token-{problem_id}"

    monkeypatch.setattr(cli, "resolve_token", token)
    monkeypatch.setattr(cli, "YukicoderClient", FakeClient)
    monkeypatch.setattr(
        cli,
        "discover_project",
        lambda _path: SimpleNamespace(root=tmp_path, config=config),
    )
    pool = cli.ClientPool()

    first = pool.scaffold(tmp_path, config, 7)
    second = pool.problem(problem)
    pool.close()
    pool.close()

    assert first is created[0]
    assert second is created[1]
    assert token_calls == [(tmp_path, 7), (tmp_path, 9)]
    assert [client.token for client in created] == ["token-7", "token-9"]
    assert all(client.closed for client in created)
    assert pool._clients == []


def test_client_pool_wraps_invalid_connection_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "resolve_token", lambda _root, _problem_id: "token")

    def invalid_client(_token: str, _base_url: str) -> object:
        raise ValueError("invalid URL")

    monkeypatch.setattr(cli, "YukicoderClient", invalid_client)
    pool = cli.ClientPool()
    with pytest.raises(AuthError, match="API接続設定が不正"):
        pool.scaffold(tmp_path, ProjectConfig(), 1)
    assert pool._clients == []


def test_init_new_languages_and_base_url_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "init_project",
        lambda path: SimpleNamespace(root=Path(path).resolve()),
    )
    code, _, stdout, _ = invoke(["init", str(tmp_path)])
    assert code == 0
    assert "初期化しました" in stdout.getvalue()

    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "find_project_root", lambda path: Path(path).resolve())

    def fake_new(problem_id: int, root: Path, **kwargs: object) -> SimpleNamespace:
        captured.update(problem_id=problem_id, root=root, **kwargs)
        return SimpleNamespace(problem_id=problem_id, path=root / "custom")

    monkeypatch.setattr(cli, "new_problem", fake_new)
    code, _, stdout, _ = invoke(
        ["new", "42", "--project", str(tmp_path), "--dir", "custom", "--testcases"]
    )
    assert code == 0
    assert captured["problem_id"] == 42
    assert captured["dir_name"] == "custom"
    assert captured["include_testcases"] is True
    assert callable(captured["client_factory"])
    assert "問題 42" in stdout.getvalue()

    monkeypatch.setattr(
        cli,
        "discover_project",
        lambda _path: SimpleNamespace(config=ProjectConfig(base_url="https://configured.test/api")),
    )
    monkeypatch.setattr(
        cli,
        "list_languages",
        lambda **_kwargs: (
            Language("enabled", "Enabled", "1", "enable"),
            Language("disabled", "Disabled", "2", "disable"),
        ),
    )
    code, _, stdout, _ = invoke(["languages", "--include-disabled"])
    assert code == 0
    assert "disabled" in stdout.getvalue()
    assert "[disable]" in stdout.getvalue()
    assert cli._base_url() == "https://configured.test/api"

    def no_project(_path: object) -> object:
        raise UsageError("x")

    monkeypatch.setattr(cli, "discover_project", no_project)
    assert cli._base_url() == cli.DEFAULT_BASE_URL


def test_diff_push_query_submit_and_solution_output_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "resolve_target", lambda _target: object())
    monkeypatch.setattr(
        cli,
        "diff_remote",
        lambda *_args, **_kwargs: SimpleNamespace(
            lines=("difference",),
            warnings=("remote warning",),
            has_changes=True,
        ),
    )
    code, _, stdout, stderr = invoke(["diff", "--exit-code"])
    assert code == 3
    assert "difference" in stdout.getvalue()
    assert "remote warning" in stderr.getvalue()

    monkeypatch.setattr(
        cli,
        "push",
        lambda *_args, **_kwargs: SimpleNamespace(
            problems=(
                SimpleNamespace(
                    planned_items=("planned",),
                    completed_items=("completed",),
                ),
            ),
            dry_run=False,
            changed=True,
            warnings=(),
        ),
    )
    code, _, stdout, _ = invoke(["push"])
    assert code == 0
    assert stdout.getvalue() == "完了: completed\n"

    monkeypatch.setattr(
        cli,
        "push",
        lambda *_args, **_kwargs: SimpleNamespace(
            problems=(),
            dry_run=False,
            changed=False,
            warnings=(),
        ),
    )
    code, _, stdout, _ = invoke(["push"])
    assert code == 0
    assert stdout.getvalue() == "差分はありません。\n"

    monkeypatch.setattr(
        cli,
        "list_remote_testcases",
        lambda *_args, **_kwargs: (
            SimpleNamespace(problem_id=1, which=SimpleNamespace(value="out"), names=("sample",)),
        ),
    )
    code, _, stdout, _ = invoke(["testcases"])
    assert code == 0
    assert "問題 1 out:" in stdout.getvalue()
    assert "sample" in stdout.getvalue()

    monkeypatch.setattr(
        cli,
        "manage_expected_solution",
        lambda *_args, **_kwargs: SimpleNamespace(submission_id=99, deleted=False),
    )
    code, _, stdout, _ = invoke(["solution", "99", "--summary", "official"])
    assert code == 0
    assert "登録しました" in stdout.getvalue()


@pytest.mark.parametrize(
    ("error", "expected_code", "message"),
    [
        (PushInterrupted("testcase upload", ("settings",)), 130, "中断しました"),
        (KeyboardInterrupt(), 130, "中断しました"),
        (UsageError("bad arguments"), 2, "エラー: bad arguments"),
        (AuthError("missing token"), 1, "エラー: missing token"),
        (YukicoderAPIError("network failed"), 1, "エラー: network failed"),
        (ResponseFormatError("bad response"), 1, "エラー: bad response"),
        (BrokenPipeError(), 1, ""),
    ],
)
def test_main_maps_failures_to_stable_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected_code: int,
    message: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> int:
        raise error

    monkeypatch.setattr(cli, "_dispatch", fail)
    code, _, stdout, stderr = invoke(["init"])
    assert code == expected_code
    assert stdout.getvalue() == ""
    assert message in stderr.getvalue()


def test_main_uses_default_streams_and_handles_parser_without_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = Input()
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    monkeypatch.setattr(cli.sys, "stdout", stdout)
    monkeypatch.setattr(cli.sys, "stderr", stderr)
    monkeypatch.setattr(cli, "_dispatch", lambda *_args, **_kwargs: 0)
    assert cli.main(["init"]) == 0

    class Parser:
        def parse_args(self, _argv: object) -> argparse.Namespace:
            return argparse.Namespace(command=None)

        def print_help(self, stream: io.StringIO) -> None:
            stream.write("help\n")

    monkeypatch.setattr(cli, "build_parser", Parser)
    assert cli.main([]) == 0
    assert stdout.getvalue() == "help\n"


def test_unhandled_dispatch_closes_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pools: list[SimpleNamespace] = []

    def pool() -> SimpleNamespace:
        value = SimpleNamespace(problem=lambda _problem: object(), closed=False)

        def close() -> None:
            value.closed = True

        value.close = close
        pools.append(value)
        return value

    monkeypatch.setattr(cli, "ClientPool", pool)
    with pytest.raises(AssertionError, match="unhandled command"):
        cli._dispatch(
            argparse.Namespace(command="unknown"),
            stdin=Input(),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
    assert pools[0].closed


def test_nonnumeric_positive_integer_is_argparse_error() -> None:
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(["new", "not-an-integer"])
    assert caught.value.code == 2


def test_show_pull_without_testcase_changes() -> None:
    result = SimpleNamespace(
        problems=(
            SimpleNamespace(
                problem_id=1,
                path=Path("problem"),
                changed_paths=(Path("problem/PROBLEM"),),
                testcase_changes=None,
                testcases_applied=False,
                warnings=("warning",),
            ),
        )
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    cli._show_pull(result, stdout, stderr)

    assert f"更新: {Path('problem') / 'PROBLEM'}" in stdout.getvalue()
    assert "テストケース:" not in stdout.getvalue()
    assert stderr.getvalue() == "警告: warning\n"
