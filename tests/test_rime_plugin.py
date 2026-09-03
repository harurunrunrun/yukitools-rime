from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yukitools_rime import rime_plugin
from yukitools_rime.models import ProjectConfig


class ReloadConfiguration(Exception):
    pass


class Registry:
    def __init__(self) -> None:
        self.classes = {
            "Project": Project,
            "Problem": Problem,
            "Testset": RimeTestset,
            "Solution": Solution,
        }

    def Get(self, name: str) -> type[Any] | None:
        return self.classes.get(name)

    def Override(self, name: str, value: type[Any]) -> None:
        assert issubclass(value, self.classes[name])
        self.classes[name] = value


class Base:
    def __init__(self) -> None:
        self.exports: dict[str, Any] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def PreLoad(self, ui: object) -> None:
        pass

    def PostLoad(self, ui: object) -> None:
        pass


class Project(Base):
    pass


class Problem(Base):
    def PreLoad(self, ui: object) -> None:
        super().PreLoad(ui)

        def problem(*args: Any, **kwargs: Any) -> None:
            self.calls.append(("problem", args, kwargs))

        self.exports["problem"] = problem


class RimeTestset(Base):
    def PreLoad(self, ui: object) -> None:
        super().PreLoad(ui)
        for kind in ("c", "cxx", "java", "kotlin", "rust", "go", "script"):
            for suffix in ("generator", "judge"):
                name = f"{kind}_{suffix}"

                def directive(*args: Any, _name: str = name, **kwargs: Any) -> None:
                    self.calls.append((_name, args, kwargs))

                self.exports[name] = directive


class Solution(Base):
    def PreLoad(self, ui: object) -> None:
        super().PreLoad(ui)
        for kind in ("c", "cxx", "java", "kotlin", "rust", "go", "script"):
            name = f"{kind}_solution"

            def solution(*args: Any, _name: str = name, **kwargs: Any) -> None:
                self.calls.append((_name, args, kwargs))

            self.exports[name] = solution


def installed(monkeypatch: pytest.MonkeyPatch) -> Registry:
    registry = Registry()
    monkeypatch.setattr(
        rime_plugin.importlib,
        "import_module",
        lambda name: SimpleNamespace(
            registry=registry,
            ReloadConfiguration=ReloadConfiguration,
        ),
    )
    with pytest.raises(ReloadConfiguration):
        rime_plugin.install()
    rime_plugin.install()
    return registry


def test_install_is_lazy_and_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = installed(monkeypatch)
    classes = dict(registry.classes)
    rime_plugin.install()
    assert registry.classes == classes


def test_project_imported_function_reaches_loading_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    target = registry.classes["Project"]()
    target.PreLoad(None)
    rime_plugin.yukicoder_project(base_url="https://example.test/api")
    assert target.yukicoder_config.base_url == "https://example.test/api"
    target.PostLoad(None)


def test_problem_delegates_to_standard_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = installed(monkeypatch)
    target = registry.classes["Problem"]()
    target.PreLoad(None)
    target.exports["yukicoder_problem"](
        problem_id=10,
        title="A",
        tags="",
        level=1,
        time_limit_ms=2500,
        memory_limit=256,
        eps_mode="-",
        eps="0",
        wip=False,
        recruiting_tester=False,
        problem_type=0,
        judge_type=1,
        rime_id="A",
        reference_solution="correct",
        rime_options={"custom": True},
    )
    _, _, kwargs = target.calls[0]
    assert kwargs["time_limit"] == 2.5
    assert kwargs["id"] == "A"
    assert kwargs["custom"] is True
    assert target.custom_judge is True


def test_program_wrappers_delegate_by_rime_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = installed(monkeypatch)
    testset = registry.classes["Testset"]()
    testset.PreLoad(None)
    testset.exports["yukicoder_generator"](
        lang_id="cpp20",
        src="gen.cpp",
        test_case_num=3,
        rime_kind="cxx",
        rime_options={"flags": ["-O2"]},
    )
    testset.exports["yukicoder_judge"](
        lang_id="cpp20",
        src="judge.cpp",
        rime_kind=None,
    )
    assert testset.calls == [("cxx_generator", ("gen.cpp",), {"flags": ["-O2"]})]
    solution = registry.classes["Solution"]()
    solution.PreLoad(None)
    solution.exports["yukicoder_solution"](
        lang_id="cpp20",
        src="main.cpp",
        rime_kind="cxx",
        challenge_cases=[],
    )
    assert solution.calls == [("cxx_solution", ("main.cpp",), {"challenge_cases": None})]


