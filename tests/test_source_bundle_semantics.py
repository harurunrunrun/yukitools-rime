from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from yukitools_rime.errors import ConfigError
from yukitools_rime.source_bundle import bundle_program_source


def _project(
    root: Path,
    *,
    source: str,
    headers: dict[str, str],
) -> tuple[Path, Path]:
    root.mkdir()
    (root / "PROJECT").write_text('project(library_dir="common")\n', encoding="utf-8")
    library = root / "common"
    library.mkdir()
    for name, content in headers.items():
        (library / name).write_text(content, encoding="utf-8")
    source_path = root / "problem" / "tests" / "program.cpp"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(source, encoding="utf-8")
    return root, source_path


def _bundle(
    root: Path,
    source_path: Path,
    dependencies: list[str],
    *,
    rime_kind: str | None = "cxx",
) -> str:
    return bundle_program_source(
        root,
        source_path,
        {"dependency": dependencies},
        rime_kind=rime_kind,
    )


def _compile_cpp(tmp_path: Path, source: str) -> None:
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("g++ is unavailable")
    path = tmp_path / "bundled.cpp"
    executable = tmp_path / "bundled"
    path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [compiler, "-std=c++17", "-Wall", "-Wextra", str(path), "-o", str(executable)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_pseudo_includes_in_comments_literals_and_continuations_remain(
    tmp_path: Path,
) -> None:
    backslash = chr(92)
    source = (
        '/* block comment\n#include "dep.h"\n*/\n'
        '// #include "dep.h"\n'
        'const char *ordinary = "#include \\"dep.h\\"";\n'
        'const char *raw = R"tag(\n#include "dep.h"\n)tag";\n'
        '#define INCLUDE_TEXT "#include \\"dep.h\\""\n'
        f'#define CONTINUED {backslash}\n#include "dep.h"\n'
        f'// continued comment {backslash}\n#include "dep.h"\n'
        '#include "dep.h"\n'
    )
    root, source_path = _project(
        tmp_path / "repo",
        source=source,
        headers={"dep.h": "#define DEP_VALUE 17\n"},
    )

    bundled = _bundle(root, source_path, ["dep.h"])

    assert '/* block comment\n#include "dep.h"\n*/' in bundled
    assert '// #include "dep.h"' in bundled
    assert '"#include \\"dep.h\\""' in bundled
    assert 'R"tag(\n#include "dep.h"\n)tag"' in bundled
    assert f'#define CONTINUED {backslash}\n#include "dep.h"' in bundled
    assert f'// continued comment {backslash}\n#include "dep.h"' in bundled
    assert bundled.count("BEGIN YUKITOOLS-RIME DEPENDENCY: dep.h") == 1
    assert bundled.count("#define DEP_VALUE 17") == 1


def test_include_surrounded_by_block_comments_is_still_a_directive(tmp_path: Path) -> None:
    root, source_path = _project(
        tmp_path / "repo",
        source='/* leading */  # include "dep.h"  /* trailing */\n',
        headers={"dep.h": "#define INCLUDED 1\n"},
    )

    bundled = _bundle(root, source_path, ["dep.h"])

    assert '# include "dep.h"' not in bundled
    assert bundled.startswith("/* leading */  /* BEGIN YUKITOOLS-RIME DEPENDENCY: dep.h */")
    assert "#define INCLUDED 1" in bundled
    assert bundled.rstrip().endswith("/* trailing */")


def test_unguarded_x_macro_dependency_expands_at_every_include(tmp_path: Path) -> None:
    root, source_path = _project(
        tmp_path / "repo",
        source=(
            "#define ITEM(name) int name;\n"
            '#include "items.inc"\n'
            "#undef ITEM\n"
            "#define ITEM(name) name = 1;\n"
            'void initialize() {\n#include "items.inc"\n}\n'
            "#undef ITEM\n"
        ),
        headers={"items.inc": "ITEM(first)\nITEM(second)\n"},
    )

    bundled = _bundle(root, source_path, ["items.inc"])

    assert bundled.count("BEGIN YUKITOOLS-RIME DEPENDENCY: items.inc") == 2
    assert bundled.count("ITEM(first)") == 2
    assert bundled.count("ITEM(second)") == 2


def test_pragma_once_dependency_uses_repeatable_guards_and_compiles(tmp_path: Path) -> None:
    root, source_path = _project(
        tmp_path / "repo",
        source=(
            '#include "once.hpp"\n'
            '#include "once.hpp"\n'
            "int main() { return once_value() == 7 ? 0 : 1; }\n"
        ),
        headers={"once.hpp": "#pragma once\ninline int once_value() { return 7; }\n"},
    )

    bundled = _bundle(root, source_path, ["once.hpp"])

    guards = re.findall(r"#ifndef (YUKITOOLS_RIME_ONCE_[A-F0-9]+)", bundled)
    assert len(guards) == 2
    assert guards[0] == guards[1]
    assert not re.search(r"^[ \t]*#[ \t]*pragma[ \t]+once", bundled, re.MULTILINE)
    assert bundled.count("REMOVED PRAGMA ONCE: once.hpp") == 2
    _compile_cpp(tmp_path, bundled)


def test_disabled_first_pragma_once_include_does_not_consume_active_include(
    tmp_path: Path,
) -> None:
    root, source_path = _project(
        tmp_path / "repo",
        source=(
            "#if 0\n"
            '#include "once.hpp"\n'
            "#endif\n"
            '#include "once.hpp"\n'
            '#include "once.hpp"\n'
            "int main() { Once value; return value.number; }\n"
        ),
        headers={"once.hpp": "#pragma once\nstruct Once { int number = 0; };\n"},
    )

    bundled = _bundle(root, source_path, ["once.hpp"])

    assert bundled.count("BEGIN YUKITOOLS-RIME DEPENDENCY: once.hpp") == 3
    _compile_cpp(tmp_path, bundled)


def test_standard_include_guarded_cycle_is_flattened_and_compiles(tmp_path: Path) -> None:
    root, source_path = _project(
        tmp_path / "repo",
        source='#include "a.hpp"\nint main() { A value{}; return value.pointer != nullptr; }\n',
        headers={
            "a.hpp": (
                "#ifndef A_HPP_INCLUDED\n"
                "#define A_HPP_INCLUDED\n"
                '#include "b.hpp"\n'
                "struct A { B *pointer; };\n"
                "#endif\n"
            ),
            "b.hpp": (
                "#ifndef B_HPP_INCLUDED\n"
                "#define B_HPP_INCLUDED\n"
                '#include "a.hpp"\n'
                "struct B {};\n"
                "#endif\n"
            ),
        },
    )

    bundled = _bundle(root, source_path, ["a.hpp", "b.hpp"])

    assert "GUARDED RECURSIVE INCLUDE: a.hpp" in bundled
    assert '#include "a.hpp"' not in bundled
    assert '#include "b.hpp"' not in bundled
    _compile_cpp(tmp_path, bundled)


@pytest.mark.parametrize(
    "a_header",
    [
        '#ifndef A_HPP_INCLUDED\n#define DIFFERENT_MACRO\n#include "b.hpp"\n#endif\n',
        '#ifndef A_HPP_INCLUDED\n#define A_HPP_INCLUDED\n#endif\n#include "b.hpp"\n',
    ],
    ids=["mismatched-guard", "guard-does-not-enclose-file"],
)
def test_false_or_non_outer_guard_does_not_hide_real_cycle(
    tmp_path: Path,
    a_header: str,
) -> None:
    root, source_path = _project(
        tmp_path / "repo",
        source='#include "a.hpp"\n',
        headers={"a.hpp": a_header, "b.hpp": '#include "a.hpp"\n'},
    )

    with pytest.raises(
        ConfigError,
        match=r"unguarded cyclic.*a\.hpp -> b\.hpp -> a\.hpp",
    ):
        _bundle(root, source_path, ["a.hpp", "b.hpp"])


@pytest.mark.parametrize("rime_kind", ["script", "java", "rust", "go", "python"])
def test_non_c_program_with_dependency_is_rejected(
    tmp_path: Path,
    rime_kind: str,
) -> None:
    root, source_path = _project(
        tmp_path / rime_kind,
        source='#include "dep.h"\n',
        headers={"dep.h": "content\n"},
    )

    with pytest.raises(ConfigError, match="only be bundled for C or C\\+\\+"):
        _bundle(root, source_path, ["dep.h"], rime_kind=rime_kind)


def test_unused_declared_dependency_is_allowed(tmp_path: Path) -> None:
    source = "int main() { return 0; }\n"
    root, source_path = _project(
        tmp_path / "repo",
        source=source,
        headers={"unused.hpp": "#error should never be inserted\n"},
    )

    assert _bundle(root, source_path, ["unused.hpp"]) == source


def test_source_and_dependency_bom_crlf_are_normalized(tmp_path: Path) -> None:
    root, source_path = _project(
        tmp_path / "repo",
        source="placeholder\n",
        headers={"dep.h": "placeholder\n"},
    )
    source_path.write_bytes(b'\xef\xbb\xbf#include "dep.h"\r\nint main() { return VALUE; }\r\n')
    (root / "common" / "dep.h").write_bytes(b"\xef\xbb\xbf#define VALUE 0\r\n")

    bundled = _bundle(root, source_path, ["dep.h"])

    assert "\ufeff" not in bundled
    assert "\r" not in bundled
    assert "#define VALUE 0\n" in bundled
