"""Pull and read-only diff services for Rime-backed yukicoder problems."""

from __future__ import annotations

import difflib
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Protocol, TypeAlias

from yukitools_rime.api.types import (
    EditorialContent,
    GeneratorContent,
    JudgeCodeContent,
    ProblemEditContent,
    ValidatorContent,
)
from yukitools_rime.errors import ConflictError, FileOperationError, LayoutError, ValidationError
from yukitools_rime.files import (
    atomic_write_bytes,
    normalize_text,
    read_bytes,
    read_text,
    remove_file,
)
from yukitools_rime.layout import (
    ProblemLayout,
    ProjectLayout,
    TargetSelection,
    discover_project,
    inspect_testcases,
)
from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProjectConfig,
    ValidatorConfig,
)
from yukitools_rime.rime_config import (
    TestsetConfig,
    merge_remote_problem,
    parse_problem_config,
    parse_testset_config,
    read_config_source,
    render_problem_block,
    render_testset_block,
    upsert_managed_block,
)
from yukitools_rime.source_languages import default_source_name, infer_rime_kind
from yukitools_rime.testcase_sync import (
    SnapshotChanges,
    TestcaseAPI,
    TestcaseSnapshot,
    compare_snapshots,
    fetch_remote_snapshot_to,
    replace_local_snapshot,
    validate_testcase_names_for_server,
)


class SyncClient(TestcaseAPI, Protocol):
    """Remote operations required by both pull and diff."""

    def get_problem_edit(self, problem_id: int) -> ProblemEditContent: ...

    def get_generator(self, problem_id: int) -> GeneratorContent | None: ...

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent | None: ...

    def get_validator(self, problem_id: int) -> ValidatorContent: ...

    def get_editorial(self, problem_id: int) -> EditorialContent: ...


ClientFactory: TypeAlias = Callable[[ProblemLayout], SyncClient]
ConfirmTestcases: TypeAlias = Callable[[ProblemLayout, SnapshotChanges], bool]
SyncTarget: TypeAlias = TargetSelection | ProjectLayout | ProblemLayout


@dataclass(frozen=True, slots=True)
class PullProblemResult:
    problem_id: int
    path: Path
    changed_paths: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()
    testcase_changes: SnapshotChanges | None = None
    testcases_applied: bool = False

    @property
    def applied(self) -> bool:
        return bool(self.changed_paths or self.testcases_applied)


@dataclass(frozen=True, slots=True)
class PullResult:
    problems: tuple[PullProblemResult, ...]

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(warning for problem in self.problems for warning in problem.warnings)

    @property
    def applied(self) -> bool:
        return any(problem.applied for problem in self.problems)


@dataclass(frozen=True, slots=True)
class DiffEntry:
    """One semantic difference, optionally followed by unified-diff lines."""

    resource: str
    detail: str
    unified: tuple[str, ...] = ()

    @property
    def lines(self) -> tuple[str, ...]:
        return (f"{self.resource}: {self.detail}", *self.unified)


