from __future__ import annotations

import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from yukitools_rime.layout import load_project, read_testcases
from yukitools_rime.subtasks import read_subtasks

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample"


def run_script(path: Path, *args: str, data: str = "", cwd: Path | None = None):
    # Binary stdin avoids Windows TextIOWrapper translating testcase newlines.
    result = subprocess.run(
        [sys.executable, str(path), *args],
        input=data.encode("utf-8"),
        cwd=cwd,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8").replace("\r\n", "\n"),
        result.stderr.decode("utf-8").replace("\r\n", "\n"),
    )


def load_interactor():
    spec = importlib.util.spec_from_file_location(
        "_sample_interactor", SAMPLE / "interactive" / "tests" / "judge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def statement_samples(name: str) -> list[tuple[str, str]]:
    source = (SAMPLE / name / "statement.md").read_text(encoding="utf-8")
    return re.findall(
        r"@in 入力\s+<pre>(.*?)</pre>\s+@out 出力\s+<pre>(.*?)</pre>",
        source,
        re.DOTALL,
    )


@pytest.mark.parametrize("name", ["normal", "special", "interactive"])
def test_sample_statements_follow_yukicoder_markup(name: str) -> None:
    source = (SAMPLE / name / "statement.md").read_text(encoding="utf-8")
    assert source.startswith("## 問題文\n")
    for marker in ("## @input 入力", "<subtask />", "## 出力", "## @samples サンプル"):
        assert source.count(marker) == 1
    for index in range(1, 4):
        assert f"@sample サンプル{index}\n" in source
    assert "改行" in source
    assert "```" not in source
    # Only the intended HTML tags may contain a literal '<'; math uses \\lt/\\gt.
    assert re.search(r"<(?!/?pre>|subtask />)", source) is None
    assert len(statement_samples(name)) == 3


def test_normal_statement_samples_match_reference_solution() -> None:
    for testcase, expected in statement_samples("normal"):
        result = run_script(SAMPLE / "normal" / "solution" / "main.py", data=testcase)
        assert result.returncode == 0 and result.stdout == expected


def test_special_statement_samples_are_accepted(tmp_path: Path) -> None:
    for index, (testcase, answer) in enumerate(statement_samples("special")):
        case = tmp_path / f"sample_{index}.in"
        case.write_text(testcase, encoding="utf-8", newline="\n")
        result = run_script(
            SAMPLE / "special" / "tests" / "judge.py",
            str(case),
            "unused-answer",
            "unused-code",
            "unused-score",
            data=answer,
        )
        assert result.returncode == 0


def test_interactive_statement_samples_are_valid_dialogues() -> None:
    for secret, (judge_lines, answer_lines) in zip(
        (42, 1, 100), statement_samples("interactive"), strict=True
    ):
        output = io.StringIO()
        assert load_interactor().interact(secret, io.StringIO(answer_lines), output)
        assert output.getvalue() == judge_lines


def test_sample_layout_is_offline_and_contains_no_generated_cases() -> None:
    project = load_project(SAMPLE)
    assert {p.path.name: p.config.settings.judge_type for p in project.problems} == {
        "normal": 0,
        "special": 1,
        "interactive": 2,
    }
    assert project.sync_problems == ()
    for problem in project.problems:
        assert problem.config.reference_solution == "solution"
        assert problem.testset is not None
        assert problem.testset.config.validator is not None
        assert {s.path.name for s in problem.solutions} >= {"solution", "wrong"}
        assert (problem.path / "statement.md").is_file()
        assert (problem.path / "editorial.md").is_file()
        # Ignore the user's rime-out: only source directories must stay case-free.
        for path in problem.path.rglob("*"):
            if "rime-out" not in path.parts:
                assert path.suffix not in {".in", ".diff"}
    assert read_subtasks(SAMPLE / "normal") is not None


@pytest.mark.parametrize("name", ["normal", "special", "interactive"])
def test_sample_generators_validate_and_are_deterministic(tmp_path: Path, name: str) -> None:
    tests = SAMPLE / name / "tests"
    assert run_script(tests / "generator.py", cwd=tmp_path).returncode == 0
    first = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert len(first) >= 3 and all(p.endswith(".in") for p in first)
    assert all(b"\r" not in contents and contents.endswith(b"\n") for contents in first.values())
    assert run_script(tests / "generator.py", cwd=tmp_path).returncode == 0
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == first
    for contents in first.values():
        assert run_script(tests / "validator.py", data=contents.decode()).returncode == 0


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("normal", ""),
        ("normal", "1 2"),
        ("normal", "1 2 3\n"),
        ("normal", "1000000001 0\n"),
        ("normal", "01 2\n"),
        ("normal", "a b\n"),
        ("special", "1\n"),
        ("special", "1000000001\n"),
        ("special", "2\n3\n"),
        ("interactive", "0\n"),
        ("interactive", "101\n"),
        ("interactive", "42x\n"),
    ],
)
def test_sample_validators_reject_invalid_inputs(name: str, data: str) -> None:
    assert run_script(SAMPLE / name / "tests" / "validator.py", data=data).returncode != 0


