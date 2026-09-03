from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from yukitools_rime.api import GeneratorContent, JudgeCodeContent, ProblemEditContent
from yukitools_rime.commands.scaffold import init_project, new_problem
from yukitools_rime.errors import ConfigError, ConflictError, ValidationError
from yukitools_rime.models import ProblemSettings, ProjectConfig, Which
from yukitools_rime.rime_config import (
    parse_problem_config,
    parse_project_config,
    parse_testset_config,
    render_project_block,
)


def settings() -> ProblemSettings:
    return ProblemSettings(
        title="A + B",
        tags="math implementation",
        level=1.5,
        time_limit_ms=2000,
        memory_limit=512,
        eps_mode="-",
        eps="0",
        wip=True,
        recruiting_tester=False,
        problem_type=0,
        judge_type=1,
        show_ans=False,
        enable_pure_judge=False,
        force_single_server_judge=False,
        allowed_langs=[],
    )


class FakeClient:
    def __init__(
        self,
        *,
        problem_id: int = 42,
        generator: GeneratorContent | None = None,
        judge: JudgeCodeContent | None = None,
        fail_generator: bool = False,
        cases: dict[str, tuple[bytes, bytes]] | None = None,
    ) -> None:
        self.problem_id = problem_id
        self.generator = generator or GeneratorContent()
        self.judge = judge
        self.fail_generator = fail_generator
        self.cases = cases or {}
        self.calls: list[str] = []
        self.closed = False

    def get_problem_edit(self, problem_id: int) -> ProblemEditContent:
        self.calls.append("problem")
        return ProblemEditContent(
            problem_id=self.problem_id,
            content="# Statement\r\n",
            is_markdown=True,
            showable=False,
            settings=settings(),
        )

    def get_generator(self, problem_id: int) -> GeneratorContent:
        self.calls.append("generator")
        if self.fail_generator:
            raise RuntimeError("remote generator failed")
        return self.generator

    def get_judge_code(self, problem_id: int) -> JudgeCodeContent | None:
        self.calls.append("judge")
        return self.judge

    def list_testcases(self, problem_id: int, which: Which | str) -> list[str]:
        side = which.value if isinstance(which, Which) else which
        self.calls.append(f"list-{side}")
        return sorted(self.cases)

    def get_testcase(self, problem_id: int, which: Which | str, name: str) -> bytes:
        side = which.value if isinstance(which, Which) else which
        self.calls.append(f"get-{side}-{name}")
        pair = self.cases[name]
        return pair[0] if side == "in" else pair[1]

    def upload_testcases(
        self, problem_id: int, which: Which | str, files: dict[str, bytes]
    ) -> object:
        raise AssertionError("new never uploads testcases")

    def delete_testcase(self, problem_id: int, which: Which | str, name: str) -> None:
        raise AssertionError("new never deletes testcases")

    def close(self) -> None:
        self.closed = True


def factory_for(
    client: FakeClient,
    seen: list[tuple[Path, ProjectConfig, int]] | None = None,
) -> Callable[[Path, ProjectConfig, int], FakeClient]:
    def factory(root: Path, config: ProjectConfig, problem_id: int) -> FakeClient:
        if seen is not None:
            seen.append((root, config, problem_id))
        return client

    return factory


def test_init_creates_idempotent_project_without_git_or_api(tmp_path: Path) -> None:
    root = tmp_path / "new-project"
    project = init_project(root)

    assert project.root == root.resolve()
    assert not (root / ".git").exists()
    assert parse_project_config((root / "PROJECT").read_text()).base_url.endswith("/api")
    ignore = (root / ".gitignore").read_text()
    assert ".env\n" in ignore
    assert "rime-out/\n" in ignore
    assert (root / ".env.example").is_file()

    snapshot = {
        path.name: path.read_bytes()
        for path in (root / "PROJECT", root / ".gitignore", root / ".env.example")
    }
    init_project(root)
    assert snapshot == {
        path.name: path.read_bytes()
        for path in (root / "PROJECT", root / ".gitignore", root / ".env.example")
    }


