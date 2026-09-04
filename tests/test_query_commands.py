from __future__ import annotations

from pathlib import Path

import pytest

import yukitools_rime.commands.query as query_module
from yukitools_rime.api.types import Language
from yukitools_rime.commands.query import list_languages, list_remote_testcases
from yukitools_rime.errors import ValidationError
from yukitools_rime.layout import ProblemLayout, ProjectLayout, TargetSelection
from yukitools_rime.models import ProblemConfig, ProblemSettings, ProjectConfig, Which


def problem(path: Path, problem_id: int, rime_id: str) -> ProblemLayout:
    settings = ProblemSettings(rime_id, "", 1, 1000, 256, "-", "0", False, False, 0, 0)
    return ProblemLayout(
        path,
        ProblemConfig(problem_id, settings, rime_id),
        None,
        (),
    )


class FakeLanguages:
    def __init__(self) -> None:
        self.values = [
            Language("a", "A", "1", ""),
            Language("b", "B", "2", "enable"),
            Language("c", "C", "3", "disable"),
        ]
        self.calls = 0
        self.exited = False

    def languages(self) -> list[Language]:
        self.calls += 1
        return list(self.values)

    def __enter__(self) -> FakeLanguages:
        return self

    def __exit__(self, *_args: object) -> None:
        self.exited = True


class FakeTestcases:
    def __init__(self, names: dict[tuple[int, str], list[str]]) -> None:
        self.names = names
        self.calls: list[tuple[int, str]] = []

    def list_testcases(self, problem_id: int, which: Which | str) -> list[str]:
        side = which.value if isinstance(which, Which) else which
        self.calls.append((problem_id, side))
        return self.names[(problem_id, side)]


def test_languages_filters_enabled_and_factory_needs_no_project() -> None:
    fake = FakeLanguages()
    result = list_languages(lambda: fake)
    assert [language.id for language in result] == ["a", "b"]
    assert fake.calls == 1
    assert [language.id for language in list_languages(fake, include_disabled=True)] == [
        "a",
        "b",
        "c",
    ]


def test_languages_default_uses_anonymous_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLanguages()
    seen: list[str] = []

    def anonymous(base_url: str) -> FakeLanguages:
        seen.append(base_url)
        return fake

    monkeypatch.setattr(query_module.YukicoderClient, "anonymous", anonymous)
    assert [item.id for item in list_languages(base_url="https://example/api")] == ["a", "b"]
    assert seen == ["https://example/api"]
    assert fake.exited


def test_testcases_support_project_all_both_sides_and_factories(tmp_path: Path) -> None:
    first = problem(tmp_path / "a", 1, "A")
    second = problem(tmp_path / "b", 2, "B")
    project = ProjectLayout(tmp_path, ProjectConfig(), (first, second))
    clients = {
        1: FakeTestcases({(1, "in"): ["z", "a"], (1, "out"): ["z", "a"]}),
        2: FakeTestcases({(2, "in"): ["b"], (2, "out"): ["b"]}),
    }
    created: list[int] = []

    def factory(item: ProblemLayout) -> FakeTestcases:
        created.append(item.problem_id)
        return clients[item.problem_id]

    result = list_remote_testcases(project, factory)
    assert [(row.problem_id, row.which, row.names) for row in result] == [
        (1, Which.IN, ("a", "z")),
        (1, Which.OUT, ("a", "z")),
        (2, Which.IN, ("b",)),
        (2, Which.OUT, ("b",)),
    ]
    assert created == [1, 2]


def test_testcases_one_side_uses_names_endpoint_only(tmp_path: Path) -> None:
    item = problem(tmp_path / "a", 1, "A")
    project = ProjectLayout(tmp_path, ProjectConfig(), (item,))
    selection = TargetSelection(project, (item,), item.path)
    fake = FakeTestcases({(1, "out"): ["sample.txt"]})
    result = list_remote_testcases(selection, fake, which="out")
    assert result[0].names == ("sample.txt",)
    assert fake.calls == [(1, "out")]


def test_testcases_validate_side_and_duplicate_names(tmp_path: Path) -> None:
    item = problem(tmp_path / "a", 1, "A")
    project = ProjectLayout(tmp_path, ProjectConfig(), (item,))
    fake = FakeTestcases({(1, "in"): ["a", "a"]})
    with pytest.raises(ValidationError, match="duplicate"):
        list_remote_testcases(project, fake, which=Which.IN)
    with pytest.raises(ValidationError, match="which"):
        list_remote_testcases(project, fake, which="side")


def test_query_rejects_invalid_option_response_and_target(tmp_path: Path) -> None:
    fake = FakeLanguages()
    with pytest.raises(ValidationError, match="include_disabled"):
        list_languages(fake, include_disabled=1)  # type: ignore[arg-type]

    fake.values = [object()]  # type: ignore[list-item]
    with pytest.raises(ValidationError, match="invalid entry"):
        list_languages(fake)

    testcase_client = FakeTestcases({})
    with pytest.raises(TypeError, match="ProjectLayout or TargetSelection"):
        list_remote_testcases(
            problem(tmp_path / "a", 1, "A"),  # type: ignore[arg-type]
            testcase_client,
        )
