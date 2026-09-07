from __future__ import annotations

import pytest

from yukitools_rime.source_languages import (
    SourceSpec,
    default_source_name,
    infer_rime_kind,
    source_spec,
)


@pytest.mark.parametrize(
    "lang_id",
    ["c", "c11", "c17", "c_latest", "c_gcc14", "gcc", "gcc13", " GCC_LATEST "],
)
def test_c_language_id_families_use_c_sources(lang_id: str) -> None:
    assert source_spec(lang_id) == SourceSpec("c", "c")
    assert default_source_name("main", lang_id) == "main.c"
    assert infer_rime_kind(lang_id) == "c"


@pytest.mark.parametrize(
    ("lang_id", "expected"),
    [
        ("cpp23", SourceSpec("cpp", "cxx")),
        ("python3", SourceSpec("py", "script")),
        ("unknown", SourceSpec("txt", None)),
    ],
)
def test_other_language_families_remain_unchanged(lang_id: str, expected: SourceSpec) -> None:
    assert source_spec(lang_id) == expected
