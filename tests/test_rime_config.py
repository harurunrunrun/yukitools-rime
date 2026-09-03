from __future__ import annotations

import pytest

from yukitools_rime import rime_config
from yukitools_rime.errors import ConfigError
from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    SolutionConfig,
)
from yukitools_rime.rime_config import (
    BEGIN_MARKER,
    END_MARKER,
    merge_remote_problem,
    parse_problem_config,
    parse_project_config,
    parse_solution_config,
    parse_testset_config,
    render_problem_block,
    render_project_block,
    render_solution_block,
    render_testset_block,
    upsert_managed_block,
    upsert_managed_block_at_end,
)


def problem(title: str = "Sum") -> ProblemConfig:
    return ProblemConfig(
        42,
        ProblemSettings(title, "math", 1.5, 2000, 512, "-", "0", True, False, 0, 1),
        "A",
        "correct",
        {"extra": ["x", 1]},
    )


def test_all_config_types_round_trip() -> None:
    project = ProjectConfig("https://example.test/api", "build-out")
    assert parse_project_config(render_project_block(project)) == project
    expected_problem = problem()
    assert parse_problem_config(render_problem_block(expected_problem)) == expected_problem
    testset = rime_config.TestsetConfig(
        GeneratorConfig("cpp20", "generator.cpp", 20, "case", "cxx", {"flags": ["-O2"]}),
        JudgeConfig("cpp20", "judge.cpp", "cxx", {}),
    )
    assert parse_testset_config(render_testset_block(testset)) == testset
    solution = SolutionConfig("cpp20", "main.cpp", "cxx", ["sample01"], {"flags": ["-O2"]})
    assert parse_solution_config(render_solution_block(solution)) == solution


@pytest.mark.parametrize(
    "source, message",
    [
        ("yukicoder_project(base_url=get_url())", "must be a literal"),
        (
            "yukicoder_project()\nyukicoder_project()",
            "multiple yukicoder_project",
        ),
        ("yukicoder_project(other=True)", "unknown yukicoder_project"),
        ("def x():\n    yukicoder_project()", "top-level"),
        ("yukicoder_project(**values)", "does not allow"),
    ],
)
def test_parser_rejects_non_static_or_ambiguous_calls(source: str, message: str) -> None:
    managed = f"# BEGIN YUKITOOLS-RIME\n{source}\n# END YUKITOOLS-RIME\n"
    with pytest.raises(ConfigError, match=message):
        parse_project_config(managed)


def test_upsert_preserves_surrounding_text_and_crlf() -> None:
    source = "use_plugin('rime_plus')\r\n\r\n# local\r\n"
    first = upsert_managed_block(source, render_project_block(ProjectConfig()))
    second = upsert_managed_block(first, render_project_block(ProjectConfig()))
    assert first == second
    assert first.startswith(source)
    assert "\r\n" in first
    assert "\n" not in first.replace("\r\n", "")
    assert first.count(BEGIN_MARKER) == 1


def test_upsert_preserves_cr_only_newlines() -> None:
    source = "# local\r"
    updated = upsert_managed_block(source, render_project_block(ProjectConfig()))
    assert "\n" not in updated
    assert "\r" in updated
    assert parse_project_config(updated) == ProjectConfig()


def test_remote_merge_keeps_only_rime_local_fields() -> None:
    local = problem("local")
    remote = ProblemConfig(
        42,
        ProblemSettings("remote", "", 2, 1000, 256, "-", 0, False, False, 0, 0),
        "server-placeholder",
    )
    merged = merge_remote_problem(local, remote)
    assert merged.settings.title == "remote"
    assert merged.rime_id == "A"
    assert merged.reference_solution == "correct"
    assert merged.rime_options == {"extra": ["x", 1]}


def test_unbalanced_marker_is_rejected() -> None:
    with pytest.raises(ConfigError, match="markers"):
        upsert_managed_block(f"before\n{BEGIN_MARKER}\n", render_project_block(ProjectConfig()))


def test_declaration_outside_managed_block_is_rejected() -> None:
    source = f"yukicoder_project()\n{BEGIN_MARKER}\n# intentionally empty\n{END_MARKER}\n"
    with pytest.raises(ConfigError, match="inside a managed"):
        parse_project_config(source)


def test_parser_accepts_and_preserves_a_leading_utf8_bom() -> None:
    source = "\ufeff" + render_project_block(ProjectConfig())
    assert parse_project_config(source) == ProjectConfig()
    updated = upsert_managed_block(source, render_project_block(ProjectConfig()))
    assert updated.startswith("\ufeff")


def test_reversed_markers_raise_config_error() -> None:
    source = f"{END_MARKER}\n{BEGIN_MARKER}\nyukicoder_project()\n"
    with pytest.raises(ConfigError, match="precedes"):
        parse_project_config(source)


def test_project_block_can_be_moved_after_later_plugins_idempotently() -> None:
    source = render_project_block(ProjectConfig()) + "\nuse_plugin('rime_plus')\n"
    updated = upsert_managed_block_at_end(source, render_project_block(ProjectConfig()))
    assert updated.index("use_plugin") < updated.index(BEGIN_MARKER)
    assert upsert_managed_block_at_end(updated, render_project_block(ProjectConfig())) == updated


def test_marker_text_inside_multiline_string_is_not_managed() -> None:
    original = (
        f'NOTICE = """keep this text\n{BEGIN_MARKER}\nnot a managed block\n{END_MARKER}\n"""\n'
    )

    updated = upsert_managed_block(original, render_project_block(ProjectConfig()))

    assert updated.startswith(original)
    assert parse_project_config(updated) == ProjectConfig()
