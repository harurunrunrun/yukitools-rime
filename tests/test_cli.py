from __future__ import annotations

import io
import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import yukitools_rime.cli as cli
from yukitools_rime.api import Language
from yukitools_rime.errors import UsageError, ValidationError
from yukitools_rime.testcase_sync import SnapshotChanges


class Input(io.StringIO):
    def __init__(self, value: str = "", *, tty: bool = False) -> None:
        super().__init__(value)
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def streams(value: str = "", *, tty: bool = False) -> tuple[Input, io.StringIO, io.StringIO]:
    return Input(value, tty=tty), io.StringIO(), io.StringIO()


def test_module_entrypoint_delegates_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "main", lambda: 17)

    with pytest.raises(SystemExit) as caught:
        runpy.run_module("yukitools_rime", run_name="__main__")

    assert caught.value.code == 17


def test_empty_command_is_an_argument_error() -> None:
    stdin, stdout, stderr = streams()
    with pytest.raises(SystemExit) as caught:
        cli.main([], stdin=stdin, stdout=stdout, stderr=stderr)
    assert caught.value.code == 2


def test_parser_exposes_exact_commands_and_options() -> None:
    parser = cli.build_parser()
    push = parser.parse_args(
        [
            "push",
            "problem-a",
            "--testcases",
            "--dry-run",
            "--prune",
            "--generate",
            "--no-wait-compile",
        ]
    )
    assert vars(push) == {
        "command": "push",
        "target": "problem-a",
        "testcases": True,
        "dry_run": True,
        "prune": True,
        "generate": True,
        "no_wait_compile": True,
    }
    submit = parser.parse_args(["submit", "solution", "--force", "--no-wait"])
    assert submit.solution == "solution"
    assert submit.force is True
    assert submit.no_wait is True
    solution = parser.parse_args(["solution", "42", "a", "--summary", "accepted"])
    assert solution.submission_id == 42
    assert solution.summary == "accepted"
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["solution", "42"])
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["new", "0"],
        ["new", "-1"],
        ["new", "1", "--dir", "../escape"],
        ["new", "1", "--dir", "nested/problem"],
        ["solution", "0", "--delete"],
        ["solution", "-1", "--delete"],
        ["solution", "1", "--summary", " \t "],
    ],
)
def test_invalid_cli_values_are_argparse_errors(argv: list[str]) -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(argv)
    assert caught.value.code == 2


