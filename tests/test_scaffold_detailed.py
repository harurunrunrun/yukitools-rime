from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from yukitools_rime.api import GeneratorContent, JudgeCodeContent, ProblemEditContent
from yukitools_rime.commands import scaffold
from yukitools_rime.errors import (
    ConfigError,
    ConflictError,
    FileOperationError,
    LayoutError,
    ValidationError,
)
from yukitools_rime.models import ProblemSettings, ProjectConfig
from yukitools_rime.rime_config import render_project_block


def _settings() -> ProblemSettings:
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
        show_ans=False,
        enable_pure_judge=False,
        force_single_server_judge=False,
        allowed_langs=[],
    )


def _problem(problem_id: int = 42) -> ProblemEditContent:
    return ProblemEditContent(
        problem_id=problem_id,
        content="statement",
        is_markdown=True,
        showable=False,
        settings=_settings(),
    )


class _Client:
    def __init__(
        self,
        *,
        problem: object | None = None,
        generator: object | None = None,
        judge: object | None = None,
    ) -> None:
        self.problem = _problem() if problem is None else problem
        self.generator = GeneratorContent() if generator is None else generator
        self.judge = judge
        self.closed = False

    def get_problem_edit(self, problem_id: int) -> ProblemEditContent:
        return cast(ProblemEditContent, self.problem)

    def get_generator(self, problem_id: int) -> GeneratorContent:
        return cast(GeneratorContent, self.generator)

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent | None:
        return cast(JudgeCodeContent | None, self.judge)

    def close(self) -> None:
        self.closed = True


def _factory(client: _Client) -> scaffold.ClientFactory:
    def create(root: Path, config: ProjectConfig, problem_id: int) -> _Client:
        return client

    return create


def _project(tmp_path: Path, config: ProjectConfig | None = None) -> Path:
    root = tmp_path / "project"
    if config is None:
        scaffold.init_project(root)
    else:
        root.mkdir()
        (root / "PROJECT").write_text(render_project_block(config), encoding="utf-8")
    return root


def test_default_factory_uses_resolved_token_and_project_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []
    sentinel = object()

    monkeypatch.setattr(scaffold, "resolve_token", lambda root, problem_id: "secret")

    def construct(token: str, base_url: str) -> object:
        seen.append((token, base_url))
        return sentinel

    monkeypatch.setattr(scaffold, "YukicoderClient", construct)
    config = ProjectConfig(base_url="https://example.invalid/api")

    assert scaffold.default_client_factory(tmp_path, config, 42) is sentinel
    assert seen == [("secret", "https://example.invalid/api")]


