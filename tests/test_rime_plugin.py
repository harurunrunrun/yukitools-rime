from __future__ import annotations

import os.path
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
            for suffix in ("generator", "judge", "validator"):
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
    testset.problem = SimpleNamespace(judge_type=0)
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
    testset.exports["yukicoder_validator"](
        lang_id="cpp20",
        src="validator.cpp",
        rime_kind="cxx",
        rime_options={"flags": ["-Wall"]},
    )
    assert testset.calls == [
        ("cxx_generator", ("gen.cpp",), {"flags": ["-O2"]}),
        ("cxx_validator", ("validator.cpp",), {"flags": ["-Wall"]}),
    ]
    solution = registry.classes["Solution"]()
    solution.PreLoad(None)
    solution.exports["yukicoder_solution"](
        lang_id="cpp20",
        src="main.cpp",
        rime_kind="cxx",
        challenge_cases=[],
        submission_id=321,
    )
    assert solution.calls == [("cxx_solution", ("main.cpp",), {"challenge_cases": None})]
    assert solution.yukicoder_config.submission_id == 321


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


def test_unknown_validator_kind_is_loaded_for_sync_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    testset = registry.classes["Testset"]()
    testset.problem = SimpleNamespace(judge_type=0)
    testset.PreLoad(None)
    testset.exports["yukicoder_validator"](
        lang_id="remote-only", src="validator.txt", rime_kind=None
    )
    assert testset.calls == []
    assert testset.yukicoder_validator_config.src == "validator.txt"


def test_sync_only_judge_rejects_custom_problem_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    testset = registry.classes["Testset"]()
    testset.problem = SimpleNamespace(judge_type=1)
    testset.PreLoad(None)

    with pytest.raises(RuntimeError, match="sync-only judge"):
        testset.exports["yukicoder_judge"](
            lang_id="remote-only",
            src="judge.txt",
            rime_kind=None,
        )


def test_custom_problem_without_any_judge_is_rejected_before_internal_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    testset = registry.classes["Testset"]()
    testset.problem = SimpleNamespace(judge_type=1)
    testset.judges = []
    testset.PreLoad(None)

    with pytest.raises(RuntimeError, match="requires a Rime judge"):
        testset.PostLoad(None)


def test_standard_testset_without_managed_problem_allows_internal_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    testset = registry.classes["Testset"]()
    testset.problem = SimpleNamespace()
    testset.judges = []
    testset.PreLoad(None)

    testset.PostLoad(None)


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
    assert target.out_dir == os.path.join("/work/problem-a", "generated")


