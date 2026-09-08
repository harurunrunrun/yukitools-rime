from __future__ import annotations

import os
from pathlib import Path

import pytest

from yukitools_rime.api.types import (
    EditorialContent,
    GeneratorContent,
    JudgeCodeContent,
    ProblemEditContent,
    StatusInfo,
    ValidatorContent,
)
from yukitools_rime.commands import push as push_module
from yukitools_rime.commands import sync as sync_module
from yukitools_rime.errors import ConfigError, ConflictError, FileOperationError, ValidationError
from yukitools_rime.layout import ProjectLayout, load_project
from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    ValidatorConfig,
)
from yukitools_rime.rime_config import (
    TestsetConfig as RimeTestsetConfig,
)
from yukitools_rime.rime_config import (
    render_problem_block,
    render_project_block,
    render_testset_block,
)
from yukitools_rime.source_bundle import (
    bundle_program_source,
    parse_project_library_dir,
)


def _settings() -> ProblemSettings:
    return ProblemSettings(
        title="dependency test",
        tags="",
        level=1.0,
        time_limit_ms=1000,
        memory_limit=256,
        eps_mode="-",
        eps="0",
        wip=False,
        recruiting_tester=False,
        problem_type=0,
        judge_type=1,
    )


def _bundle_fixture(
    root: Path,
    *,
    source: str = '#include "dep.h"\nint main() {}\n',
    headers: dict[str, str] | None = None,
    project_call: str = 'project(library_dir="common")\n',
) -> tuple[Path, Path]:
    root.mkdir()
    (root / "PROJECT").write_text(project_call, encoding="utf-8")
    library = root / "common"
    library.mkdir()
    for name, content in (headers or {"dep.h": "#define DEP 1\n"}).items():
        (library / name).write_text(content, encoding="utf-8")
    source_dir = root / "problem" / "tests"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "generator.cpp"
    source_path.write_text(source, encoding="utf-8")
    return root, source_path


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('project(library_dir="common")\n', "common"),
        ("project('headers')\n", "headers"),
        ("project('lib', '9.9.9')\n", "lib"),
        ('\ufeffproject(required_rime_plus_version="1.0", library_dir="shared")\n', "shared"),
    ],
)
def test_parse_project_library_dir_accepts_literal_rime_plus_forms(
    source: str, expected: str
) -> None:
    assert parse_project_library_dir(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "",
        'project(library_dir="a")\nproject(library_dir="b")\n',
        'if True:\n    project(library_dir="common")\n',
        "project(library_dir=get_library())\n",
        "project()\n",
        "project(None)\n",
        "project('a', '1', 'extra')\n",
        "project(**{'library_dir': 'common'})\n",
        "project('a', library_dir='b')\n",
        "project(library_dir='unterminated)\n",
    ],
)
def test_parse_project_library_dir_rejects_ambiguous_or_dynamic_forms(source: str) -> None:
    with pytest.raises(ConfigError):
        parse_project_library_dir(source)


def test_project_parser_never_executes_configuration(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    source = f"project(library_dir=__import__('pathlib').Path({str(marker)!r}).write_text('bad'))\n"
    with pytest.raises(ConfigError, match="literal"):
        parse_project_library_dir(source)
    assert not marker.exists()


def test_bundle_recursively_expands_all_declared_quoted_dependencies(tmp_path: Path) -> None:
    root, source_path = _bundle_fixture(
        tmp_path / "repo",
        source=(
            "#include <vector>\n"
            '#include "testlib.h" // server also has this name\n'
            '#include "server_only.h"\n'
            '#include "./outer.hpp"\n'
            "int main() { return OUTER + TESTLIB; }\n"
        ),
        headers={
            "testlib.h": "#pragma once\n#define TESTLIB 1\n",
            "outer.hpp": '#include "inner.hpp"\n#define OUTER INNER\n',
            "inner.hpp": "#define INNER 2",
        },
    )
    before = source_path.read_bytes()
    before_mtime = source_path.stat().st_mtime_ns

    result = bundle_program_source(
        root,
        source_path,
        {"dependency": ["testlib.h", "outer.hpp", "inner.hpp"]},
    )

    assert "#include <vector>" in result
    assert '#include "server_only.h"' in result
    assert '#include "testlib.h"' not in result
    assert '#include "./outer.hpp"' not in result
    assert '#include "inner.hpp"' not in result
    assert result.index("DEPENDENCY: testlib.h") < result.index("#define TESTLIB")
    assert result.index("DEPENDENCY: outer.hpp") < result.index("DEPENDENCY: inner.hpp")
    assert result.index("#define INNER") < result.index("#define OUTER")
    assert result.index("#define OUTER") < result.index("int main")
    assert result.endswith("\n")
    assert source_path.read_bytes() == before
    assert source_path.stat().st_mtime_ns == before_mtime


def test_no_dependencies_returns_normalized_source_without_reading_project(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source_path = root / "problem" / "tests" / "generator.cpp"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"\xef\xbb\xbfline1\r\nline2\r")
    assert bundle_program_source(root, source_path, {}) == "line1\nline2\n"


def test_empty_deletion_source_skips_even_invalid_dependency_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source_path = root / "problem" / "tests" / "judge.cpp"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b" \r\n")
    assert bundle_program_source(root, source_path, {"dependency": object()}) == " \n"


