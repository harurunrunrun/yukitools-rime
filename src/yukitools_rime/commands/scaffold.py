"""Project initialization and transactional new-problem scaffolding."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from yukitools_rime.api import (
    GeneratorContent,
    JudgeCodeContent,
    ProblemEditContent,
    YukicoderClient,
)
from yukitools_rime.auth import resolve_token
from yukitools_rime.errors import (
    ConfigError,
    ConflictError,
    FileOperationError,
    LayoutError,
    ValidationError,
)
from yukitools_rime.files import atomic_write_text, read_text_verbatim, safe_child
from yukitools_rime.layout import ProblemLayout, ProjectLayout, load_problem, load_project
from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    Statement,
)
from yukitools_rime.rime_config import (
    BEGIN_MARKER,
    END_MARKER,
    TestsetConfig,
    contains_declaration,
    extract_managed_block,
    parse_project_config,
    render_problem_block,
    render_project_block,
    render_testset_block,
    upsert_managed_block,
    upsert_managed_block_at_end,
    write_config_atomic,
)
from yukitools_rime.testcase_sync import (
    TestcaseAPI,
    TestcaseSnapshot,
    fetch_remote_snapshot_to,
    replace_local_snapshot,
)

_RESERVED_PROBLEM_DIRECTORY_NAMES = {".env"}

_ENV_EXAMPLE = """# Copy this file to .env and add one or more credentials.
# Never commit real tokens.
#
# Resolution order:
#   YUKICODER_TOKEN_<problem id>
#   YUKICODER_TOKEN
#   YUKICODER_API_KEY
# For the same key, the process environment overrides .env.