@pytest.mark.parametrize("kind", ["c", "cxx", "java", "kotlin", "rust", "go", "script"])
def test_every_supported_rime_kind_is_delegated(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    registry = installed(monkeypatch)
    testset = registry.classes["Testset"]()
    testset.problem = SimpleNamespace(judge_type=1)
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
    testset.exports["yukicoder_validator"](
        lang_id="remote",
        src="validator.src",
        rime_kind=kind,
    )
    assert [call[0] for call in testset.calls] == [
        f"{kind}_generator",
        f"{kind}_judge",
        f"{kind}_validator",
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


PROBLEM_ARGS: dict[str, object] = {
    "problem_id": 10,
    "title": "A",
    "tags": "",
    "level": 1,
    "time_limit_ms": 1000,
    "memory_limit": 256,
    "eps_mode": "-",
    "eps": "0",
    "wip": False,
    "recruiting_tester": False,
    "problem_type": 0,
    "judge_type": 0,
    "rime_id": "A",
}


def test_public_dsl_wrappers_forward_defaults_to_active_handlers() -> None:
    captured: dict[str, dict[str, object]] = {}

    def capture(kind: str):
        def handler(**kwargs: object) -> None:
            captured[kind] = kwargs

        return handler

    rime_plugin._active.clear()
    for kind in (
        "yukicoder_problem",
        "yukicoder_generator",
        "yukicoder_judge",
        "yukicoder_solution",
    ):
        rime_plugin._active[kind] = capture(kind)
    try:
        rime_plugin.yukicoder_problem(**PROBLEM_ARGS)  # type: ignore[arg-type]
        rime_plugin.yukicoder_generator(lang_id="cpp", src="gen.cpp", test_case_num=1)
        rime_plugin.yukicoder_judge(lang_id="cpp", src="judge.cpp")
        rime_plugin.yukicoder_solution(lang_id="cpp", src="main.cpp")
    finally:
        rime_plugin._active.clear()

    assert captured["yukicoder_problem"]["rime_options"] == {}
    assert captured["yukicoder_generator"]["prefix"] is None
    assert captured["yukicoder_generator"]["rime_options"] == {}
    assert captured["yukicoder_judge"]["rime_options"] == {}
    assert captured["yukicoder_solution"]["challenge_cases"] == ()
    assert captured["yukicoder_solution"]["rime_options"] == {}
    assert captured["yukicoder_solution"]["submission_id"] is None


def test_is_within_handles_incompatible_path_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_commonpath(paths: object) -> str:
        raise ValueError("different drives")

    monkeypatch.setattr(rime_plugin.os.path, "commonpath", fail_commonpath)
    assert rime_plugin._is_within("C:\\root", "D:\\other") is False


def test_regular_source_validation_accepts_missing_rime_metadata_and_valid_file(
    tmp_path: Path,
) -> None:
    rime_plugin._validate_regular_source(SimpleNamespace(), "remote.txt")

    source_dir = tmp_path / "problem" / "solution"
    source_dir.mkdir(parents=True)
    source = source_dir / "main.cpp"
    source.write_text("code", encoding="utf-8")
    target = SimpleNamespace(
        src_dir=str(source_dir),
        problem=SimpleNamespace(base_dir=str(tmp_path / "problem")),
    )
    rime_plugin._validate_regular_source(target, "main.cpp")


def test_regular_source_validation_rejects_source_directory_escape(tmp_path: Path) -> None:
    problem = tmp_path / "problem"
    problem.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main.cpp").write_text("code", encoding="utf-8")
    target = SimpleNamespace(
        src_dir=str(outside),
        problem=SimpleNamespace(base_dir=str(problem)),
    )

    with pytest.raises(RuntimeError, match="source directory is unsafe"):
        rime_plugin._validate_regular_source(target, "main.cpp")


def test_problem_output_rejects_regular_file_and_synthetic_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem = tmp_path / "problem"
    problem.mkdir()
    output = problem / "rime-out"
    output.write_bytes(b"file")

    with pytest.raises(RuntimeError, match="must be a directory"):
        rime_plugin._validated_problem_output(str(problem), ProjectConfig())

    output.unlink()
    monkeypatch.setattr(rime_plugin, "_is_within", lambda root, candidate: False)
    with pytest.raises(RuntimeError, match="escapes problem root"):
        rime_plugin._validated_problem_output(str(problem), ProjectConfig())


def test_component_output_validation_handles_missing_file_and_escape(tmp_path: Path) -> None:
    rime_plugin._validate_component_output(SimpleNamespace())

    root = tmp_path / "out"
    root.mkdir()
    component = root / "tests"
    component.write_bytes(b"file")
    target = SimpleNamespace(
        out_dir=str(component),
        problem=SimpleNamespace(out_dir=str(root)),
    )
    with pytest.raises(RuntimeError, match="must be a directory"):
        rime_plugin._validate_component_output(target)

    outside = tmp_path / "outside"
    target.out_dir = str(outside)
    with pytest.raises(RuntimeError, match="unsafe"):
        rime_plugin._validate_component_output(target)


def test_register_code_rejects_missing_directive_and_reserved_solution_option() -> None:
    generator = rime_plugin.GeneratorConfig("cpp", "gen.cpp", 1, rime_kind="cxx")
    with pytest.raises(RuntimeError, match="not available"):
        rime_plugin._register_code(SimpleNamespace(exports={}), generator, "generator")

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def directive(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    solution = rime_plugin.SolutionConfig(
        "cpp",
        "main.cpp",
        rime_kind="cxx",
        challenge_cases=["sample"],
        rime_options={"challenge_cases": ["bad"]},
    )
    with pytest.raises(RuntimeError, match="must not contain challenge_cases"):
        rime_plugin._register_code(
            SimpleNamespace(exports={"cxx_solution": directive}),
            solution,
            "solution",
            solution=True,
        )

    solution = rime_plugin.SolutionConfig(
        "cpp", "main.cpp", rime_kind="cxx", challenge_cases=["sample"]
    )
    rime_plugin._register_code(
        SimpleNamespace(exports={"cxx_solution": directive}),
        solution,
        "solution",
        solution=True,
    )
    assert calls == [(("main.cpp",), {"challenge_cases": ["sample"]})]


def test_project_adapter_rejects_missing_and_duplicate_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    missing = registry.classes["Project"]()
    missing.PreLoad(None)
    with pytest.raises(RuntimeError, match="declaration is missing"):
        missing.PostLoad(None)

    duplicate = registry.classes["Project"]()
    duplicate.PreLoad(None)
    duplicate.exports["yukicoder_project"]()
    with pytest.raises(RuntimeError, match="multiple"):
        duplicate.exports["yukicoder_project"]()
    duplicate.PostLoad(None)


def test_problem_adapter_rejects_duplicate_and_rime_option_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    target = registry.classes["Problem"]()
    target.PreLoad(None)
    target.exports["yukicoder_problem"](**PROBLEM_ARGS)
    with pytest.raises(RuntimeError, match="multiple"):
        target.exports["yukicoder_problem"](**PROBLEM_ARGS)
    target.PostLoad(None)

    target = registry.classes["Problem"]()
    target.PreLoad(None)
    conflicting = dict(PROBLEM_ARGS)
    conflicting["rime_options"] = {"time_limit": 2}
    with pytest.raises(RuntimeError, match="conflicts"):
        target.exports["yukicoder_problem"](**conflicting)
    target.PostLoad(None)


def test_problem_postload_removes_sync_only_solutions_and_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    target = registry.classes["Problem"]()
    target.PreLoad(None)
    target.yukicoder_config = object()
    sync_only = SimpleNamespace(yukicoder_sync_only=True)
    normal = SimpleNamespace(yukicoder_sync_only=False)
    target.solutions = [sync_only, normal]
    target.reference_solution = sync_only
    errors: list[tuple[object, str]] = []
    ui = SimpleNamespace(
        errors=SimpleNamespace(Error=lambda owner, message: errors.append((owner, message)))
    )

    target.PostLoad(ui)

    assert target.solutions == [normal]
    assert target.reference_solution is None
    assert errors and "sync-only" in errors[0][1]


def test_problem_postload_without_solution_list_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    target = registry.classes["Problem"]()
    target.PreLoad(None)
    target.yukicoder_config = object()
    target.PostLoad(None)


def test_testset_rejects_duplicates_and_shared_source_in_both_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = installed(monkeypatch)
    target = registry.classes["Testset"]()
    target.problem = SimpleNamespace(judge_type=1)
    target.PreLoad(None)
    target.exports["yukicoder_generator"](
        lang_id="cpp", src="same.cpp", test_case_num=1, rime_kind="cxx"
    )
    with pytest.raises(RuntimeError, match="multiple"):
        target.exports["yukicoder_generator"](
            lang_id="cpp", src="other.cpp", test_case_num=1, rime_kind="cxx"
        )
    with pytest.raises(RuntimeError, match="must be distinct"):
        target.exports["yukicoder_judge"](lang_id="cpp", src="same.cpp", rime_kind="cxx")
    target.PostLoad(None)

    target = registry.classes["Testset"]()
    target.problem = SimpleNamespace(judge_type=1)
    target.PreLoad(None)
    target.exports["yukicoder_judge"](lang_id="cpp", src="same.cpp", rime_kind="cxx")
    with pytest.raises(RuntimeError, match="multiple"):
        target.exports["yukicoder_judge"](lang_id="cpp", src="other.cpp", rime_kind="cxx")
    with pytest.raises(RuntimeError, match="must be distinct"):
        target.exports["yukicoder_generator"](
            lang_id="cpp", src="same.cpp", test_case_num=1, rime_kind="cxx"
        )
    target.PostLoad(None)


def test_solution_duplicate_sync_challenges_and_normal_correctness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Solution, "IsCorrect", lambda self: True, raising=False)
    registry = installed(monkeypatch)

    sync_only = registry.classes["Solution"]()
    sync_only._codes = []
    sync_only.PreLoad(None)
    sync_only.exports["yukicoder_solution"](
        lang_id="remote", src="main.txt", rime_kind=None, challenge_cases=["sample"]
    )
    with pytest.raises(RuntimeError, match="multiple"):
        sync_only.exports["yukicoder_solution"](lang_id="remote", src="other.txt", rime_kind=None)
    sync_only.PostLoad(None)
    assert sync_only.challenge_cases == ["sample"]
    assert sync_only.IsCorrect() is False

    normal = registry.classes["Solution"]()
    normal.PreLoad(None)
    normal.yukicoder_sync_only = False
    assert normal.IsCorrect() is True
    normal.PostLoad(None)


def test_install_rejects_missing_basic_registry_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry()
    del registry.classes["Solution"]
    monkeypatch.setattr(
        rime_plugin.importlib,
        "import_module",
        lambda name: SimpleNamespace(
            registry=registry,
            ReloadConfiguration=ReloadConfiguration,
        ),
    )

    with pytest.raises(RuntimeError, match=r"load rime\.basic first"):
        rime_plugin.install()
