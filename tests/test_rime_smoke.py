from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    SolutionConfig,
)
from yukitools_rime.rime_config import (
    TestsetConfig as RimeTestsetConfig,
)
from yukitools_rime.rime_config import (
    render_problem_block,
    render_project_block,
    render_solution_block,
    render_testset_block,
)


def _reference_rime() -> Path:
    configured = os.environ.get("RIME_REFERENCE_DIR")
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else Path(__file__).resolve().parents[2] / "rime"
    )
    if not (root / "rime.py").is_file():
        pytest.skip("reference Rime checkout is not available")
    return root


def test_reference_rime_loads_all_managed_configurations(tmp_path: Path) -> None:
    rime_root = _reference_rime()
    project = tmp_path / "project"
    problem = project / "a"
    testset = problem / "tests"
    solution = problem / "solution"
    unknown_solution = problem / "unknown"
    testset.mkdir(parents=True)
    solution.mkdir()
    unknown_solution.mkdir()

    (project / "PROJECT").write_text(
        'use_plugin("rime_plus")\n\n'
        + render_project_block(ProjectConfig(rime_out_dir="generated")),
        encoding="utf-8",
    )
    (problem / "PROBLEM").write_text(
        render_problem_block(
            ProblemConfig(
                problem_id=12345,
                settings=ProblemSettings(
                    title="Smoke test",
                    tags="test",
                    level=1.5,
                    time_limit_ms=2000,
                    memory_limit=512,
                    eps_mode="-",
                    eps="0",
                    wip=True,
                    recruiting_tester=False,
                    problem_type=0,
                    judge_type=1,
                ),
                rime_id="A",
                reference_solution="solution",
            )
        ),
        encoding="utf-8",
    )
    (testset / "TESTSET").write_text(
        render_testset_block(
            RimeTestsetConfig(
                generator=GeneratorConfig(
                    lang_id="python3",
                    src="generator.py",
                    test_case_num=1,
                    prefix="sample",
                    rime_kind="script",
                ),
                judge=JudgeConfig(
                    lang_id="python3",
                    src="judge.py",
                    rime_kind="script",
                ),
            )
        ),
        encoding="utf-8",
    )
    (solution / "SOLUTION").write_text(
        render_solution_block(
            SolutionConfig(
                lang_id="python3",
                src="main.py",
                rime_kind="script",
            )
        ),
        encoding="utf-8",
    )
    (unknown_solution / "SOLUTION").write_text(
        render_solution_block(
            SolutionConfig(
                lang_id="remote-unknown",
                src="main.txt",
                rime_kind=None,
            )
        ),
        encoding="utf-8",
    )
    for path in (
        testset / "generator.py",
        testset / "judge.py",
        solution / "main.py",
        unknown_solution / "main.txt",
    ):
        path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        (str(source_root), str(rime_root), env.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [sys.executable, str(rime_root / "rime.py"), "help"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    build = subprocess.run(
        [sys.executable, str(rime_root / "rime.py"), "build"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert build.returncode == 0, build.stdout + build.stderr
    assert (problem / "generated").is_dir()
    assert not (problem / "rime-out").exists()
