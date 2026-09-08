"""Safe discovery of the deliberately shallow Rime project layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yukitools_rime.errors import LayoutError, ValidationError
from yukitools_rime.models import ProblemConfig, ProjectConfig, validate_testcase_name
from yukitools_rime.rime_config import (
    TestsetConfig,
    is_managed_configuration,
    parse_problem_config,
    parse_project_config,
    parse_solution_config,
    parse_testset_config,
    read_config_source,
)


@dataclass(frozen=True, slots=True)
class TestCaseData:
    """One paired testcase using its reversible yukicoder name."""

    name: str
    input: bytes
    output: bytes

    def __post_init__(self) -> None:
        validate_testcase_name(self.name)
        if not isinstance(self.input, bytes):
            raise ValidationError("testcase input must be bytes")
        if not isinstance(self.output, bytes):
            raise ValidationError("testcase output must be bytes")


@dataclass(frozen=True, slots=True)
class TestsetLayout:
    path: Path
    config: TestsetConfig

    @property
    def config_path(self) -> Path:
        return self.path / "TESTSET"


@dataclass(frozen=True, slots=True)
class SolutionLayout:
    path: Path
    config: object

    @property
    def config_path(self) -> Path:
        return self.path / "SOLUTION"


@dataclass(frozen=True, slots=True)
class ProblemLayout:
    path: Path
    config: ProblemConfig
    testset: TestsetLayout | None
    solutions: tuple[SolutionLayout, ...]

    @property
    def config_path(self) -> Path:
        return self.path / "PROBLEM"

    @property
    def problem_id(self) -> int:
        return self.config.problem_id

    def output_dir(self, project_config: ProjectConfig) -> Path:
        directory = self.path / project_config.rime_out_dir
        if directory.is_symlink():
            raise LayoutError(f"Rime output directory must not be a symlink: {directory}")
        if directory.exists() and not directory.is_dir():
            raise LayoutError(f"Rime output path must be a directory: {directory}")
        resolved = directory.resolve(strict=False)
        try:
            resolved.relative_to(self.path)
        except ValueError as exc:
            raise LayoutError(
                f"Rime output directory escapes problem root {self.path}: {directory}"
            ) from exc
        for config_name in ("PROBLEM", "TESTSET", "SOLUTION"):
            config_path = directory / config_name
            if config_path.exists() or config_path.is_symlink():
                raise LayoutError(
                    f"rime_out_dir collides with a Rime source directory: {directory}"
                )
        return directory

    def testcase_dir(
        self,
        project_config: ProjectConfig,
        *,
        default_testset_name: str | None = None,
    ) -> Path:
        if self.testset is None and default_testset_name is None:
            raise LayoutError(f"{self.path}: no direct-child TESTSET was found")
        testset_name = self.testset.path.name if self.testset is not None else default_testset_name
        assert testset_name is not None
        directory = self.output_dir(project_config) / testset_name
        if directory.is_symlink():
            raise LayoutError(f"testcase directory must not be a symlink: {directory}")
        if directory.exists() and not directory.is_dir():
            raise LayoutError(f"testcase path must be a directory: {directory}")
        return directory


@dataclass(frozen=True, slots=True)
class ProjectLayout:
    root: Path
    config: ProjectConfig
    problems: tuple[ProblemLayout, ...]

    @property
    def config_path(self) -> Path:
        return self.root / "PROJECT"

    @property
    def sync_problems(self) -> tuple[ProblemLayout, ...]:
        """Problems selected by project-wide synchronization commands."""
        return tuple(problem for problem in self.problems if problem.config.sync)

    def problem_by_id(self, problem_id: int) -> ProblemLayout:
        for problem in self.problems:
            if problem.problem_id == problem_id:
                return problem
        raise LayoutError(f"problem id {problem_id} is not in {self.root}")


@dataclass(frozen=True, slots=True)
class TargetSelection:
    project: ProjectLayout
    problems: tuple[ProblemLayout, ...]
    target: Path
    solution: SolutionLayout | None = None

    @property
    def problem(self) -> ProblemLayout | None:
        return self.problems[0] if len(self.problems) == 1 else None


def _read(path: Path) -> str:
    return read_config_source(path)


def _regular_config(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def find_project_root(start: str | Path = ".") -> Path:
    """Walk upward from a resolved path until a regular PROJECT is found."""

    requested = Path(start)
    try:
        path = requested.resolve(strict=True)
    except OSError as exc:
        raise LayoutError(f"target does not exist: {requested}") from exc
    if path.is_file():
        path = path.parent
    for candidate in (path, *path.parents):
        if _regular_config(candidate / "PROJECT"):
            return candidate
    raise LayoutError(f"PROJECT not found above {requested}")


def _direct_component_dirs(problem_path: Path, filename: str) -> list[Path]:
    result: list[Path] = []
    try:
        children = sorted(problem_path.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise LayoutError(f"cannot inspect {problem_path}: {exc}") from exc
    for child in children:
        if child.is_symlink():
            if child.is_dir() and _regular_config(child / filename):
                raise LayoutError(f"symlink target is not allowed: {child}")
            continue
        if child.is_dir() and _regular_config(child / filename):
            result.append(child.resolve())
    return result


def load_problem(path: Path) -> ProblemLayout:
    """Load one direct-child problem and its direct components."""

    resolved = path.resolve(strict=True)
    config = parse_problem_config(_read(resolved / "PROBLEM"))
    testset_dirs = _direct_component_dirs(resolved, "TESTSET")
    if len(testset_dirs) > 1:
        names = ", ".join(item.name for item in testset_dirs)
        raise LayoutError(f"{resolved}: multiple TESTSET directories found: {names}")
    testset = None
    if testset_dirs:
        testset_path = testset_dirs[0]
        testset = TestsetLayout(
            testset_path,
            parse_testset_config(_read(testset_path / "TESTSET")),
        )
    solutions_list: list[SolutionLayout] = []
    for item in _direct_component_dirs(resolved, "SOLUTION"):
        source = _read(item / "SOLUTION")
        if is_managed_configuration(source, "yukicoder_solution"):
            solutions_list.append(SolutionLayout(item, parse_solution_config(source)))
    solutions = tuple(solutions_list)
    return ProblemLayout(resolved, config, testset, solutions)


def load_project(root: str | Path) -> ProjectLayout:
    """Load a project without recursively treating nested configs as targets."""

    resolved = Path(root).resolve(strict=True)
    project_file = resolved / "PROJECT"
    if not _regular_config(project_file):
        raise LayoutError(f"PROJECT not found at {resolved}")
    config = parse_project_config(_read(project_file))
    problems_list: list[ProblemLayout] = []
    for path in _direct_component_dirs(resolved, "PROBLEM"):
        source = _read(path / "PROBLEM")
        if is_managed_configuration(source, "yukicoder_problem"):
            problems_list.append(load_problem(path))
    problems = tuple(problems_list)
    seen: dict[int, Path] = {}
    for problem in problems:
        problem.output_dir(config)
        previous = seen.get(problem.problem_id)
        if previous is not None:
            raise LayoutError(
                f"duplicate problem id {problem.problem_id}: {previous} and {problem.path}"
            )
        seen[problem.problem_id] = problem.path
    return ProjectLayout(resolved, config, problems)


def discover_project(start: str | Path = ".") -> ProjectLayout:
    return load_project(find_project_root(start))


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_target(
    target: str | Path = ".",
    *,
    project: str | Path | ProjectLayout | None = None,
) -> TargetSelection:
    """Resolve a project/problem/component descendant to its problem selection."""

    requested = Path(target)
    try:
        resolved_target = requested.resolve(strict=True)
    except OSError as exc:
        raise LayoutError(f"target does not exist: {requested}") from exc
    if resolved_target.is_file():
        resolved_target = resolved_target.parent
    loaded = (
        project
        if isinstance(project, ProjectLayout)
        else load_project(project)
        if project is not None
        else discover_project(resolved_target)
    )
    if not _contained(resolved_target, loaded.root):
        raise LayoutError(f"target escapes project root {loaded.root}: {resolved_target}")
    if resolved_target == loaded.root:
        return TargetSelection(loaded, loaded.sync_problems, resolved_target)
    matches = tuple(
        problem for problem in loaded.problems if _contained(resolved_target, problem.path)
    )
    if len(matches) != 1:
        raise LayoutError(f"target is not inside a direct-child Rime problem: {resolved_target}")
    problem = matches[0]
    solution = next(
        (item for item in problem.solutions if _contained(resolved_target, item.path)),
        None,
    )
    return TargetSelection(loaded, (problem,), resolved_target, solution)


def local_testcase_paths(directory: Path, name: str) -> tuple[Path, Path]:
    """Map a remote name to Rime's paired .in/.diff files."""

    validate_testcase_name(name)
    return directory / f"{name}.in", directory / f"{name}.diff"