@pytest.mark.parametrize(
    "options",
    [
        {"dependency": "dep.h"},
        {"dependency": [1]},
        {"dependency": ["dep.h", "DEP.H"]},
        {"dependency": ["../dep.h"]},
        {"dependency": ["NUL.h"]},
    ],
)
def test_dependency_metadata_is_portable_and_unambiguous(
    tmp_path: Path, options: dict[str, object]
) -> None:
    root, source_path = _bundle_fixture(tmp_path / "repo")
    with pytest.raises(ValidationError):
        bundle_program_source(root, source_path, options)


def test_declared_but_unused_dependency_is_allowed_like_rime(tmp_path: Path) -> None:
    root, source_path = _bundle_fixture(
        tmp_path / "repo",
        source="#include <dep.h>\n",
    )
    assert (
        bundle_program_source(
            root,
            source_path,
            {"dependency": ["dep.h"]},
        )
        == "#include <dep.h>\n"
    )


def test_cyclic_dependency_include_is_rejected_with_chain(tmp_path: Path) -> None:
    root, source_path = _bundle_fixture(
        tmp_path / "repo",
        source='#include "a.h"\n',
        headers={
            "a.h": '#include "b.h"\n',
            "b.h": '#include "a.h"\n',
        },
    )
    with pytest.raises(ConfigError, match=r"a\.h -> b\.h -> a\.h"):
        bundle_program_source(root, source_path, {"dependency": ["a.h", "b.h"]})


@pytest.mark.parametrize(
    "library_dir",
    ["/tmp", ".", "..", "../common", "common/", "common//nested", "common\\nested", "C:/common"],
)
def test_library_directory_must_be_safe_and_relative(tmp_path: Path, library_dir: str) -> None:
    root, source_path = _bundle_fixture(
        tmp_path / "repo",
        project_call=f"project(library_dir={library_dir!r})\n",
    )
    with pytest.raises(ValidationError):
        bundle_program_source(root, source_path, {"dependency": ["dep.h"]})


def test_library_directory_must_exist_and_be_a_directory(tmp_path: Path) -> None:
    root, source_path = _bundle_fixture(tmp_path / "repo")
    os.replace(root / "common", root / "moved")
    with pytest.raises(ConfigError, match="missing"):
        bundle_program_source(root, source_path, {"dependency": ["dep.h"]})

    (root / "common").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ConfigError, match="not a directory"):
        bundle_program_source(root, source_path, {"dependency": ["dep.h"]})