# YUKICODER_TOKEN_12345=ypt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# YUKICODER_TOKEN=ypt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# YUKICODER_API_KEY=xxxxxxxxxxxxxxxxxxxx
"""


def _gitignore_block(config: ProjectConfig) -> str:
    return f"{BEGIN_MARKER}\n/.env\n/*/{config.rime_out_dir}/\n{END_MARKER}\n"


class ScaffoldClient(Protocol):
    """Remote operations needed before a problem directory can be published."""

    def get_problem_edit(self, problem_id: int) -> ProblemEditContent: ...

    def get_generator(self, problem_id: int) -> GeneratorContent: ...

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent | None: ...


ClientFactory = Callable[[Path, ProjectConfig, int], ScaffoldClient]


def default_client_factory(
    project_root: Path, project_config: ProjectConfig, problem_id: int
) -> YukicoderClient:
    """Build an authenticated client without exposing its token to the caller."""

    return YukicoderClient(
        resolve_token(project_root, problem_id),
        project_config.base_url,
    )


def _read_optional_file(path: Path) -> str:
    if path.is_symlink():
        raise FileOperationError(f"refusing to use symlink: {path}")
    if not path.exists():
        return ""
    if not path.is_file():
        raise FileOperationError(f"not a regular file: {path}")
    return read_text_verbatim(path)


def _preflight_optional_file(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise FileOperationError(f"not a regular file: {path}")


def _validate_existing_output_directories(
    root: Path,
    project_config: ProjectConfig,
) -> None:
    try:
        children = tuple(root.iterdir())
    except OSError as exc:
        raise FileOperationError(f"could not inspect project directory {root}: {exc}") from exc
    for child in children:
        problem_file = child / "PROBLEM"
        if child.is_symlink():
            if child.is_dir() and problem_file.is_file():
                raise ConfigError(f"symlink Rime problem is not allowed: {child}")
            continue
        if not child.is_dir():
            continue
        if not problem_file.is_file() or problem_file.is_symlink():
            continue
        if child.name.casefold() in _RESERVED_PROBLEM_DIRECTORY_NAMES:
            raise ConfigError(
                f"Rime problem directory name is reserved by yukitools-rime: {child.name}"
            )
        output = child / project_config.rime_out_dir
        if output.is_symlink() or (output.exists() and not output.is_dir()):
            raise ConfigError(f"unsafe Rime output path: {output}")
        for config_name in ("PROBLEM", "TESTSET", "SOLUTION"):
            config_path = output / config_name
            if config_path.exists() or config_path.is_symlink():
                raise ConfigError(f"rime_out_dir collides with a Rime source directory: {output}")


def init_project(path: str | Path) -> ProjectLayout:
    """Create or augment a Rime project without invoking Git or any network API."""

    requested = Path(path)
    if requested.is_symlink():
        raise FileOperationError(f"project path must not be a symlink: {requested}")
    try:
        requested.mkdir(parents=True, exist_ok=True)
        root = requested.resolve(strict=True)
    except OSError as exc:
        raise FileOperationError(f"could not create project directory {requested}: {exc}") from exc
    if not root.is_dir():
        raise FileOperationError(f"project path is not a directory: {root}")

    project_path = root / "PROJECT"
    ignore_path = root / ".gitignore"
    env_example_path = root / ".env.example"
    for candidate in (project_path, ignore_path, env_example_path):
        _preflight_optional_file(candidate)

    project_source = _read_optional_file(project_path)
    if extract_managed_block(project_source) is None:
        if contains_declaration(project_source, "yukicoder_project"):
            raise ConfigError("yukicoder_project() exists outside a managed configuration block")
        project_config = ProjectConfig()
    else:
        project_config = parse_project_config(project_source)
    _validate_existing_output_directories(root, project_config)
    updated_project = upsert_managed_block_at_end(
        project_source,
        render_project_block(project_config),
    )

    ignore_source = _read_optional_file(ignore_path)
    updated_ignore = upsert_managed_block(
        ignore_source, _gitignore_block(project_config), python_source=False
    )

    if updated_project != project_source:
        write_config_atomic(project_path, updated_project)
    if updated_ignore != ignore_source:
        write_config_atomic(ignore_path, updated_ignore)
    if not env_example_path.exists():
        write_config_atomic(env_example_path, _ENV_EXAMPLE)
    return load_project(root)


@dataclass(frozen=True, slots=True)
class _SourceSpec:
    extension: str
    rime_kind: str | None


def _source_spec(lang_id: str) -> _SourceSpec:
    """Map stable yukicoder language-id families to Rime declarations."""

    language = lang_id.strip().lower()
    if language.startswith("cpp"):
        return _SourceSpec("cpp", "cxx")
    if language == "c" or re.match(r"c\d", language):
        return _SourceSpec("c", "c")
    if language.startswith("kotlin"):
        return _SourceSpec("kt", "kotlin")
    if language.startswith("java"):
        return _SourceSpec("java", "java")
    if language.startswith("rust"):
        return _SourceSpec("rs", "rust")
    if language.startswith(("go", "golang")):
        return _SourceSpec("go", "go")
    if language.startswith(("python", "pypy")):
        return _SourceSpec("py", "script")
    if language.startswith("ruby"):
        return _SourceSpec("rb", "script")
    if language.startswith("perl"):
        return _SourceSpec("pl", "script")
    if language.startswith(("bash", "sh")):
        return _SourceSpec("sh", "script")
    return _SourceSpec("txt", None)


def _problem_resources(
    problem_id: int,
    directory_name: str,
    client: ScaffoldClient,
    *,
    include_testcases: bool,
    testcase_stage: Path | None,
) -> tuple[
    ProblemConfig,
    Statement,
    GeneratorConfig | None,
    str | None,
    JudgeConfig | None,
    str | None,
    TestcaseSnapshot | None,
]:
    remote_problem = client.get_problem_edit(problem_id)
    if not isinstance(remote_problem, ProblemEditContent):
        raise ValidationError("problem API returned an unexpected response type")
    if remote_problem.problem_id != problem_id:
        raise ValidationError(
            f"requested problem {problem_id}, but API returned {remote_problem.problem_id}"
        )
    if not isinstance(remote_problem.settings, ProblemSettings):
        raise ValidationError("problem API returned invalid settings")
    problem = ProblemConfig(
        problem_id=problem_id,
        settings=remote_problem.settings,
        rime_id=directory_name,
    )
    statement = Statement.from_remote(
        remote_problem.content,
        remote_problem.is_markdown,
    )

    remote_generator = client.get_generator(problem_id)
    if not isinstance(remote_generator, GeneratorContent):
        raise ValidationError("generator API returned an unexpected response type")
    generator: GeneratorConfig | None = None
    generator_source: str | None = None
    if remote_generator.source.strip():
        spec = _source_spec(remote_generator.lang_id)
        generator = GeneratorConfig(
            lang_id=remote_generator.lang_id,
            src=f"generator.{spec.extension}",
            test_case_num=remote_generator.test_case_num,
            prefix=None,
            rime_kind=spec.rime_kind,
        )
        generator_source = remote_generator.source

    remote_judge = client.get_judge_code(problem_id)
    judge: JudgeConfig | None = None
    judge_source: str | None = None
    if remote_judge is not None:
        if not isinstance(remote_judge, JudgeCodeContent):
            raise ValidationError("judge API returned an unexpected response type")
        if remote_judge.source.strip():
            spec = _source_spec(remote_judge.lang_id)
            judge = JudgeConfig(
                lang_id=remote_judge.lang_id,
                src=f"judge.{spec.extension}",
                rime_kind=spec.rime_kind,
            )
            judge_source = remote_judge.source

    if include_testcases:
        if testcase_stage is None:
            raise ValueError("testcase staging is required when fetching testcases")
        testcases = fetch_remote_snapshot_to(cast(TestcaseAPI, client), problem_id, testcase_stage)
    else:
        testcases = None
    return (
        problem,
        statement,
        generator,
        generator_source,
        judge,
        judge_source,
        testcases,
    )


def _close_client(client: object) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _write_problem_stage(
    stage: Path,
    problem: ProblemConfig,
    statement: Statement,
    generator: GeneratorConfig | None,
    generator_source: str | None,
    judge: JudgeConfig | None,
    judge_source: str | None,
    testcases: TestcaseSnapshot | None,
    project_config: ProjectConfig,
) -> None:
    tests = stage / "tests"
    tests.mkdir()
    write_config_atomic(
        tests / "TESTSET",
        render_testset_block(TestsetConfig(generator=generator, judge=judge)),
    )
    if generator is not None and generator_source is not None:
        atomic_write_text(tests / generator.src, generator_source, create_parents=False)
    if judge is not None and judge_source is not None:
        atomic_write_text(tests / judge.src, judge_source, create_parents=False)
    atomic_write_text(
        stage / f"statement{statement.suffix}",
        statement.text,
        create_parents=False,
    )
    if testcases is not None:
        replace_local_snapshot(
            stage / project_config.rime_out_dir / tests.name,
            testcases,
        )
    # Write the discovery marker last so concurrent readers never accept an
    # incomplete staged problem as a direct-child target.
    write_config_atomic(stage / "PROBLEM", render_problem_block(problem))
    staged_problem = load_problem(stage)
    staged_problem.output_dir(project_config)


def new_problem(
    problem_id: int,
    project_path: str | Path,
    dir_name: str | None = None,
    include_testcases: bool = False,
    client_factory: ClientFactory = default_client_factory,
) -> ProblemLayout:
    """Fetch and atomically publish one new direct-child Rime problem."""

    if isinstance(problem_id, bool) or not isinstance(problem_id, int) or problem_id < 1:
        raise ValidationError("problem_id must be a positive integer")
    project = load_project(project_path)
    reserved_output_names = {"problem", "tests", "statement.md", "statement.html"}
    if project.config.rime_out_dir.casefold() in reserved_output_names:
        raise ConflictError(
            "rime_out_dir conflicts with a source path created by new: "
            f"{project.config.rime_out_dir}"
        )
    directory_name = str(problem_id) if dir_name is None else dir_name
    if directory_name.casefold() in _RESERVED_PROBLEM_DIRECTORY_NAMES:
        raise ValidationError(
            f"problem directory name is reserved by yukitools-rime: {directory_name}"
        )
    target = safe_child(project.root, directory_name, label="problem directory")
    if target.exists() or target.is_symlink():
        raise ConflictError(f"problem directory already exists: {target}")
    for existing in project.problems:
        if existing.problem_id == problem_id:
            raise ConflictError(f"problem {problem_id} already exists at {existing.path}")

    with ExitStack() as testcase_staging:
        testcase_stage = (
            Path(
                testcase_staging.enter_context(
                    tempfile.TemporaryDirectory(prefix="yukitools-rime-new-")
                )
            )
            if include_testcases
            else None
        )
        client = client_factory(project.root, project.config, problem_id)
        try:
            resources = _problem_resources(
                problem_id,
                directory_name,
                client,
                include_testcases=include_testcases,
                testcase_stage=testcase_stage,
            )
        finally:
            _close_client(client)

        stage: Path | None = None
        try:
            stage = Path(
                tempfile.mkdtemp(
                    dir=project.root,
                    prefix=f".{directory_name}.stage-",
                )
            )
            _write_problem_stage(stage, *resources, project.config)
            if target.exists() or target.is_symlink():
                raise ConflictError(f"problem directory appeared during creation: {target}")
            os.replace(stage, target)
            stage = None
        except (OSError, LayoutError) as exc:
            raise FileOperationError(f"could not create problem directory {target}: {exc}") from exc
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
        return load_problem(target)


__all__ = [
    "ClientFactory",
    "ScaffoldClient",
    "default_client_factory",
    "init_project",
    "new_problem",
]