@pytest.mark.parametrize("mode", ["rime", "yukicoder"])
@pytest.mark.parametrize(
    ("answer", "accepted"),
    [
        ("1 16\n", True),
        ("16 1\r\n", True),
        ("0 17\n", False),
        ("1 1\n", False),
        ("1 16 0\n", False),
        ("oops", False),
        ("9" * 128, False),
    ],
)
def test_sample_special_judge(
    tmp_path: Path,
    mode: str,
    answer: str,
    accepted: bool,
) -> None:
    case, output, reference = (tmp_path / name for name in ("case.in", "out", "case.diff"))
    case.write_text("17\n", encoding="utf-8")
    output.write_text(answer, encoding="utf-8")
    reference.write_text("1 16\n", encoding="utf-8")
    if mode == "rime":
        args = ["--infile", str(case), "--difffile", str(reference), "--outfile", str(output)]
        stdin = ""
    else:
        args = [str(case), str(reference), "unused-source", "unused-score"]
        stdin = answer
    result = run_script(SAMPLE / "special" / "tests" / "judge.py", *args, data=stdin)
    assert (result.returncode == 0) == accepted


@pytest.mark.parametrize(
    "protocol",
    [
        "",
        "? 0\n",
        "? 101\n",
        "? x\n",
        "? 42",
        "oops 42\n",
        "? 42 extra\n",
        "? " + "9" * 65 + "\n",
        "? 1\n" * 8,
        "! 1\n",
    ],
)
def test_sample_interactor_rejects_bad_protocol(protocol: str) -> None:
    output = io.StringIO()
    assert not load_interactor().interact(42, io.StringIO(protocol), output)
    assert output.getvalue().startswith("100\n")
    assert output.getvalue().endswith("-1\n")


@pytest.mark.parametrize("secret", range(1, 101))
def test_sample_interactor_all_secrets(secret: int) -> None:
    queries = []
    low, high = 1, 100
    while low <= high:
        middle = (low + high) // 2
        queries.append(f"? {middle}\n")
        if secret == middle:
            break
        if secret < middle:
            high = middle - 1
        else:
            low = middle + 1
    assert len(queries) <= 7
    output = io.StringIO()
    assert load_interactor().interact(
        secret, io.StringIO("".join(queries) + f"! {secret}\n"), output
    )
    assert output.getvalue().endswith("EQUAL\nOK\n")


@pytest.mark.parametrize("solution", ["solution", "wrong"])
@pytest.mark.parametrize("secret", [1, 42, 100])
def test_sample_real_local_interaction(solution: str, secret: int) -> None:
    result = run_script(
        SAMPLE / "interactive" / "tests" / "judge.py",
        "--local",
        sys.executable,
        str(SAMPLE / "interactive" / solution / "main.py"),
        data=f"{secret}\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("AC\n" if solution == "solution" else "WA\n")


@pytest.mark.parametrize(
    "source",
    [
        "import time; time.sleep(60)",
        "print('? x', flush=True)",
        "input(); print('! 42', flush=True); input(); print('trailing', flush=True)",
        "input(); print('! 42', flush=True); input(); raise SystemExit(1)",
    ],
)
def test_sample_local_bridge_rejects_stalls_crashes_and_trailing_output(
    tmp_path: Path,
    source: str,
) -> None:
    solution = tmp_path / "solution with spaces.py"
    solution.write_text(source, encoding="utf-8")
    result = run_script(
        SAMPLE / "interactive" / "tests" / "judge.py",
        "--local",
        sys.executable,
        str(solution),
        data="42\n",
    )
    assert result.returncode == 0 and result.stdout == "WA\n"


@pytest.mark.parametrize("solution", ["solution", "wrong"])
def test_sample_yukicoder_style_live_pipes(tmp_path: Path, solution: str) -> None:
    hidden = tmp_path / "hidden.in"
    hidden.write_text("42\n", encoding="utf-8")
    judge_path = SAMPLE / "interactive" / "tests" / "judge.py"
    solution_path = SAMPLE / "interactive" / solution / "main.py"
    with subprocess.Popen(
        [
            sys.executable,
            str(judge_path),
            str(hidden),
            "unused-answer",
            "unused-code",
            "unused-score",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as judge:
        assert judge.stdin is not None and judge.stdout is not None
        try:
            with subprocess.Popen(
                [sys.executable, str(solution_path)],
                stdin=judge.stdout,
                stdout=judge.stdin,
                stderr=subprocess.DEVNULL,
            ) as answer:
                judge.stdout.close()
                judge.stdin.close()
                try:
                    assert answer.wait(timeout=5) == 0
                    assert judge.wait(timeout=5) == (0 if solution == "solution" else 1)
                finally:
                    if answer.poll() is None:
                        answer.kill()
        finally:
            if judge.poll() is None:
                judge.kill()


@pytest.mark.skipif(os.name == "nt", reason="reference Rime runs on Linux/WSL")
def test_samples_pass_reference_rime_in_unicode_space_path(tmp_path: Path) -> None:
    rime_root = Path(os.environ.get("RIME_REFERENCE_DIR", str(ROOT.parent / "rime"))).resolve()
    if not (rime_root / "rime.py").is_file():
        pytest.skip("reference Rime checkout is not available")
    project = tmp_path / "sample 雪 with spaces"
    shutil.copytree(
        SAMPLE, project, ignore=shutil.ignore_patterns("rime-out", "__pycache__", ".env")
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(rime_root)))
    for name in tuple(environment):
        if name.startswith(("YUKICODER_", "GH_", "GITHUB_")):
            environment.pop(name)
    result = subprocess.run(
        [sys.executable, str(rime_root / "rime.py"), "test"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout, result.stdout
    for summary in (
        "normal ... 2 solutions, 4 tests",
        "special ... 3 solutions, 3 tests",
        "interactive ... 2 solutions, 3 tests",
        "Total 0 errors, 0 warnings",
    ):
        assert summary in result.stdout, result.stdout
    for problem in load_project(project).problems:
        cases = read_testcases(problem.path / "rime-out" / "tests", require_nonempty=True)
        assert len(cases) >= 3
        assert not list((problem.path / "tests").glob("*.in"))
