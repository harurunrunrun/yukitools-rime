"""Shared yukicoder language-id to Rime source mapping."""

from __future__ import annotations

import re
from dataclasses import dataclass

_C_LANGUAGE = re.compile(r"c\d")


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """A safe source extension and its optional Rime code kind."""

    extension: str
    rime_kind: str | None


def source_spec(lang_id: str) -> SourceSpec:
    """Map a yukicoder language-id family to one deterministic source spec."""

    language = lang_id.strip().lower()
    if language.startswith("cpp"):
        return SourceSpec("cpp", "cxx")
    if (
        language == "c"
        or language.startswith("c_")
        or _C_LANGUAGE.match(language)
        or language.startswith("gcc")
    ):
        return SourceSpec("c", "c")
    if language.startswith("java"):
        return SourceSpec("java", "java")
    if language.startswith("kotlin"):
        return SourceSpec("kt", "kotlin")
    if language.startswith("rust"):
        return SourceSpec("rs", "rust")
    if language.startswith(("go", "golang")):
        return SourceSpec("go", "go")
    if language.startswith(("python", "pypy")):
        return SourceSpec("py", "script")
    if language.startswith("ruby"):
        return SourceSpec("rb", "script")
    if language.startswith("perl"):
        return SourceSpec("pl", "script")
    if language.startswith(("bash", "sh")):
        return SourceSpec("sh", "script")
    return SourceSpec("txt", None)


def infer_rime_kind(lang_id: str) -> str | None:
    """Return the Rime kind for a known language-id family."""

    return source_spec(lang_id).rime_kind


def default_source_name(stem: str, lang_id: str) -> str:
    """Build a source basename using the shared language mapping."""

    return f"{stem}.{source_spec(lang_id).extension}"


__all__ = ["SourceSpec", "default_source_name", "infer_rime_kind", "source_spec"]