def test_optional_file_guards_symlinks_directories_and_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert scaffold._read_optional_file(missing) == ""
    scaffold._preflight_optional_file(missing)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(FileOperationError, match="regular file"):
        scaffold._read_optional_file(directory)
    with pytest.raises(FileOperationError, match="regular file"):
        scaffold._preflight_optional_file(directory)

    target = tmp_path / "target"
    target.write_text("value", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(FileOperationError, match="symlink"):
        scaffold._read_optional_file(link)
    with pytest.raises(FileOperationError, match="regular file"):
        scaffold._preflight_optional_file(link)


def test_output_validation_wraps_iteration_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_iterdir = Path.iterdir

    def denied(path: Path) -> Any:
        if path == tmp_path:
            raise PermissionError("inspection denied")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(FileOperationError, match="inspection denied"):
        scaffold._validate_existing_output_directories(tmp_path, ProjectConfig())


def test_output_validation_exercises_skips_and_rejects_symlink_problem(
    tmp_path: Path,
) -> None:
    (tmp_path / "ordinary").write_text("x", encoding="utf-8")
    (tmp_path / "plain-directory").mkdir()
    broken = tmp_path / "broken-link"
    broken.symlink_to(tmp_path / "absent", target_is_directory=True)

    source_problem = tmp_path / "source-problem"
    source_problem.mkdir()
    (source_problem / "PROBLEM").write_text("problem(lambda: None)\n", encoding="utf-8")
    linked_problem = tmp_path / "linked-problem"
    linked_problem.symlink_to(source_problem, target_is_directory=True)

    with pytest.raises(ConfigError, match="symlink Rime problem"):
        scaffold._validate_existing_output_directories(tmp_path, ProjectConfig())


def test_output_validation_skips_symlink_problem_file(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.write_text("problem(lambda: None)\n", encoding="utf-8")
    problem = tmp_path / "problem"
    problem.mkdir()
    (problem / "PROBLEM").symlink_to(external)

    scaffold._validate_existing_output_directories(tmp_path, ProjectConfig())


@pytest.mark.parametrize("kind", ["file", "symlink", "collision"])
def test_output_validation_rejects_unsafe_output(
    tmp_path: Path,
    kind: str,
) -> None:
    problem = tmp_path / "problem"
    problem.mkdir()
    (problem / "PROBLEM").write_text("problem(lambda: None)\n", encoding="utf-8")
    output = problem / "rime-out"
    if kind == "file":
        output.write_text("not a directory", encoding="utf-8")
    elif kind == "symlink":
        actual = tmp_path / "actual-output"
        actual.mkdir()
        output.symlink_to(actual, target_is_directory=True)
    else:
        output.mkdir()
        (output / "TESTSET").write_text("collision", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"unsafe|collides"):
        scaffold._validate_existing_output_directories(tmp_path, ProjectConfig())


def test_restore_init_files_keeps_first_rollback_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = tmp_path / "removed"
    restored = tmp_path / "restored"

    def fail_remove(path: Path, *, missing_ok: bool) -> None:
        raise FileOperationError("first rollback error")

    def fail_write(path: Path, data: bytes) -> None:
        raise FileOperationError("second rollback error")

    monkeypatch.setattr(scaffold, "remove_file", fail_remove)
    monkeypatch.setattr(scaffold, "atomic_write_bytes", fail_write)
    error = scaffold._restore_init_files([(restored, b"old"), (removed, None)])

    assert error is not None
    assert "first rollback error" in str(error)


def test_init_reports_rollback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()

    def fail_write(path: Path, source: str) -> None:
        raise ConfigError("write failed")

    monkeypatch.setattr(scaffold, "write_config_atomic", fail_write)
    monkeypatch.setattr(
        scaffold,
        "_restore_init_files",
        lambda backups: FileOperationError("restore failed"),
    )

    with pytest.raises(FileOperationError, match="rollback also failed"):
        scaffold._apply_init_updates(root, ((root / "PROJECT", "source"),))


def test_cleanup_created_project_rejects_symlink_and_wraps_rmtree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileOperationError, match="unexpected symlink"):
        scaffold._cleanup_created_project(link, RuntimeError("initial"))

    def denied(path: Path) -> None:
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(shutil, "rmtree", denied)
    with pytest.raises(FileOperationError, match="cleanup denied"):
        scaffold._cleanup_created_project(target, RuntimeError("initial"))


@pytest.mark.parametrize("existing", [False, True])
def test_init_wraps_mkdir_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    requested = tmp_path / "project"
    if existing:
        requested.mkdir()
    real_mkdir = Path.mkdir

    def denied(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path == requested:
            raise PermissionError("mkdir denied")
        real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(FileOperationError, match="mkdir denied"):
        scaffold.init_project(requested)

    assert requested.exists() is existing


def test_init_existing_nondirectory_reports_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = tmp_path / "project"
    requested.write_text("file", encoding="utf-8")

    def no_op_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        return None

    monkeypatch.setattr(Path, "mkdir", no_op_mkdir)
    with pytest.raises(FileOperationError, match="not a directory"):
        scaffold.init_project(requested)


@pytest.mark.parametrize(
    ("client", "message"),
    [
        (_Client(problem=object()), "problem API"),
        (
            _Client(
                problem=ProblemEditContent(
                    problem_id=42,
                    content="x",
                    is_markdown=True,
                    showable=False,
                    settings=cast(ProblemSettings, object()),
                )
            ),
            "invalid settings",
        ),
        (_Client(generator=object()), "generator API"),
        (_Client(judge=object()), "judge API"),
    ],
)
def test_problem_resources_rejects_invalid_remote_types(
    tmp_path: Path,
    client: _Client,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        scaffold._problem_resources(
            42,
            "problem",
            client,
            include_testcases=False,
            testcase_stage=None,
        )


def test_problem_resources_requires_testcase_stage() -> None:
    with pytest.raises(ValueError, match="staging"):
        scaffold._problem_resources(
            42,
            "problem",
            _Client(),
            include_testcases=True,
            testcase_stage=None,
        )


def test_empty_judge_is_preserved_as_absent() -> None:
    resources = scaffold._problem_resources(
        42,
        "problem",
        _Client(judge=JudgeCodeContent(lang_id="cpp", source=" \n", status="AC")),
        include_testcases=False,
        testcase_stage=None,
    )
    assert resources[4:6] == (None, None)


def test_close_client_accepts_missing_and_noncallable_close() -> None:
    scaffold._close_client(object())
    scaffold._close_client(SimpleNamespace(close=1))


def test_init_rejects_project_path_symlink(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(FileOperationError, match="symlink"):
        scaffold.init_project(linked)


@pytest.mark.parametrize("problem_id", [True, False, 0, -1, "42"])
def test_new_rejects_invalid_problem_ids_without_loading_project(
    problem_id: object,
) -> None:
    with pytest.raises(ValidationError, match="positive integer"):
        scaffold.new_problem(cast(Any, problem_id), "absent")


def test_new_rejects_broken_symlink_target(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "target").symlink_to(root / "missing", target_is_directory=True)

    with pytest.raises(ConflictError, match="already exists"):
        scaffold.new_problem(42, root, dir_name="target", client_factory=_factory(_Client()))


def test_output_validation_skips_nonproblem_symlink(tmp_path: Path) -> None:
    broken = tmp_path / "broken-link"
    broken.symlink_to(tmp_path / "absent", target_is_directory=True)

    scaffold._validate_existing_output_directories(tmp_path, ProjectConfig())


@pytest.mark.parametrize("failure_type", [OSError, LayoutError])
def test_new_stage_failure_is_wrapped_and_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
) -> None:
    root = _project(tmp_path)

    def fail_stage(*args: object, **kwargs: object) -> None:
        raise failure_type("stage failed")

    monkeypatch.setattr(scaffold, "_write_problem_stage", fail_stage)
    with pytest.raises(FileOperationError, match="stage failed"):
        scaffold.new_problem(42, root, dir_name="failed", client_factory=_factory(_Client()))

    assert not (root / "failed").exists()
    assert not list(root.glob(".failed.stage-*"))


def test_new_mkdtemp_failure_is_wrapped_without_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)

    def denied(*args: object, **kwargs: object) -> str:
        raise PermissionError("mkdtemp denied")

    monkeypatch.setattr(tempfile, "mkdtemp", denied)
    with pytest.raises(FileOperationError, match="mkdtemp denied"):
        scaffold.new_problem(42, root, client_factory=_factory(_Client()))

    assert sorted(path.name for path in root.iterdir()) == [".env.example", ".gitignore", "PROJECT"]


def test_new_publish_conflict_cleans_complete_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    real_write_stage = scaffold._write_problem_stage

    def create_conflict(*args: Any, **kwargs: Any) -> None:
        real_write_stage(*args, **kwargs)
        (root / "late").mkdir()

    monkeypatch.setattr(scaffold, "_write_problem_stage", create_conflict)
    with pytest.raises(ConflictError, match="appeared during creation"):
        scaffold.new_problem(42, root, dir_name="late", client_factory=_factory(_Client()))

    assert (root / "late").is_dir()
    assert not list(root.glob(".late.stage-*"))


def test_new_replace_and_keyboard_failures_clean_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    real_replace = os.replace

    def denied(source: object, destination: object) -> None:
        if Path(cast(Any, destination)) == root / "replace-fail":
            raise PermissionError("publish denied")
        real_replace(cast(Any, source), cast(Any, destination))

    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(FileOperationError, match="publish denied"):
        scaffold.new_problem(
            42,
            root,
            dir_name="replace-fail",
            client_factory=_factory(_Client()),
        )
    assert not list(root.glob(".replace-fail.stage-*"))

    monkeypatch.undo()
    root = _project(tmp_path / "second")

    def interrupt_stage(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(scaffold, "_write_problem_stage", interrupt_stage)
    with pytest.raises(KeyboardInterrupt):
        scaffold.new_problem(
            42,
            root,
            dir_name="interrupt",
            client_factory=_factory(_Client()),
        )
    assert not list(root.glob(".interrupt.stage-*"))