def test_normal_judge_source_is_sync_only(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = installed(monkeypatch)
    testset = registry.classes["Testset"]()
    testset.problem = SimpleNamespace(judge_type=0)
    testset.PreLoad(None)

    testset.exports["yukicoder_judge"](
        lang_id="cpp20",
        src="judge.cpp",
        rime_kind="cxx",
    )

    assert testset.calls == []
    assert testset.yukicoder_judge_config.src == "judge.cpp"


def test_module_function_outside_config_load_is_rejected() -> None:
    rime_plugin._active.clear()
    with pytest.raises(RuntimeError, match="only available"):
        rime_plugin.yukicoder_project()


def test_problem_uses_configured_rime_output_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    target = registry.classes["Problem"]()
    target.base_dir = "/work/problem-a"
    target.project = SimpleNamespace(yukicoder_config=ProjectConfig(rime_out_dir="generated"))
    target.PreLoad(None)
    assert target.out_dir == "/work/problem-a/generated"


@pytest.mark.parametrize("kind", ["c", "cxx", "java", "kotlin", "rust", "go", "script"])
def test_every_supported_rime_kind_is_delegated(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    registry = installed(monkeypatch)
    testset = registry.classes["Testset"]()
    testset.PreLoad(None)
    testset.exports["yukicoder_generator"](
        lang_id="remote",
        src="generator.src",
        test_case_num=1,
        rime_kind=kind,
    )
    testset.exports["yukicoder_judge"](
        lang_id="remote",
        src="judge.src",
        rime_kind=kind,
    )
    assert [call[0] for call in testset.calls] == [
        f"{kind}_generator",
        f"{kind}_judge",
    ]

    solution = registry.classes["Solution"]()
    solution.PreLoad(None)
    solution.exports["yukicoder_solution"](lang_id="remote", src="main.src", rime_kind=kind)
    assert solution.calls[0][0] == f"{kind}_solution"


def test_unknown_solution_kind_is_loaded_as_sync_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    solution = registry.classes["Solution"]()
    solution._codes = []
    solution.PreLoad(None)
    solution.exports["yukicoder_solution"](lang_id="unknown", src="main.txt", rime_kind=None)
    solution.PostLoad(None)
    assert solution.yukicoder_sync_only is True
    assert len(solution._codes) == 1
    assert solution.IsCorrect() is False


def test_standard_problem_and_solution_declarations_remain_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    problem = registry.classes["Problem"]()
    problem.PreLoad(None)
    problem.exports["problem"](time_limit=1.0, id="plain")
    problem.PostLoad(None)
    assert problem.yukicoder_config is None
    assert problem.calls == [("problem", (), {"time_limit": 1.0, "id": "plain"})]

    solution = registry.classes["Solution"]()
    solution.PreLoad(None)
    solution.exports["cxx_solution"]("main.cpp")
    solution.PostLoad(None)
    assert solution.yukicoder_config is None
    assert solution.calls == [("cxx_solution", ("main.cpp",), {})]


def test_problem_output_symlink_and_source_collision_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    project_root = tmp_path / "project"
    problem_root = project_root / "a"
    outside = tmp_path / "outside"
    problem_root.mkdir(parents=True)
    outside.mkdir()
    output = problem_root / "generated"
    try:
        output.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    target = registry.classes["Problem"]()
    target.base_dir = str(problem_root)
    target.project = SimpleNamespace(
        base_dir=str(project_root),
        yukicoder_config=ProjectConfig(rime_out_dir="generated"),
    )
    with pytest.raises(RuntimeError, match="symlink"):
        target.PreLoad(None)

    output.unlink()
    output.mkdir()
    (output / "TESTSET").write_text("", encoding="utf-8")
    target = registry.classes["Problem"]()
    target.base_dir = str(problem_root)
    target.project = SimpleNamespace(
        base_dir=str(project_root),
        yukicoder_config=ProjectConfig(rime_out_dir="generated"),
    )
    with pytest.raises(RuntimeError, match="collides"):
        target.PreLoad(None)


def test_symlink_problem_cannot_escape_runtime_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside_problem = tmp_path / "outside-problem"
    outside_problem.mkdir()
    linked_problem = project_root / "linked"
    try:
        linked_problem.symlink_to(outside_problem, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    target = registry.classes["Problem"]()
    target.base_dir = str(linked_problem)
    target.project = SimpleNamespace(
        base_dir=str(project_root),
        yukicoder_config=ProjectConfig(),
    )

    with pytest.raises(RuntimeError, match=r"unsafe|escape|symlink"):
        target.PreLoad(None)


def test_component_output_symlink_is_rejected_after_base_preload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    problem_root = tmp_path / "problem"
    source_dir = problem_root / "tests"
    output_root = problem_root / "rime-out"
    outside = tmp_path / "outside"
    source_dir.mkdir(parents=True)
    output_root.mkdir()
    outside.mkdir()
    output = output_root / "tests"
    try:
        output.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    target = registry.classes["Testset"]()
    target.problem = SimpleNamespace(
        base_dir=str(problem_root),
        out_dir=str(output_root),
    )
    target.src_dir = str(source_dir)
    target.out_dir = str(output)

    with pytest.raises(RuntimeError, match="output directory is unsafe"):
        target.PreLoad(None)


@pytest.mark.parametrize(
    ("target_name", "declaration", "arguments"),
    [
        (
            "Testset",
            "yukicoder_generator",
            {
                "lang_id": "cpp23",
                "src": "source.cpp",
                "test_case_num": 1,
                "rime_kind": "cxx",
            },
        ),
        (
            "Testset",
            "yukicoder_judge",
            {
                "lang_id": "cpp23",
                "src": "source.cpp",
                "rime_kind": "cxx",
            },
        ),
        (
            "Solution",
            "yukicoder_solution",
            {
                "lang_id": "remote-only",
                "src": "source.cpp",
                "rime_kind": None,
            },
        ),
    ],
)
def test_runtime_declarations_reject_source_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    declaration: str,
    arguments: dict[str, object],
) -> None:
    registry = installed(monkeypatch)
    problem_root = tmp_path / "problem"
    source_dir = problem_root / target_name.lower()
    output_root = problem_root / "rime-out"
    outside = tmp_path / "outside.cpp"
    source_dir.mkdir(parents=True)
    output_root.mkdir()
    outside.write_text("outside", encoding="utf-8")
    try:
        (source_dir / "source.cpp").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    target = registry.classes[target_name]()
    target.project = SimpleNamespace(base_dir=str(tmp_path))
    target.problem = SimpleNamespace(
        base_dir=str(problem_root),
        out_dir=str(output_root),
    )
    target.src_dir = str(source_dir)
    target.out_dir = str(output_root / target_name.lower())
    target.PreLoad(None)

    with pytest.raises(RuntimeError, match="direct regular file"):
        target.exports[declaration](**arguments)
