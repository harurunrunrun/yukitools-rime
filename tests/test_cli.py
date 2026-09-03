from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import yukitools_rime.cli as cli
from yukitools_rime.api import Language
from yukitools_rime.errors import UsageError
from yukitools_rime.testcase_sync import SnapshotChanges


class Input(io.StringIO):
    def __init__(self, value: str = "", *, tty: bool = False) -> None:
        super().__init__(value)
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def streams(value: str = "", *, tty: bool = False) -> tuple[Input, io.StringIO, io.StringIO]:
    return Input(value, tty=tty), io.StringIO(), io.StringIO()


def test_empty_command_prints_help() -> None:
    stdin, stdout, stderr = streams()
    assert cli.main([], stdin=stdin, stdout=stdout, stderr=stderr) == 0
    assert "init" in stdout.getvalue()
    assert "languages" in stdout.getvalue()
    assert stderr.getvalue() == ""


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
    solution = parser.parse_args(["solution", "42", "a", "--summary", "accepted"])
    assert solution.submission_id == 42
    assert solution.summary == "accepted"
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["solution", "42"])
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
        lambda *args: SimpleNamespace(problem_id=1, submission_id=99),
    )
    stdin, stdout, stderr = streams()
    assert cli.main(["submit"], stdin=stdin, stdout=stdout, stderr=stderr) == 0
    assert "99" in stdout.getvalue()

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
