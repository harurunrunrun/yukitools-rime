"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO, cast

from yukitools_rime import __version__
from yukitools_rime.api import (
    DEFAULT_BASE_URL,
    ResponseFormatError,
    YukicoderAPIError,
    YukicoderClient,
)
from yukitools_rime.auth import AuthError, resolve_token
from yukitools_rime.commands.push import (
    ClientFactory as PushClientFactory,
)
from yukitools_rime.commands.push import PushInterrupted, push
from yukitools_rime.commands.query import (
    ProblemClientFactory as QueryClientFactory,
)
from yukitools_rime.commands.query import (
    list_languages,
    list_remote_testcases,
)
from yukitools_rime.commands.scaffold import init_project, new_problem
from yukitools_rime.commands.submission import (
    SubmissionClientFactory,
    manage_expected_solution,
    submit_solution,
)
from yukitools_rime.commands.sync import (
    ClientFactory as SyncClientFactory,
)
from yukitools_rime.commands.sync import (
    ConfirmTestcases,
    PullResult,
    diff_remote,
    pull,
)
from yukitools_rime.errors import AppError
from yukitools_rime.layout import (
    ProblemLayout,
    discover_project,
    find_project_root,
    resolve_target,
)
from yukitools_rime.models import ProjectConfig, Which, validate_basename
from yukitools_rime.testcase_sync import SnapshotChanges


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("1以上の整数で指定してください") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("1以上の整数で指定してください")
    return parsed


def _direct_name(value: str) -> str:
    try:
        return validate_basename(value, label="--dir")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("直下のディレクトリ名を1要素で指定してください") from exc