def test_diff_exit_code_is_three_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "resolve_target", lambda target: object())
    monkeypatch.setattr(
        cli,
        "diff_remote",
        lambda *args, **kwargs: SimpleNamespace(
            lines=("settings.title: differs",),
            warnings=(),
            has_changes=True,
        ),
    )
    stdin, stdout, stderr = streams()
    assert cli.main(["diff"], stdin=stdin, stdout=stdout, stderr=stderr) == 0
    stdin, stdout, stderr = streams()
    assert (
        cli.main(
            ["diff", "--exit-code"],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        == 3
    )
    assert "differs" in stdout.getvalue()


def test_pull_noninteractive_declines_cases_but_returns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions: list[bool] = []

    def fake_pull(
        target: object,
        factory: object,
        *,
        include_testcases: bool,
        confirm: Any,
    ) -> object:
        decision = confirm(
            SimpleNamespace(problem_id=12),
            SnapshotChanges(added=("sample",)),
        )
        decisions.append(decision)
        problem = SimpleNamespace(
            problem_id=12,
            path=Path("a"),
            changed_paths=(Path("a/PROBLEM"),),
            testcase_changes=SnapshotChanges(added=("sample",)),
            testcases_applied=decision,
            warnings=(),
        )
        return SimpleNamespace(problems=(problem,))

    monkeypatch.setattr(cli, "resolve_target", lambda target: object())
    monkeypatch.setattr(cli, "pull", fake_pull)
    stdin, stdout, stderr = streams(tty=False)
    code = cli.main(
        ["pull", "--testcases"],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 1
    assert decisions == [False]
    assert "非対話環境" in stderr.getvalue()
    assert "PROBLEM" in stdout.getvalue()


def test_pull_yes_accepts_without_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    decisions: list[bool] = []

    def fake_pull(
        target: object,
        factory: object,
        *,
        include_testcases: bool,
        confirm: Any,
    ) -> object:
        decision = confirm(
            SimpleNamespace(problem_id=12),
            SnapshotChanges(changed=("sample",)),
        )
        decisions.append(decision)
        problem = SimpleNamespace(
            problem_id=12,
            path=Path("a"),
            changed_paths=(Path("a/rime-out/tests"),),
            testcase_changes=SnapshotChanges(changed=("sample",)),
            testcases_applied=decision,
            warnings=(),
        )
        return SimpleNamespace(problems=(problem,))

    monkeypatch.setattr(cli, "resolve_target", lambda target: object())
    monkeypatch.setattr(cli, "pull", fake_pull)
    stdin, stdout, stderr = streams(tty=False)
    assert (
        cli.main(
            ["pull", "--testcases", "--yes"],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        == 0
    )
    assert decisions == [True]
    assert "置換" in stdout.getvalue()


def test_push_options_are_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_push(target: object, factory: object, **kwargs: object) -> object:
        captured.update(kwargs)
        problem = SimpleNamespace(
            planned_items=("problem 1 settings/statement",),
            completed_items=(),
        )
        return SimpleNamespace(
            problems=(problem,),
            dry_run=True,
            changed=True,
            warnings=("warning",),
        )

    monkeypatch.setattr(cli, "resolve_target", lambda target: object())
    monkeypatch.setattr(cli, "push", fake_push)
    stdin, stdout, stderr = streams()
    code = cli.main(
        [
            "push",
            "--testcases",
            "--dry-run",
            "--prune",
            "--generate",
            "--no-wait-compile",
        ],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 0
    assert captured == {
        "include_testcases": True,
        "dry_run": True,
        "prune": True,
        "generate": True,
        "no_wait_compile": True,
    }
    assert "予定" in stdout.getvalue()
    assert "警告" in stderr.getvalue()


def test_languages_and_testcase_query_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "list_languages",
        lambda **kwargs: (Language("cpp17", "C++", "17"),),
    )
    stdin, stdout, stderr = streams()
    assert cli.main(["languages"], stdin=stdin, stdout=stdout, stderr=stderr) == 0
    assert "cpp17" in stdout.getvalue()

    monkeypatch.setattr(cli, "resolve_target", lambda target: object())
    monkeypatch.setattr(
        cli,
        "list_remote_testcases",
        lambda *args, **kwargs: (
            SimpleNamespace(problem_id=1, which=SimpleNamespace(value="in"), names=("a",)),
        ),
    )
    stdin, stdout, stderr = streams()
    assert (
        cli.main(
            ["testcases", "--which", "in"],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        == 0
    )
    assert "a" in stdout.getvalue()


def test_submit_and_expected_solution_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "resolve_target", lambda target: object())
    monkeypatch.setattr(
        cli,
        "submit_solution",
        lambda *args, **kwargs: SimpleNamespace(
            problem_id=1,
            submission_id=99,
            judge_status="AC",
            run_time_ms=42,
            wait_timed_out=False,
        ),
    )
    stdin, stdout, stderr = streams()
    assert cli.main(["submit"], stdin=stdin, stdout=stdout, stderr=stderr) == 0
    assert "99" in stdout.getvalue()
    assert "結果: AC (42 ms)" in stdout.getvalue()

    monkeypatch.setattr(
        cli,
        "manage_expected_solution",
        lambda *args, **kwargs: SimpleNamespace(submission_id=99, deleted=True),
    )
    stdin, stdout, stderr = streams()
    assert (
        cli.main(
            ["solution", "99", "--delete"],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        == 0
    )
    assert "解除" in stdout.getvalue()


def test_submit_reports_success_when_response_has_no_submission_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "resolve_target", lambda target: object())
    monkeypatch.setattr(
        cli,
        "submit_solution",
        lambda *args, **kwargs: SimpleNamespace(
            problem_id=1,
            submission_id=None,
            raw_response='Bearer top-secret {"accepted":true}',
        ),
    )
    stdin, stdout, stderr = streams()
    assert cli.main(["submit"], stdin=stdin, stdout=stdout, stderr=stderr) == 0
    output = stdout.getvalue()
    errors = stderr.getvalue()
    assert "提出しました: 問題 1" in output
    assert "提出ID" not in output
    assert "サーバーレスポンス" not in output
    assert "accepted" not in output + errors
    assert "top-secret" not in output + errors
    assert "提出IDをサーバー応答から判別できませんでした" in errors


def test_submit_wait_flag_and_timeout_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "resolve_target", lambda target: object())
    waits: list[bool] = []
    forces: list[bool] = []

    def fake_submit(*args: object, **kwargs: object) -> SimpleNamespace:
        waits.append(bool(kwargs["wait"]))
        forces.append(bool(kwargs["force"]))
        return SimpleNamespace(
            problem_id=1,
            submission_id=99,
            judge_status=None,
            run_time_ms=None,
            wait_timed_out=True,
        )

    monkeypatch.setattr(cli, "submit_solution", fake_submit)
    stdin, stdout, stderr = streams()
    assert cli.main(["submit"], stdin=stdin, stdout=stdout, stderr=stderr) == 0
    assert waits == [True]
    assert forces == [False]
    assert "10分待っても" in stderr.getvalue()

    stdin, stdout, stderr = streams()
    assert (
        cli.main(
            ["submit", "--force", "--no-wait"],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        == 0
    )
    assert waits == [True, False]
    assert forces == [False, True]


def test_submit_reports_existing_id_without_claiming_a_new_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "resolve_target", lambda target: object())
    captured: dict[str, object] = {}

    def fake_submit(*args: object, **kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            problem_id=1,
            submission_id=99,
            already_submitted=True,
        )

    monkeypatch.setattr(cli, "submit_solution", fake_submit)
    stdin, stdout, stderr = streams()

    assert cli.main(["submit"], stdin=stdin, stdout=stdout, stderr=stderr) == 0
    output = stdout.getvalue()
    assert captured["force"] is False
    assert "提出済みです: 問題 1, 提出ID 99" in output
    assert "https://yukicoder.me/submissions/99" in output
    assert "--force" in output
    assert "提出しました" not in output
    assert stderr.getvalue() == ""


def test_submit_reports_id_before_wait_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "resolve_target", lambda target: object())

    def fail_after_submit(*args: object, **kwargs: object) -> object:
        callback = kwargs["on_submitted"]
        callback(1, 77)  # type: ignore[operator]
        raise ValidationError("poll failed")

    monkeypatch.setattr(cli, "submit_solution", fail_after_submit)
    stdin, stdout, stderr = streams()

    assert cli.main(["submit"], stdin=stdin, stdout=stdout, stderr=stderr) == 1
    assert "提出ID 77" in stdout.getvalue()
    assert "https://yukicoder.me/submissions/77" in stdout.getvalue()
    assert "poll failed" in stderr.getvalue()


def test_submit_wait_interrupt_returns_130(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "resolve_target", lambda target: object())

    def interrupt(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "submit_solution", interrupt)
    stdin, stdout, stderr = streams()
    assert cli.main(["submit"], stdin=stdin, stdout=stdout, stderr=stderr) == 130
    assert "中断しました" in stderr.getvalue()


def test_expected_errors_and_interrupt_have_stable_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def usage_error(path: object) -> object:
        raise UsageError("bad combination")

    monkeypatch.setattr(cli, "init_project", usage_error)
    stdin, stdout, stderr = streams()
    assert cli.main(["init"], stdin=stdin, stdout=stdout, stderr=stderr) == 2
    assert "bad combination" in stderr.getvalue()

    def interrupted(path: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "init_project", interrupted)
    stdin, stdout, stderr = streams()
    assert cli.main(["init"], stdin=stdin, stdout=stdout, stderr=stderr) == 130