@dataclass(frozen=True, slots=True)
class ProblemDiffResult:
    problem_id: int
    path: Path
    entries: tuple[DiffEntry, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(self.entries)

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(line for entry in self.entries for line in entry.lines)


@dataclass(frozen=True, slots=True)
class DiffResult:
    problems: tuple[ProblemDiffResult, ...]

    @property
    def has_changes(self) -> bool:
        return any(problem.has_changes for problem in self.problems)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(warning for problem in self.problems for warning in problem.warnings)

    @property
    def lines(self) -> tuple[str, ...]:
        result: list[str] = []
        for problem in self.problems:
            result.append(f"--- problem {problem.problem_id} ---")
            result.extend(problem.lines)
        return tuple(result)


@dataclass(frozen=True, slots=True)
class _RemoteProblem:
    problem: ProblemLayout
    edit: ProblemEditContent
    generator: GeneratorContent | None
    judge: JudgeCodeContent | None
    editorial: EditorialContent
    testcases: TestcaseSnapshot | None
    validator: ValidatorContent = field(default_factory=ValidatorContent)


@dataclass(frozen=True, slots=True)
class _Mutation:
    path: Path
    data: bytes | None


@dataclass(frozen=True, slots=True)
class _PullPlan:
    remote: _RemoteProblem
    mutations: tuple[_Mutation, ...]
    warnings: tuple[str, ...]
    testcase_changes: SnapshotChanges | None
    apply_testcases: bool
    testcase_dir: Path | None


def _selected_problems(target: SyncTarget) -> tuple[ProblemLayout, ...]:
    if isinstance(target, ProblemLayout):
        return (target,)
    if isinstance(target, ProjectLayout):
        if not target.problems:
            raise LayoutError("sync target contains no managed problems")
        return target.sync_problems
    if target.problems:
        return target.problems
    if target.project.problems and target.target == target.project.root:
        return ()
    raise LayoutError("sync target contains no managed problems")


def _project_config(target: SyncTarget) -> ProjectConfig:
    if isinstance(target, ProjectLayout):
        return target.config
    if isinstance(target, TargetSelection):
        return target.project.config
    return discover_project(target.path).config


def _get_validator(client: SyncClient, problem_id: int) -> ValidatorContent:
    """Fetch validator data, tolerating legacy injected clients used by integrations."""

    getter = getattr(client, "get_validator", None)
    if getter is None:
        return ValidatorContent()
    result = getter(problem_id)
    if not isinstance(result, ValidatorContent):
        raise ValidationError("validator API returned an unexpected response type")
    return result


def _fetch_remote(
    target: SyncTarget,
    client_factory: ClientFactory,
    *,
    include_testcases: bool,
    validate_local_testcase_names: bool = False,
    testcase_staging: ExitStack | None = None,
) -> tuple[_RemoteProblem, ...]:
    """Fetch every requested resource for every target before local mutation."""

    fetched: list[_RemoteProblem] = []
    for problem in _selected_problems(target):
        client = client_factory(problem)
        edit = client.get_problem_edit(problem.problem_id)
        if edit.problem_id != problem.problem_id:
            raise ValidationError(
                f"requested problem {problem.problem_id}, but remote returned "
                f"problem {edit.problem_id}"
            )
        generator = client.get_generator(problem.problem_id)
        judge = client.get_judge_code(problem.problem_id)
        validator = _get_validator(client, problem.problem_id)
        editorial = client.get_editorial(problem.problem_id)
        if include_testcases:
            if testcase_staging is None:
                raise ValueError("testcase staging is required when fetching testcases")
            staging_directory = Path(
                testcase_staging.enter_context(
                    tempfile.TemporaryDirectory(prefix=f"yukitools-rime-{problem.problem_id}-")
                )
            )
            testcase_dir = _testcase_directory(problem, _project_config(target))
            local, missing_outputs, missing_inputs = inspect_testcases(testcase_dir)
            if validate_local_testcase_names:
                validate_testcase_names_for_server(
                    client,
                    (*local, *missing_outputs, *missing_inputs),
                )
            testcases = fetch_remote_snapshot_to(
                client,
                problem.problem_id,
                staging_directory,
                reuse=local,
            )
        else:
            testcases = None
        fetched.append(
            _RemoteProblem(
                problem,
                edit,
                generator,
                judge,
                editorial,
                testcases,
                validator,
            )
        )
    return tuple(fetched)


def _document(
    problem: ProblemLayout,
    stem: str,
) -> tuple[Path, str, bool] | None:
    markdown = problem.path / f"{stem}.md"
    html = problem.path / f"{stem}.html"
    present = [path for path in (markdown, html) if path.exists() or path.is_symlink()]
    if len(present) > 1:
        raise LayoutError(f"{problem.path}: both {stem}.md and {stem}.html exist")
    if not present:
        return None
    path = present[0]
    return path, read_text(path), path.suffix == ".md"


def _remote_document_path(problem: ProblemLayout, stem: str, markdown: bool) -> Path:
    return problem.path / f"{stem}{'.md' if markdown else '.html'}"


def _mutation_if_changed(path: Path, data: bytes | None) -> _Mutation | None:
    exists = path.exists() or path.is_symlink()
    if data is None:
        return _Mutation(path, None) if exists else None
    if exists and read_bytes(path) == data:
        return None
    return _Mutation(path, data)


def _append_mutation(mutations: list[_Mutation], path: Path, data: bytes | None) -> None:
    mutation = _mutation_if_changed(path, data)
    if mutation is not None:
        mutations.append(mutation)


def _update_document(
    mutations: list[_Mutation],
    problem: ProblemLayout,
    stem: str,
    remote_text: str,
    remote_markdown: bool,
    *,
    only_if_present: bool,
) -> None:
    local = _document(problem, stem)
    if local is None and only_if_present:
        return
    target = _remote_document_path(problem, stem, remote_markdown)
    _append_mutation(mutations, target, normalize_text(remote_text).encode("utf-8"))
    if local is not None and local[0] != target:
        _append_mutation(mutations, local[0], None)


def _testset_state(problem: ProblemLayout) -> tuple[Path, str, TestsetConfig]:
    if problem.testset is None:
        path = problem.path / "tests"
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ConflictError(f"cannot create TESTSET at unsafe path: {path}")
        if path.exists():
            try:
                entries = tuple(path.iterdir())
            except OSError as exc:
                raise FileOperationError(f"cannot inspect TESTSET directory {path}: {exc}") from exc
            if entries:
                raise ConflictError(f"cannot create managed TESTSET in non-empty directory: {path}")
        return path, "", TestsetConfig()
    path = problem.testset.path
    source = read_config_source(path / "TESTSET")
    return path, source, parse_testset_config(source)


def _testcase_directory(problem: ProblemLayout, project_config: ProjectConfig) -> Path:
    return problem.testcase_dir(
        project_config,
        default_testset_name="tests",
    )


def _plan_testcases(
    remote: _RemoteProblem,
    project_config: ProjectConfig,
    confirm: ConfirmTestcases | None,
) -> tuple[SnapshotChanges | None, bool, Path | None]:
    if remote.testcases is None:
        return None, False, None
    directory = _testcase_directory(remote.problem, project_config)
    existed = directory.is_dir() and not directory.is_symlink()
    incomplete: tuple[str, ...] = ()
    local: TestcaseSnapshot = {}
    if existed:
        local, missing_outputs, missing_inputs = inspect_testcases(directory)
        incomplete = missing_outputs + missing_inputs
    changes = compare_snapshots(local, remote.testcases, incomplete=incomplete)
    if not changes.has_changes:
        return changes, False, directory
    if existed:
        if confirm is None:
            raise ConflictError(
                f"remote testcases differ from {directory}; replacement needs confirmation"
            )
        if not confirm(remote.problem, changes):
            return changes, False, directory
    return changes, True, directory


def _build_pull_plan(
    remote: _RemoteProblem,
    project_config: ProjectConfig,
    confirm: ConfirmTestcases | None,
) -> _PullPlan:
    problem = remote.problem
    mutations: list[_Mutation] = []
    warnings: list[str] = []

    problem_source = read_config_source(problem.config_path)
    local_problem = parse_problem_config(problem_source)
    remote_problem = ProblemConfig(
        problem_id=remote.edit.problem_id,
        settings=remote.edit.settings,
        rime_id=local_problem.rime_id,
    )
    merged = merge_remote_problem(local_problem, remote_problem)
    rendered_problem = upsert_managed_block(problem_source, render_problem_block(merged))
    _append_mutation(
        mutations,
        problem.config_path,
        rendered_problem.encode("utf-8"),
    )
    _update_document(
        mutations,
        problem,
        "statement",
        remote.edit.content,
        remote.edit.is_markdown,
        only_if_present=False,
    )
    if not remote.edit.showable:
        warnings.append(f"problem {problem.problem_id} is not public")

    testset_path, testset_source, testset = _testset_state(problem)
    original_testset = TestsetConfig(testset.generator, testset.judge, testset.validator)
    generator = remote.generator
    if generator is None or not generator.source.strip():
        warnings.append(f"problem {problem.problem_id}: remote generator is unavailable or empty")
    else:
        existing = testset.generator
        config = GeneratorConfig(
            lang_id=generator.lang_id,
            src=existing.src if existing else default_source_name("generator", generator.lang_id),
            test_case_num=generator.test_case_num,
            prefix=existing.prefix if existing else None,
            rime_kind=existing.rime_kind if existing else infer_rime_kind(generator.lang_id),
            rime_options=dict(existing.rime_options) if existing else {},
        )
        source_path = testset_path / config.src
        if existing is None and (source_path.exists() or source_path.is_symlink()):
            raise ConflictError(f"refusing to overwrite existing generator source: {source_path}")
        testset.generator = config
        _append_mutation(
            mutations,
            source_path,
            normalize_text(generator.source).encode("utf-8"),
        )

    judge = remote.judge
    if judge is None or not judge.source.strip():
        warnings.append(f"problem {problem.problem_id}: remote judge is unavailable or empty")
    else:
        existing_judge = testset.judge
        judge_config = JudgeConfig(
            lang_id=judge.lang_id,
            src=(
                existing_judge.src
                if existing_judge
                else default_source_name("judge", judge.lang_id)
            ),
            rime_kind=(
                existing_judge.rime_kind if existing_judge else infer_rime_kind(judge.lang_id)
            ),
            rime_options=dict(existing_judge.rime_options) if existing_judge else {},
        )
        testset.judge = judge_config
        judge_source_path = testset_path / judge_config.src
        if existing_judge is None and (
            judge_source_path.exists() or judge_source_path.is_symlink()
        ):
            raise ConflictError(f"refusing to overwrite existing judge source: {judge_source_path}")
        _append_mutation(
            mutations,
            judge_source_path,
            normalize_text(judge.source).encode("utf-8"),
        )

    validator = remote.validator
    if not validator.source.strip():
        if testset.validator is not None:
            warnings.append(
                f"problem {problem.problem_id}: remote validator is unavailable or empty"
            )
    else:
        existing_validator = testset.validator
        validator_config = ValidatorConfig(
            lang_id=validator.lang_id,
            src=(
                existing_validator.src
                if existing_validator
                else default_source_name("validator", validator.lang_id)
            ),
            rime_kind=(
                existing_validator.rime_kind
                if existing_validator
                else infer_rime_kind(validator.lang_id)
            ),
            rime_options=(dict(existing_validator.rime_options) if existing_validator else {}),
        )
        testset.validator = validator_config
        validator_source_path = testset_path / validator_config.src
        if existing_validator is None and (
            validator_source_path.exists() or validator_source_path.is_symlink()
        ):
            raise ConflictError(
                f"refusing to overwrite existing validator source: {validator_source_path}"
            )
        _append_mutation(
            mutations,
            validator_source_path,
            normalize_text(validator.source).encode("utf-8"),
        )

    testset = TestsetConfig(testset.generator, testset.judge, testset.validator)
    needs_testset_for_cases = problem.testset is None and remote.testcases is not None

    if testset != original_testset or needs_testset_for_cases:
        rendered_testset = upsert_managed_block(testset_source, render_testset_block(testset))
        _append_mutation(
            mutations,
            testset_path / "TESTSET",
            rendered_testset.encode("utf-8"),
        )

    _update_document(
        mutations,
        problem,
        "editorial",
        remote.editorial.content,
        remote.editorial.is_markdown,
        only_if_present=True,
    )
    testcase_changes, apply_testcases, testcase_dir = _plan_testcases(
        remote, project_config, confirm
    )
    if testcase_changes is not None and testcase_changes.has_changes and not apply_testcases:
        warnings.append(f"problem {problem.problem_id}: testcase replacement was declined")
    return _PullPlan(
        remote,
        tuple(mutations),
        tuple(warnings),
        testcase_changes,
        apply_testcases,
        testcase_dir,
    )


def _restore_mutations(
    backups: list[tuple[Path, bytes | None]],
    created_dirs: tuple[Path, ...] = (),
) -> Exception | None:
    rollback_error: Exception | None = None
    for path, previous in reversed(backups):
        try:
            if previous is None:
                remove_file(path, missing_ok=True)
            else:
                atomic_write_bytes(path, previous)
        except Exception as exc:
            rollback_error = rollback_error or exc
    for directory in sorted(created_dirs, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            rollback_error = rollback_error or exc
    return rollback_error


def _missing_parent_dirs(mutations: tuple[_Mutation, ...]) -> tuple[Path, ...]:
    missing: set[Path] = set()
    for mutation in mutations:
        if mutation.data is None:
            continue
        directory = mutation.path.parent
        while not directory.exists():
            missing.add(directory)
            directory = directory.parent
    return tuple(missing)


def _apply_mutations(
    mutations: tuple[_Mutation, ...],
) -> tuple[list[tuple[Path, bytes | None]], tuple[Path, ...]]:
    backups: list[tuple[Path, bytes | None]] = []
    created_dirs = _missing_parent_dirs(mutations)
    try:
        for mutation in mutations:
            existed = mutation.path.exists() or mutation.path.is_symlink()
            previous = read_bytes(mutation.path) if existed else None
            backups.append((mutation.path, previous))
            if mutation.data is None:
                remove_file(mutation.path)
            else:
                atomic_write_bytes(mutation.path, mutation.data)
    except BaseException as error:
        rollback_error = _restore_mutations(backups, created_dirs)
        if rollback_error is not None:
            raise FileOperationError(
                f"pull failed and rollback also failed: {rollback_error}"
            ) from error
        raise
    return backups, created_dirs


def pull(
    target: SyncTarget,
    client_factory: ClientFactory,
    *,
    include_testcases: bool = False,
    confirm: ConfirmTestcases | None = None,
) -> PullResult:
    """Fetch first, then transactionally apply each selected problem."""

    with ExitStack() as testcase_staging:
        remote_problems = _fetch_remote(
            target,
            client_factory,
            include_testcases=include_testcases,
            testcase_staging=testcase_staging,
        )
        project_config = _project_config(target)
        plans = tuple(
            _build_pull_plan(remote, project_config, confirm) for remote in remote_problems
        )
        results: list[PullProblemResult] = []
        for plan in plans:
            backups, created_dirs = _apply_mutations(plan.mutations)
            if plan.apply_testcases:
                assert plan.testcase_dir is not None
                assert plan.remote.testcases is not None
                try:
                    replace_local_snapshot(plan.testcase_dir, plan.remote.testcases)
                except BaseException as error:
                    rollback_error = _restore_mutations(backups, created_dirs)
                    if rollback_error is not None:
                        raise FileOperationError(
                            "testcase replacement failed and regular-file rollback "
                            f"also failed: {rollback_error}"
                        ) from error
                    raise
            changed_paths = tuple(mutation.path for mutation in plan.mutations)
            if plan.apply_testcases and plan.testcase_dir is not None:
                changed_paths += (plan.testcase_dir,)
            results.append(
                PullProblemResult(
                    plan.remote.problem.problem_id,
                    plan.remote.problem.path,
                    changed_paths,
                    plan.warnings,
                    plan.testcase_changes,
                    plan.apply_testcases,
                )
            )
        return PullResult(tuple(results))


def _unified(
    resource: str,
    remote: str,
    local: str,
) -> tuple[str, ...]:
    remote_text = normalize_text(remote)
    local_text = normalize_text(local)
    if remote_text == local_text:
        return ()
    return tuple(
        line.rstrip("\n")
        for line in difflib.unified_diff(
            remote_text.splitlines(keepends=True),
            local_text.splitlines(keepends=True),
            fromfile=f"yukicoder/{resource}",
            tofile=f"local/{resource}",
            lineterm="",
        )
    )


def _text_entries(
    resource: str,
    remote_text: str,
    remote_markdown: bool,
    local: tuple[Path, str, bool] | None,
) -> list[DiffEntry]:
    if local is None:
        return [DiffEntry(resource, "missing locally")]
    path, local_text, local_markdown = local
    entries: list[DiffEntry] = []
    if remote_markdown != local_markdown:
        entries.append(
            DiffEntry(
                resource,
                "format differs "
                f"(remote={'Markdown' if remote_markdown else 'HTML'}, "
                f"local={'Markdown' if local_markdown else 'HTML'})",
            )
        )
    unified = _unified(path.name, remote_text, local_text)
    if unified:
        entries.append(DiffEntry(resource, "content differs", unified))
    return entries


def _source_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise FileOperationError(f"source is not a regular file: {path}")
    return read_text(path)


def _diff_problem(remote: _RemoteProblem, project_config: ProjectConfig) -> ProblemDiffResult:
    problem = remote.problem
    entries: list[DiffEntry] = []
    warnings: list[str] = []
    local_problem = parse_problem_config(read_config_source(problem.config_path))
    for model_field in fields(remote.edit.settings):
        name = model_field.name
        remote_value = getattr(remote.edit.settings, name)
        local_value = getattr(local_problem.settings, name)
        if remote_value != local_value:
            entries.append(
                DiffEntry(
                    f"settings.{name}",
                    f"remote={remote_value!r} local={local_value!r}",
                )
            )

    entries.extend(
        _text_entries(
            "statement",
            remote.edit.content,
            remote.edit.is_markdown,
            _document(problem, "statement"),
        )
    )
    local_editorial = _document(problem, "editorial")
    if local_editorial is not None:
        entries.extend(
            _text_entries(
                "editorial",
                remote.editorial.content,
                remote.editorial.is_markdown,
                local_editorial,
            )
        )

    testset_path, _, testset = _testset_state(problem)
    generator = remote.generator
    if generator is None or not generator.source.strip():
        if testset.generator is not None:
            local_source = _source_text(testset_path / testset.generator.src)
            if local_source.strip():
                entries.append(DiffEntry("generator", "registered locally but empty remotely"))
        warnings.append(f"problem {problem.problem_id}: remote generator is unavailable or empty")
    elif testset.generator is None:
        entries.append(DiffEntry("generator", "present remotely but missing locally"))
    else:
        local_generator = testset.generator
        if generator.lang_id != local_generator.lang_id:
            entries.append(
                DiffEntry(
                    "generator.lang_id",
                    f"remote={generator.lang_id!r} local={local_generator.lang_id!r}",
                )
            )
        if generator.test_case_num != local_generator.test_case_num:
            entries.append(
                DiffEntry(
                    "generator.test_case_num",
                    f"remote={generator.test_case_num!r} local={local_generator.test_case_num!r}",
                )
            )
        unified = _unified(
            local_generator.src,
            generator.source,
            _source_text(testset_path / local_generator.src),
        )
        if unified:
            entries.append(DiffEntry("generator", "source differs", unified))

    judge = remote.judge
    if judge is None:
        warnings.append(f"problem {problem.problem_id}: remote judge API is unavailable")
    elif not judge.source.strip():
        if testset.judge is not None:
            local_source = _source_text(testset_path / testset.judge.src)
            if local_source.strip():
                entries.append(DiffEntry("judge", "registered locally but empty remotely"))
        warnings.append(f"problem {problem.problem_id}: remote judge is empty")
    elif testset.judge is None:
        entries.append(DiffEntry("judge", "present remotely but missing locally"))
    else:
        local_judge = testset.judge
        if judge.lang_id != local_judge.lang_id:
            entries.append(
                DiffEntry(
                    "judge.lang_id",
                    f"remote={judge.lang_id!r} local={local_judge.lang_id!r}",
                )
            )
        unified = _unified(
            local_judge.src,
            judge.source,
            _source_text(testset_path / local_judge.src),
        )
        if unified:
            entries.append(DiffEntry("judge", "source differs", unified))

    validator = remote.validator
    if not validator.source.strip():
        if testset.validator is not None:
            local_source = _source_text(testset_path / testset.validator.src)
            if local_source.strip():
                entries.append(DiffEntry("validator", "registered locally but empty remotely"))
            warnings.append(
                f"problem {problem.problem_id}: remote validator is unavailable or empty"
            )
    elif testset.validator is None:
        entries.append(DiffEntry("validator", "present remotely but missing locally"))
    else:
        local_validator = testset.validator
        if validator.lang_id != local_validator.lang_id:
            entries.append(
                DiffEntry(
                    "validator.lang_id",
                    f"remote={validator.lang_id!r} local={local_validator.lang_id!r}",
                )
            )
        unified = _unified(
            local_validator.src,
            validator.source,
            _source_text(testset_path / local_validator.src),
        )
        if unified:
            entries.append(DiffEntry("validator", "source differs", unified))

    if remote.testcases is not None:
        directory = _testcase_directory(problem, project_config)
        local_cases, missing_outputs, missing_inputs = inspect_testcases(directory)
        changes = compare_snapshots(
            local_cases, remote.testcases, incomplete=missing_outputs + missing_inputs
        )
        for name in changes.added:
            entries.append(DiffEntry(f"testcase {name}", "exists only remotely"))
        for name in changes.removed:
            entries.append(DiffEntry(f"testcase {name}", "exists only locally"))
        for name in changes.changed:
            entries.append(DiffEntry(f"testcase {name}", "raw bytes differ"))

    return ProblemDiffResult(
        problem.problem_id,
        problem.path,
        tuple(entries),
        tuple(warnings),
    )


def diff_remote(
    target: SyncTarget,
    client_factory: ClientFactory,
    *,
    include_testcases: bool = False,
) -> DiffResult:
    """Return remote-to-local differences without modifying any local path."""

    with ExitStack() as testcase_staging:
        remote_problems = _fetch_remote(
            target,
            client_factory,
            include_testcases=include_testcases,
            validate_local_testcase_names=include_testcases,
            testcase_staging=testcase_staging,
        )
        project_config = _project_config(target)
        return DiffResult(
            tuple(_diff_problem(remote, project_config) for remote in remote_problems)
        )


diff = diff_remote
pull_selection = pull
diff_selection = diff_remote


__all__ = [
    "ClientFactory",
    "ConfirmTestcases",
    "DiffEntry",
    "DiffResult",
    "ProblemDiffResult",
    "PullProblemResult",
    "PullResult",
    "SyncClient",
    "SyncTarget",
    "default_source_name",
    "diff",
    "diff_remote",
    "diff_selection",
    "infer_rime_kind",
    "pull",
    "pull_selection",
]