def _nonempty_text(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("空白だけの値は指定できません")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yukitools-rime",
        description="Rimeプロジェクトからyukicoderの問題を管理する",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    command = commands.add_parser("init", help="Rimeプロジェクトを初期化する")
    command.add_argument("project", nargs="?", default=".", metavar="PROJECT")

    command = commands.add_parser("new", help="新しいRime問題を取得する")
    command.add_argument("problem_id", type=_positive_int, metavar="問題ID")
    command.add_argument("--project", metavar="PATH")
    command.add_argument("--dir", dest="dir_name", type=_direct_name, metavar="NAME")
    command.add_argument("--testcases", action="store_true")

    command = commands.add_parser("pull", help="サーバーの内容をローカルへ反映する")
    command.add_argument("target", nargs="?", default=".", metavar="TARGET")
    command.add_argument("--testcases", action="store_true")
    command.add_argument("--yes", action="store_true")

    command = commands.add_parser("diff", help="サーバーからローカル方向の差分を表示する")
    command.add_argument("target", nargs="?", default=".", metavar="TARGET")
    command.add_argument("--testcases", action="store_true")
    command.add_argument("--exit-code", action="store_true")

    command = commands.add_parser("push", help="ローカルの変更をサーバーへ反映する")
    command.add_argument("target", nargs="?", default=".", metavar="TARGET")
    command.add_argument("--testcases", action="store_true")
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--prune", action="store_true")
    command.add_argument("--generate", action="store_true")
    command.add_argument("--no-wait-compile", action="store_true")

    command = commands.add_parser("submit", help="RimeのSOLUTIONを提出する")
    command.add_argument("solution", nargs="?", default=".", metavar="SOLUTION")

    command = commands.add_parser("solution", help="提出を想定解として登録または解除する")
    command.add_argument("submission_id", type=_positive_int, metavar="提出ID")
    command.add_argument("target", nargs="?", default=".", metavar="TARGET")
    mode = command.add_mutually_exclusive_group(required=True)
    mode.add_argument("--summary", type=_nonempty_text, metavar="TEXT")
    mode.add_argument("--delete", action="store_true")

    command = commands.add_parser("testcases", help="サーバー側のケース名だけを表示する")
    command.add_argument("target", nargs="?", default=".", metavar="TARGET")
    command.add_argument("--which", choices=("in", "out"))

    command = commands.add_parser("languages", help="利用可能な言語IDを表示する")
    command.add_argument("--include-disabled", action="store_true")
    return parser


class ClientPool:
    """Own all authenticated clients created for command services."""

    def __init__(self) -> None:
        self._clients: list[YukicoderClient] = []

    def _new(self, root: Path, config: ProjectConfig, problem_id: int) -> YukicoderClient:
        token = resolve_token(root, problem_id)
        try:
            client = YukicoderClient(token, config.base_url)
        except ValueError as exc:
            raise AuthError(f"API接続設定が不正です: {exc}") from exc
        self._clients.append(client)
        return client

    def scaffold(self, root: Path, config: ProjectConfig, problem_id: int) -> YukicoderClient:
        return self._new(root, config, problem_id)

    def problem(self, problem: ProblemLayout) -> YukicoderClient:
        project = discover_project(problem.path)
        return self._new(project.root, project.config, problem.problem_id)

    def close(self) -> None:
        for client in self._clients:
            client.close()
        self._clients.clear()


def _line(stream: TextIO, value: str = "") -> None:
    stream.write(value + "\n")


def _confirm_testcases(
    assume_yes: bool,
    stdin: TextIO,
    stderr: TextIO,
    noninteractive: list[bool],
) -> ConfirmTestcases:
    def confirm(problem: ProblemLayout, changes: SnapshotChanges) -> bool:
        added = changes.added_count
        changed = changes.changed_count
        removed = changes.removed_count
        _line(
            stderr,
            f"警告: 問題 {problem.problem_id} のテストケースがrime-outと異なります"
            f" (追加 {added}, 変更 {changed}, 削除 {removed})。",
        )
        if assume_yes:
            return True
        if not stdin.isatty():
            noninteractive[0] = True
            _line(stderr, "警告: 非対話環境のため置換しません。--yesで承認できます。")
            return False
        stderr.write("remoteの完全なスナップショットで置換しますか? [y/N] ")
        stderr.flush()
        return stdin.readline().strip().lower() in {"y", "yes"}

    return cast(ConfirmTestcases, confirm)


def _base_url() -> str:
    try:
        return discover_project(".").config.base_url
    except AppError:
        return DEFAULT_BASE_URL


def _show_pull(result: PullResult, stdout: TextIO, stderr: TextIO) -> None:
    for problem in result.problems:
        _line(stdout, f"問題 {problem.problem_id}: {problem.path}")
        for path in problem.changed_paths:
            _line(stdout, f"  更新: {path}")
        changes = problem.testcase_changes
        if changes is not None and changes.has_changes:
            status = "置換" if problem.testcases_applied else "未変更"
            _line(
                stdout,
                f"  テストケース: 追加 {changes.added_count}, "
                f"変更 {changes.changed_count}, 削除 {changes.removed_count} ({status})",
            )
        for warning in problem.warnings:
            _line(stderr, f"警告: {warning}")


def _dispatch(
    args: argparse.Namespace,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if args.command == "init":
        project = init_project(args.project)
        _line(stdout, f"初期化しました: {project.root}")
        return 0

    if args.command == "languages":
        languages = list_languages(
            include_disabled=args.include_disabled,
            base_url=_base_url(),
        )
        for language in languages:
            enabled = language.status in {"", "enable"}
            suffix = "" if enabled else f" [{language.status}]"
            _line(stdout, f"{language.id:<24} {language.name} ({language.ver}){suffix}")
        return 0

    pool = ClientPool()
    try:
        problem_factory = pool.problem
        if args.command == "new":
            root = find_project_root(args.project or ".")
            problem = new_problem(
                args.problem_id,
                root,
                dir_name=args.dir_name,
                include_testcases=args.testcases,
                client_factory=pool.scaffold,
            )
            _line(stdout, f"問題 {problem.problem_id} を作成しました: {problem.path}")
            return 0

        if args.command == "pull":
            selection = resolve_target(args.target)
            noninteractive = [False]
            pull_result = pull(
                selection,
                cast(SyncClientFactory, problem_factory),
                include_testcases=args.testcases,
                confirm=_confirm_testcases(args.yes, stdin, stderr, noninteractive),
            )
            _show_pull(pull_result, stdout, stderr)
            return 1 if noninteractive[0] else 0

        if args.command == "diff":
            selection = resolve_target(args.target)
            diff_result = diff_remote(
                selection,
                cast(SyncClientFactory, problem_factory),
                include_testcases=args.testcases,
            )
            for line in diff_result.lines:
                _line(stdout, line)
            for warning in diff_result.warnings:
                _line(stderr, f"警告: {warning}")
            return 3 if args.exit_code and diff_result.has_changes else 0

        if args.command == "push":
            selection = resolve_target(args.target)
            push_result = push(
                selection,
                cast(PushClientFactory, problem_factory),
                include_testcases=args.testcases,
                dry_run=args.dry_run,
                prune=args.prune,
                generate=args.generate,
                no_wait_compile=args.no_wait_compile,
            )
            for problem_result in push_result.problems:
                items = (
                    problem_result.planned_items
                    if push_result.dry_run
                    else problem_result.completed_items
                )
                for item in items:
                    status = "予定" if push_result.dry_run else "完了"
                    _line(stdout, f"{status}: {item}")
            if not push_result.changed:
                _line(stdout, "差分はありません。")
            for warning in push_result.warnings:
                _line(stderr, f"警告: {warning}")
            return 0

        if args.command == "testcases":
            selection = resolve_target(args.target)
            which = Which(args.which) if args.which is not None else None
            results = list_remote_testcases(
                selection,
                cast(QueryClientFactory, problem_factory),
                which=which,
            )
            for testcase_result in results:
                _line(
                    stdout,
                    f"問題 {testcase_result.problem_id} {testcase_result.which.value}:",
                )
                for name in testcase_result.names:
                    _line(stdout, f"  {name}")
            return 0

        if args.command == "submit":
            submit_result = submit_solution(
                resolve_target(args.solution),
                cast(SubmissionClientFactory, problem_factory),
            )
            if submit_result.submission_id is None:
                _line(stdout, f"提出しました: 問題 {submit_result.problem_id}")
                _line(stderr, "警告: 提出IDをサーバー応答から判別できませんでした。")
            else:
                _line(
                    stdout,
                    f"提出しました: 問題 {submit_result.problem_id}, "
                    f"提出ID {submit_result.submission_id}",
                )
            return 0

        if args.command == "solution":
            solution_result = manage_expected_solution(
                args.submission_id,
                resolve_target(args.target),
                cast(SubmissionClientFactory, problem_factory),
                summary=args.summary,
                delete=args.delete,
            )
            verb = "解除" if solution_result.deleted else "登録"
            _line(stdout, f"提出 {solution_result.submission_id} を想定解として{verb}しました。")
            return 0
    finally:
        pool.close()
    raise AssertionError(f"unhandled command: {args.command}")


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    input_stream = sys.stdin if stdin is None else stdin
    if args.command is None:
        parser.print_help(output)
        return 0
    try:
        return _dispatch(args, stdin=input_stream, stdout=output, stderr=errors)
    except PushInterrupted as exc:
        _line(errors, f"中断しました。{exc}")
        return 130
    except KeyboardInterrupt:
        _line(errors, "中断しました。")
        return 130
    except AppError as exc:
        _line(errors, f"エラー: {exc}")
        return exc.exit_code
    except (AuthError, YukicoderAPIError, ResponseFormatError) as exc:
        _line(errors, f"エラー: {exc}")
        return 1
    except BrokenPipeError:
        return 1