def inspect_testcases(
    directory: str | Path,
) -> tuple[dict[str, TestCaseData], tuple[str, ...], tuple[str, ...]]:
    """Read complete pairs and report names that have only one local side."""

    root = Path(directory)
    if root.is_symlink():
        raise LayoutError(f"testcase directory must not be a symlink: {root}")
    if root.exists() and not root.is_dir():
        raise LayoutError(f"testcase path must be a directory: {root}")
    if not root.is_dir():
        return {}, (), ()
    inputs: dict[str, Path] = {}
    outputs: dict[str, Path] = {}
    spellings: dict[str, str] = {}
    try:
        entries = tuple(root.iterdir())
    except OSError as exc:
        raise LayoutError(f"cannot inspect testcase directory {root}: {exc}") from exc
    for entry in entries:
        input_file = entry.name.endswith(".in")
        output_file = entry.name.endswith(".diff")
        if not input_file and not output_file:
            continue
        if entry.is_symlink():
            raise LayoutError(f"testcase path is not a regular file: {entry}")
        if not entry.is_file():
            raise LayoutError(f"testcase path is not a regular file: {entry}")
        if input_file:
            name = entry.name[:-3]
            validate_testcase_name(name)
            previous = spellings.setdefault(name.casefold(), name)
            if previous != name:
                raise LayoutError(
                    f"case-insensitive testcase name collision: {previous!r} and {name!r}"
                )
            inputs[name] = entry
        else:
            name = entry.name[:-5]
            validate_testcase_name(name)
            previous = spellings.setdefault(name.casefold(), name)
            if previous != name:
                raise LayoutError(
                    f"case-insensitive testcase name collision: {previous!r} and {name!r}"
                )
            outputs[name] = entry
    missing_outputs = tuple(sorted(inputs.keys() - outputs.keys()))
    missing_inputs = tuple(sorted(outputs.keys() - inputs.keys()))
    result: dict[str, TestCaseData] = {}
    for name in sorted(inputs.keys() & outputs.keys()):
        try:
            input_data = inputs[name].read_bytes()
            output_data = outputs[name].read_bytes()
        except OSError as exc:
            raise LayoutError(f"cannot read testcase {name!r}: {exc}") from exc
        result[name] = TestCaseData(name, input_data, output_data)
    return result, missing_outputs, missing_inputs