def test_init_uses_plain_text_markers_for_gitignore(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    original = "continued-pattern\\\n"
    (root / ".gitignore").write_text(original, encoding="utf-8")

    init_project(root)
    init_project(root)

    updated = (root / ".gitignore").read_text(encoding="utf-8")
    assert updated.startswith(original)
    assert updated.count("# BEGIN YUKITOOLS-RIME") == 1


def test_init_preserves_existing_content_values_and_env_example(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    custom = ProjectConfig(
        base_url="https://mirror.example/api",
        rime_out_dir="generated",
    )
    (root / "PROJECT").write_text(
        "use_plugin('rime_plus')\n\n" + render_project_block(custom),
        encoding="utf-8",
    )
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    (root / ".env.example").write_text("KEEP=me\n", encoding="utf-8")

    project = init_project(root)

    assert project.config == custom
    assert (root / "PROJECT").read_text().startswith("use_plugin('rime_plus')")
    assert (root / ".gitignore").read_text().startswith("build/\n")
    assert (root / ".env.example").read_text() == "KEEP=me\n"


def test_new_problem_writes_rime_layout_and_remote_sources(tmp_path: Path) -> None:
    root = tmp_path / "project"
    init_project(root)
    client = FakeClient(
        generator=GeneratorContent(
            lang_id="cpp23",
            source="#include <iostream>\r\n",
            enable=True,
            test_case_num=10,
        ),
        judge=JudgeCodeContent(
            lang_id="unusual-lang",
            source="judge source\r\n",
            status="AC",
        ),
    )
    seen: list[tuple[Path, ProjectConfig, int]] = []

    result = new_problem(
        42,
        root,
        dir_name="addition",
        client_factory=factory_for(client, seen),
    )

    problem_dir = root / "addition"
    assert result.path == problem_dir.resolve()
    assert client.calls == ["problem", "generator", "judge"]
    assert client.closed
    assert seen[0][0] == root.resolve()
    assert seen[0][2] == 42
    problem = parse_problem_config((problem_dir / "PROBLEM").read_text())
    assert problem.problem_id == 42
    assert problem.rime_id == "addition"
    assert (problem_dir / "statement.md").read_text() == "# Statement\n"
    testset = parse_testset_config((problem_dir / "tests" / "TESTSET").read_text())
    assert testset.generator is not None
    assert testset.generator.src == "generator.cpp"
    assert testset.generator.rime_kind == "cxx"
    assert (problem_dir / "tests" / "generator.cpp").read_text().endswith("\n")
    assert testset.judge is not None
    assert testset.judge.src == "judge.txt"
    assert testset.judge.rime_kind is None
    assert not (problem_dir / "editorial.md").exists()
    assert not (problem_dir / "editorial.html").exists()


def test_new_problem_omits_unregistered_remote_sources(tmp_path: Path) -> None:
    root = tmp_path / "project"
    init_project(root)
    client = FakeClient(generator=GeneratorContent(source="  "), judge=None)

    new_problem(42, root, client_factory=factory_for(client))

    testset = parse_testset_config((root / "42" / "tests" / "TESTSET").read_text())
    assert testset.generator is None
    assert testset.judge is None
    assert sorted(path.name for path in (root / "42" / "tests").iterdir()) == ["TESTSET"]


def test_new_problem_fetches_testcases_into_ignored_rime_output(tmp_path: Path) -> None:
    root = tmp_path / "project"
    init_project(root)
    client = FakeClient(cases={"sample.01.txt": (b"1 2\n", b"3\n")})

    new_problem(
        42,
        root,
        include_testcases=True,
        client_factory=factory_for(client),
    )

    output = root / "42" / "rime-out" / "tests"
    assert (output / "sample.01.txt.in").read_bytes() == b"1 2\n"
    assert (output / "sample.01.txt.diff").read_bytes() == b"3\n"
    assert client.calls == [
        "problem",
        "generator",
        "judge",
        "list-in",
        "list-out",
        "get-in-sample.01.txt",
        "get-out-sample.01.txt",
    ]


def test_remote_failure_leaves_no_problem_or_stage(tmp_path: Path) -> None:
    root = tmp_path / "project"
    init_project(root)
    client = FakeClient(fail_generator=True)

    with pytest.raises(RuntimeError, match="remote generator failed"):
        new_problem(
            42,
            root,
            dir_name="failed",
            client_factory=factory_for(client),
        )

    assert client.closed
    assert not (root / "failed").exists()
    assert not list(root.glob(".failed.stage-*"))


def test_mismatched_remote_id_leaves_no_problem(tmp_path: Path) -> None:
    root = tmp_path / "project"
    init_project(root)
    client = FakeClient(problem_id=99)
    with pytest.raises(ValidationError, match="API returned 99"):
        new_problem(42, root, client_factory=factory_for(client))
    assert not (root / "42").exists()


@pytest.mark.parametrize("name", ["", "../escape", "nested/problem", "/absolute"])
def test_new_problem_rejects_non_child_directory_names(tmp_path: Path, name: str) -> None:
    root = tmp_path / "project"
    init_project(root)
    called = False

    def factory(root: Path, config: ProjectConfig, problem_id: int) -> FakeClient:
        nonlocal called
        called = True
        return FakeClient()

    with pytest.raises(ValidationError):
        new_problem(42, root, dir_name=name, client_factory=factory)
    assert not called


def test_new_problem_rejects_existing_directory_and_duplicate_id(tmp_path: Path) -> None:
    root = tmp_path / "project"
    init_project(root)
    (root / "occupied").mkdir()
    with pytest.raises(ConflictError):
        new_problem(
            42,
            root,
            dir_name="occupied",
            client_factory=factory_for(FakeClient()),
        )

    new_problem(42, root, dir_name="first", client_factory=factory_for(FakeClient()))
    with pytest.raises(ConflictError, match="already exists"):
        new_problem(42, root, dir_name="second", client_factory=factory_for(FakeClient()))


def test_init_preserves_project_bom_crlf_and_custom_output_ignore(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    custom = ProjectConfig(
        base_url="https://mirror.example/api",
        rime_out_dir="generated",
    )
    source = "\ufeffuse_plugin('rime_plus')\r\n\r\n" + render_project_block(custom).replace(
        "\n", "\r\n"
    )
    (root / "PROJECT").write_bytes(source.encode("utf-8"))

    project = init_project(root)

    assert project.config == custom
    assert (root / "PROJECT").read_bytes() == source.encode("utf-8")
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert "generated/\n" in ignore
    assert "rime-out/\n" not in ignore


def test_init_rejects_unmanaged_declaration_without_partial_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project_file = root / "PROJECT"
    original = b"yukicoder_project()\n"
    project_file.write_bytes(original)

    with pytest.raises(ConfigError, match="outside a managed"):
        init_project(root)

    assert project_file.read_bytes() == original
    assert not (root / ".gitignore").exists()
    assert not (root / ".env.example").exists()


def test_init_ignore_rules_hide_credentials_and_generated_cases_from_git(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    init_project(root)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    baseline = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    (root / ".env").write_text("YUKICODER_TOKEN=secret\n", encoding="utf-8")
    case_dir = root / "a" / "rime-out" / "tests"
    case_dir.mkdir(parents=True)
    (case_dir / "sample.in").write_bytes(b"input")
    (case_dir / "sample.diff").write_bytes(b"output")
    current = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert current == baseline