def test_library_and_header_symlinks_are_rejected(tmp_path: Path) -> None:
    root, source_path = _bundle_fixture(tmp_path / "repo")
    actual_library = root / "actual"
    os.replace(root / "common", actual_library)
    try:
        (root / "common").symlink_to(actual_library, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(ConfigError, match="symlink"):
        bundle_program_source(root, source_path, {"dependency": ["dep.h"]})

    (root / "common").unlink()
    os.replace(actual_library, root / "common")
    real_header = root / "common" / "real.h"
    real_header.write_text("#define DEP 1\n", encoding="utf-8")
    (root / "common" / "dep.h").unlink()
    (root / "common" / "dep.h").symlink_to(real_header)
    with pytest.raises(FileOperationError, match="regular file"):
        bundle_program_source(root, source_path, {"dependency": ["dep.h"]})


def test_missing_and_non_utf8_headers_are_rejected(tmp_path: Path) -> None:
    root, source_path = _bundle_fixture(tmp_path / "repo")
    (root / "common" / "dep.h").unlink()
    with pytest.raises(FileOperationError, match="regular file"):
        bundle_program_source(root, source_path, {"dependency": ["dep.h"]})

    (root / "common" / "dep.h").write_bytes(b"\xff")
    with pytest.raises(FileOperationError, match="UTF-8"):
        bundle_program_source(root, source_path, {"dependency": ["dep.h"]})


def test_source_must_remain_inside_project(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.cpp"
    outside.write_text("int main() {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="escapes"):
        bundle_program_source(root, outside, {})


def _integration_project(root: Path) -> tuple[ProjectLayout, dict[str, str]]:
    root.mkdir()
    (root / "PROJECT").write_text(
        'use_plugin("plus")\nproject(library_dir="common")\n'
        + render_project_block(ProjectConfig()),
        encoding="utf-8",
    )
    library = root / "common"
    library.mkdir()
    (library / "testlib.h").write_text("#define TESTLIB 1\n", encoding="utf-8")
    (library / "shared.hpp").write_text(
        '#include "nested.hpp"\n#define SHARED NESTED\n',
        encoding="utf-8",
    )
    (library / "nested.hpp").write_text("#define NESTED 2\n", encoding="utf-8")

    problem = root / "a"
    problem.mkdir()
    (problem / "PROBLEM").write_text(
        render_problem_block(ProblemConfig(1, _settings(), "A")),
        encoding="utf-8",
    )
    (problem / "statement.md").write_text("statement\n", encoding="utf-8")
    testset = problem / "tests"
    testset.mkdir()
    options = {"dependency": ["testlib.h", "shared.hpp", "nested.hpp"]}
    configs = RimeTestsetConfig(
        GeneratorConfig("cpp20", "generator.cpp", 3, None, "cxx", options),
        JudgeConfig("cpp20", "judge.cpp", "cxx", options),
        ValidatorConfig("cpp20", "validator.cpp", "cxx", options),
    )
    (testset / "TESTSET").write_text(render_testset_block(configs), encoding="utf-8")
    for name in ("generator", "judge", "validator"):
        source = (
            '#include "testlib.h"\n'
            '#include "shared.hpp"\n'
            f"int {name}() {{ return SHARED + TESTLIB; }}\n"
        )
        (testset / f"{name}.cpp").write_bytes(source.encode("utf-8"))
    layout = load_project(root)
    loaded_testset = layout.problems[0].testset
    assert loaded_testset is not None
    expected = {
        name: bundle_program_source(
            root,
            testset / f"{name}.cpp",
            getattr(loaded_testset.config, name).rime_options,
        )
        for name in ("generator", "judge", "validator")
    }
    return layout, expected


class _PlanningClient:
    def __init__(self, settings: ProblemSettings) -> None:
        self.edit = ProblemEditContent(1, "statement\n", True, True, settings)

    def get_problem_edit(self, problem_id: int) -> ProblemEditContent:
        return self.edit

    def get_generator(self, problem_id: int) -> GeneratorContent:
        return GeneratorContent("cpp20", "old generator\n", True, 3)

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent:
        return JudgeCodeContent("cpp20", "old judge\n", "AC")

    def get_validator(self, problem_id: int) -> ValidatorContent:
        return ValidatorContent("cpp20", "old validator\n", "AC")

    def statuses(self) -> list[StatusInfo]:
        return [StatusInfo("WJ", "judging")]


def test_push_preflight_and_plan_bundle_generator_judge_and_validator(tmp_path: Path) -> None:
    project, expected = _integration_project(tmp_path / "repo")
    problem = project.problems[0]
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (problem.path / "tests").glob("*.cpp")
    }

    local = push_module._preflight_local(
        problem,
        project.config,
        include_testcases=False,
        generate=False,
    )
    plan = push_module._build_remote_plan(
        local,
        _PlanningClient(_settings()),
        include_testcases=False,
        prune=False,
        generate=False,
    )

    assert plan.generator_request is not None
    assert plan.generator_request.source == expected["generator"]
    assert plan.judge_request is not None
    assert plan.judge_request.source == expected["judge"]
    assert plan.validator is not None
    assert plan.validator.request is not None
    assert plan.validator.request.source == expected["validator"]
    for path, state in before.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns) == state


class _RemoteClient:
    def __init__(self, expected: dict[str, str]) -> None:
        self.expected = expected

    def get_problem_edit(self, problem_id: int) -> ProblemEditContent:
        return ProblemEditContent(1, "statement\n", True, True, _settings())

    def get_generator(self, problem_id: int) -> GeneratorContent:
        return GeneratorContent("cpp20", self.expected["generator"], True, 3)

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent:
        return JudgeCodeContent("cpp20", self.expected["judge"], "AC")

    def get_validator(self, problem_id: int) -> ValidatorContent:
        return ValidatorContent("cpp20", self.expected["validator"], "AC")

    def get_editorial(self, problem_id: int) -> EditorialContent:
        return EditorialContent()


def test_expanded_remote_source_is_clean_for_diff_and_does_not_flatten_on_pull(
    tmp_path: Path,
) -> None:
    project, expected = _integration_project(tmp_path / "repo")
    client = _RemoteClient(expected)
    sources = tuple((project.root / "a" / "tests").glob("*.cpp"))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sources}

    result = sync_module.diff_remote(project, lambda _problem: client)
    assert not result.has_changes

    pulled = sync_module.pull(project, lambda _problem: client)
    assert not pulled.applied
    for path, state in before.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns) == state