def read_testcases(
    directory: str | Path,
    *,
    require_nonempty: bool = True,
) -> dict[str, TestCaseData]:
    """Read and validate direct regular .in/.diff pairs as raw bytes."""

    root = Path(directory)
    if require_nonempty and not root.is_dir():
        raise LayoutError(f"testcase directory does not exist: {root}")
    result, missing_outputs, missing_inputs = inspect_testcases(root)
    if missing_outputs or missing_inputs:
        details: list[str] = []
        if missing_outputs:
            details.append("missing .diff for " + ", ".join(missing_outputs))
        if missing_inputs:
            details.append("missing .in for " + ", ".join(missing_inputs))
        raise LayoutError(f"incomplete testcase pairs in {root}: {'; '.join(details)}")
    if require_nonempty and not result:
        raise LayoutError(f"no testcase pairs found in {root}")
    return result


def testcase_change_counts(
    local: dict[str, TestCaseData],
    remote: dict[str, TestCaseData],
) -> tuple[int, int, int]:
    """Return remote-to-local added, changed, and removed counts."""

    added = remote.keys() - local.keys()
    removed = local.keys() - remote.keys()
    changed = {name for name in remote.keys() & local.keys() if remote[name] != local[name]}
    return len(added), len(changed), len(removed)


read_testcase_snapshot = read_testcases
resolve_rime_target = resolve_target
