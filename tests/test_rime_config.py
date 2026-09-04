from __future__ import annotations

from pathlib import Path

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
    write_config_atomic,
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


def test_config_writer_rejects_symlink_with_stable_config_error(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real-PROJECT"
    target.write_bytes(b"unchanged\r\n")
    link = tmp_path / "PROJECT"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ConfigError, match="cannot write configuration"):
        write_config_atomic(link, render_project_block(ProjectConfig()))

    assert target.read_bytes() == b"unchanged\r\n"
    assert link.is_symlink()


def test_testset_rejects_same_generator_and_judge_source_case_insensitively() -> None:
    with pytest.raises(ConfigError, match="must be distinct"):
        rime_config.TestsetConfig(
            GeneratorConfig("cpp", "Tool.cpp", 1),
            JudgeConfig("cpp", "tool.CPP"),
        )


def test_parser_is_static_and_never_executes_other_statements() -> None:
    source = (
        f"{BEGIN_MARKER}\n"
        "__import__('module_that_must_not_exist')\n"
        "yukicoder_project()\n"
        f"{END_MARKER}\n"
        "raise RuntimeError('must not execute')\n"
    )

    assert parse_project_config(source) == ProjectConfig()


def test_attribute_call_is_not_mistaken_for_managed_declaration() -> None:
    source = f"{BEGIN_MARKER}\nplugin.yukicoder_project()\n{END_MARKER}\n"
    with pytest.raises(ConfigError, match="declaration is missing"):
        parse_project_config(source)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        ("yukicoder_project('https://example.test')", "keyword arguments only"),
        (
            "yukicoder_project(base_url='https://a.test', base_url='https://b.test')",
            "duplicate yukicoder_project",
        ),
        ("yukicoder_project(base_url=1)", "invalid yukicoder_project"),
    ],
)
def test_project_parser_rejects_positional_duplicate_and_invalid_values(
    call: str, message: str
) -> None:
    source = f"{BEGIN_MARKER}\n{call}\n{END_MARKER}\n"
    with pytest.raises(ConfigError, match=message):
        parse_project_config(source)


def test_optional_declarations_can_be_required() -> None:
    with pytest.raises(ConfigError, match=r"yukicoder_generator.*missing"):
        rime_config.parse_generator_config("", required=True)
    with pytest.raises(ConfigError, match=r"yukicoder_judge.*missing"):
        rime_config.parse_judge_config("", required=True)
    assert rime_config.parse_generator_config("") is None
    assert rime_config.parse_judge_config("") is None


def test_declaration_classification_handles_plain_and_managed_sources() -> None:
    assert rime_config.contains_declaration("yukicoder_solution()\n", "yukicoder_solution")
    assert not rime_config.contains_declaration(
        "module.yukicoder_solution()\n", "yukicoder_solution"
    )
    assert rime_config.is_managed_configuration(f"{BEGIN_MARKER}\n{END_MARKER}\n", "missing")
    assert not rime_config.is_managed_configuration("# plain\n", "missing")


def test_unterminated_token_stream_does_not_execute_or_leak_token_error() -> None:
    assert rime_config._marker_spans('"""unterminated', BEGIN_MARKER) == ()


def test_non_python_physical_markers_support_bom_and_replacement() -> None:
    original = f"\ufeff{BEGIN_MARKER}\r\nnot valid python: {{{{\r\n{END_MARKER}\r\n"
    updated = upsert_managed_block(
        original,
        render_project_block(ProjectConfig()),
        python_source=False,
    )

    assert updated.startswith("\ufeff")
    assert "not valid python" not in updated
    assert "\r\n" in updated


@pytest.mark.parametrize(
    "config",
    [
        rime_config.TestsetConfig(),
        rime_config.TestsetConfig(generator=GeneratorConfig("cpp", "gen.cpp", 1)),
        rime_config.TestsetConfig(judge=JudgeConfig("cpp", "judge.cpp")),
    ],
)
def test_testset_rendering_round_trips_each_optional_combination(
    config: rime_config.TestsetConfig,
) -> None:
    assert parse_testset_config(render_testset_block(config)) == config


@pytest.mark.parametrize(
    ("source", "expected_gap"),
    [
        ("local", "\n\n"),
        ("local\n", "\n\n"),
        ("local\n\n", "\n\n"),
    ],
)
def test_upsert_uses_one_blank_line_before_new_block(source: str, expected_gap: str) -> None:
    updated = upsert_managed_block(source, render_project_block(ProjectConfig()))
    assert f"local{expected_gap}{BEGIN_MARKER}" in updated


@pytest.mark.parametrize("separator", ["\n", "\r\n"])
def test_move_managed_block_after_suffix_handles_leading_newline(separator: str) -> None:
    block = render_project_block(ProjectConfig()).replace("\n", separator)
    source = block + separator + "use_plugin('later')" + separator

    updated = upsert_managed_block_at_end(
        source,
        render_project_block(ProjectConfig()),
    )

    assert updated.index("use_plugin") < updated.index(BEGIN_MARKER)
    assert updated.count(BEGIN_MARKER) == 1


def test_update_problem_block_preserves_local_only_fields() -> None:
    local = problem("local")
    remote = ProblemConfig(
        42,
        ProblemSettings(
            "remote",
            "new",
            2,
            3000,
            1024,
            "-",
            "0",
            False,
            True,
            1,
            0,
        ),
        "remote-id",
    )
    source = "# local header\n" + render_problem_block(local)

    updated = rime_config.update_problem_block(source, remote)
    parsed = parse_problem_config(updated)

    assert parsed.settings.title == "remote"
    assert parsed.rime_id == local.rime_id
    assert parsed.reference_solution == local.reference_solution
    assert parsed.rime_options == local.rime_options
    assert updated.startswith("# local header\n")


def test_config_readers_wrap_file_and_parser_errors(tmp_path: Path) -> None:
    missing = tmp_path / "MISSING"
    with pytest.raises(ConfigError, match="cannot read configuration"):
        rime_config.read_config_source(missing)

    path = tmp_path / "PROJECT"
    path.write_text("invalid", encoding="utf-8")

    def reject(source: str) -> object:
        raise ConfigError("parser rejected")

    with pytest.raises(ConfigError) as caught:
        rime_config.read_config(path, reject)
    assert str(path) in str(caught.value)
    assert "parser rejected" in str(caught.value)


def test_config_writer_rejects_non_string_and_unencodable_source(tmp_path: Path) -> None:
    path = tmp_path / "PROJECT"
    with pytest.raises(TypeError):
        write_config_atomic(path, b"bytes")  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="cannot encode"):
        write_config_atomic(path, "\ud800")


def test_syntax_error_without_line_has_stable_message() -> None:
    error = rime_config._syntax_error(SyntaxError("broken"))
    assert str(error) == "invalid Rime configuration syntax: broken"
