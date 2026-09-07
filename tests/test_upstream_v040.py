from __future__ import annotations

from pathlib import Path
from typing import Never

import pytest

from yukitools_rime.api.body import is_safe_testcase_name
from yukitools_rime.commands.push import push
from yukitools_rime.commands.query import list_remote_testcases
from yukitools_rime.commands.sync import diff_remote, pull
from yukitools_rime.errors import LayoutError
from yukitools_rime.layout import load_project, resolve_target
from yukitools_rime.models import ProblemConfig, ProblemSettings, ProjectConfig
from yukitools_rime.rime_config import render_problem_block, render_project_block
from yukitools_rime.source_languages import source_spec


def _problem(root: Path, name: str, problem_id: int, *, sync: bool = True) -> Path:
    path = root / name
    path.mkdir()
    config = ProblemConfig(
        problem_id=problem_id,
        settings=ProblemSettings(
            title=name,
            tags="",
            level=1,
            time_limit_ms=1000,
            memory_limit=256,
            eps_mode="-",
            eps="0",
            wip=False,
            recruiting_tester=False,
            problem_type=0,
            judge_type=0,
        ),
        rime_id=name.upper(),
        sync=sync,
    )
    (path / "PROBLEM").write_text(render_problem_block(config), encoding="utf-8")
    return path


def test_current_server_testcase_alphabet_accepts_hyphen() -> None:
    assert is_safe_testcase_name("case-01.txt")


def test_current_c_language_ids_use_the_rime_c_adapter() -> None:
    for lang_id in (
        "c",
        "c17",
        "c_latest",
        "c11_gcc12",
        "c90_gcc15",
        "gcc14",
    ):
        assert source_spec(lang_id).extension == "c"
        assert source_spec(lang_id).rime_kind == "c"


def test_project_target_skips_sync_false_but_explicit_target_does_not(
    tmp_path: Path,
) -> None:
    (tmp_path / "PROJECT").write_text(
        render_project_block(ProjectConfig()),
        encoding="utf-8",
    )
    enabled = _problem(tmp_path, "enabled", 1)
    disabled = _problem(tmp_path, "disabled", 2, sync=False)

    assert [problem.path for problem in resolve_target(tmp_path).problems] == [enabled.resolve()]
    explicit = resolve_target(disabled)
    assert explicit.problem is not None
    assert explicit.problem.path == disabled.resolve()


def test_project_services_noop_when_all_problems_disable_sync(
    tmp_path: Path,
) -> None:
    (tmp_path / "PROJECT").write_text(
        render_project_block(ProjectConfig()),
        encoding="utf-8",
    )
    _problem(tmp_path, "disabled", 2, sync=False)
    project = load_project(tmp_path)

    def forbidden_client(_problem: object) -> Never:
        raise AssertionError("disabled problems must not create an API client")

    assert push(project, forbidden_client).problems == ()
    assert pull(project, forbidden_client).problems == ()
    assert diff_remote(project, forbidden_client).problems == ()
    assert list_remote_testcases(project, forbidden_client) == ()


def test_project_services_reject_project_without_problems(
    tmp_path: Path,
) -> None:
    (tmp_path / "PROJECT").write_text(
        render_project_block(ProjectConfig()),
        encoding="utf-8",
    )
    project = load_project(tmp_path)

    def forbidden_client(_problem: object) -> Never:
        raise AssertionError("empty projects must not create an API client")

    with pytest.raises(LayoutError, match="no managed problems"):
        push(project, forbidden_client)
    with pytest.raises(LayoutError, match="no managed problems"):
        pull(project, forbidden_client)
    with pytest.raises(LayoutError, match="no managed problems"):
        diff_remote(project, forbidden_client)
    with pytest.raises(LayoutError, match="no managed problems"):
        list_remote_testcases(project, forbidden_client)