def test_pull_rejects_divergent_remote_dependency_bundle_without_flattening(
    tmp_path: Path,
) -> None:
    project, expected = _integration_project(tmp_path / "repo")
    expected["generator"] += "// remote edit\n"
    client = _RemoteClient(expected)
    sources = tuple((project.root / "a" / "tests").glob("*.cpp"))
    before = {path: path.read_bytes() for path in sources}

    with pytest.raises(ConflictError, match="refusing to flatten"):
        sync_module.pull(project, lambda _problem: client)

    assert {path: path.read_bytes() for path in sources} == before


def test_pull_rejects_remote_include_when_local_dependency_is_unused(
    tmp_path: Path,
) -> None:
    project, expected = _integration_project(tmp_path / "repo")
    source = project.root / "a" / "tests" / "generator.cpp"
    source.write_text("int generator() { return 0; }\n", encoding="utf-8")
    expected["generator"] = '#include "shared.hpp"\nint generator() { return SHARED; }\n'

    with pytest.raises(ConflictError, match="refusing to flatten"):
        sync_module.pull(project, lambda _problem: _RemoteClient(expected))

    assert source.read_text(encoding="utf-8") == "int generator() { return 0; }\n"


def test_pull_normalizes_modular_source_without_flattening_it(tmp_path: Path) -> None:
    project, expected = _integration_project(tmp_path / "repo")
    source = project.root / "a" / "tests" / "generator.cpp"
    modular = source.read_text(encoding="utf-8")
    source.write_bytes(b"\xef\xbb\xbf" + modular.replace("\n", "\r\n").encode())

    pulled = sync_module.pull(project, lambda _problem: _RemoteClient(expected))

    assert pulled.applied
    assert source.read_bytes() == modular.encode()
    assert '#include "shared.hpp"' in source.read_text(encoding="utf-8")
    assert "BEGIN YUKITOOLS-RIME DEPENDENCY" not in source.read_text(encoding="utf-8")


def test_missing_dependency_fails_before_push_creates_client(tmp_path: Path) -> None:
    project, _ = _integration_project(tmp_path / "repo")
    (project.root / "common" / "nested.hpp").unlink()
    called = False

    def client_factory(_problem: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("client must not be created")

    with pytest.raises(FileOperationError, match="regular file"):
        push_module.push(project, client_factory)  # type: ignore[arg-type]
    assert not called
