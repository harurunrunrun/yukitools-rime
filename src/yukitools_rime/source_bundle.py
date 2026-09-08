"""Build one uploadable source from Rime Plus library dependencies."""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from yukitools_rime.errors import ConfigError
from yukitools_rime.files import read_text
from yukitools_rime.models import validate_basename

_INCLUDE_HEAD = re.compile(r"#[ \t]*include(?=[ \t]|$)")
_PRAGMA_ONCE_HEAD = re.compile(r"#[ \t]*pragma[ \t]+once(?=[ \t]|$)")
_IFNDEF = re.compile(r"#[ \t]*ifndef[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*$")
_DEFINE = re.compile(r"#[ \t]*define[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:[ \t]+.*)?$")
_ENDIF = re.compile(r"#[ \t]*endif[ \t]*$")
_CONDITIONAL_START = re.compile(r"#[ \t]*(?:if|ifdef|ifndef)(?=[ \t]|$)")
_CONDITIONAL_END = re.compile(r"#[ \t]*endif(?=[ \t]|$)")
_RAW_START = re.compile(r'(?<![A-Za-z0-9_])(?:u8|u|U|L)?R"(?P<delimiter>[^ ()\\\t\r\n]{0,16})\(')


def _continued_line(line: str) -> bool:
    physical = line.rstrip("\r\n")
    return physical.endswith("\\")


class _LexicalState:
    """Mask C/C++ comments and literals while tracking physical-line state."""

    __slots__ = (
        "block_comment",
        "continued_line",
        "line_comment",
        "quote",
        "raw_end",
    )

    def __init__(self) -> None:
        self.block_comment = False
        self.continued_line = False
        self.line_comment = False
        self.quote: str | None = None
        self.raw_end: str | None = None

    def mask(self, line: str) -> str:
        masked = list(line)
        self.continued_line = _continued_line(line)

        def hide(start: int, end: int) -> None:
            for position in range(start, end):
                if masked[position] not in "\r\n":
                    masked[position] = " "

        if self.line_comment:
            hide(0, len(line))
            self.line_comment = _continued_line(line)
            return "".join(masked)
        index = 0
        escaped = False
        while index < len(line):
            if self.raw_end is not None:
                end = line.find(self.raw_end, index)
                if end < 0:
                    hide(index, len(line))
                    return "".join(masked)
                end += len(self.raw_end)
                hide(index, end)
                index = end
                self.raw_end = None
                continue
            if self.block_comment:
                end = line.find("*/", index)
                if end < 0:
                    hide(index, len(line))
                    return "".join(masked)
                end += 2
                hide(index, end)
                index = end
                self.block_comment = False
                continue
            if self.quote is not None:
                character = line[index]
                is_closing_quote = not escaped and character == self.quote
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif is_closing_quote or (character in "\r\n" and not _continued_line(line)):
                    self.quote = None
                if not is_closing_quote:
                    hide(index, index + 1)
                index += 1
                continue
            if line.startswith("//", index):
                hide(index, len(line))
                self.line_comment = _continued_line(line)
                return "".join(masked)
            if line.startswith("/*", index):
                hide(index, index + 2)
                self.block_comment = True
                index += 2
                continue
            raw = _RAW_START.match(line, index)
            if raw is not None:
                hide(index, raw.end())
                self.raw_end = f'){raw.group("delimiter")}"'
                index = raw.end()
                continue
            character = line[index]
            if character in {'"', "'"}:
                self.quote = character
            index += 1
        return "".join(masked)


def _directive_start(masked: str) -> int | None:
    match = re.match(r"[ \t]*(#)", masked)
    return match.start(1) if match is not None else None


def _content_end(line: str) -> int:
    return len(line.rstrip("\r\n"))


def _active_include(
    line: str,
    masked: str,
) -> tuple[str, str, str, str] | None:
    start = _directive_start(masked)
    if start is None:
        return None
    head = _INCLUDE_HEAD.match(masked, start)
    if head is None:
        return None

    end = _content_end(line)
    opening = masked.find('"', head.end(), end)
    if opening < 0 or masked[head.end() : opening].strip():
        return None
    closing = masked.find('"', opening + 1, end)
    if closing < 0 or masked[closing + 1 : end].strip():
        return None
    return (
        line[opening + 1 : closing],
        line[:start],
        line[closing + 1 : end],
        line[end:],
    )


def _active_pragma_once(
    line: str,
    masked: str,
) -> tuple[str, str, str] | None:
    start = _directive_start(masked)
    if start is None:
        return None
    head = _PRAGMA_ONCE_HEAD.match(masked, start)
    end = _content_end(line)
    if head is None or masked[head.end() : end].strip():
        return None
    return line[:start], line[head.end() : end], line[end:]


def _masked_lines(source: str) -> list[tuple[str, str, bool]]:
    state = _LexicalState()
    result: list[tuple[str, str, bool]] = []
    for line in source.splitlines(keepends=True):
        directive_allowed = not state.continued_line
        result.append((line, state.mask(line), directive_allowed))
    return result


def _include_guard(source: str) -> str | None:
    significant = [
        (masked.strip(), directive_allowed)
        for _, masked, directive_allowed in _masked_lines(source)
        if masked.strip()
    ]
    if len(significant) < 3:
        return None
    opening = _IFNDEF.fullmatch(significant[0][0]) if significant[0][1] else None
    definition = _DEFINE.fullmatch(significant[1][0]) if significant[1][1] else None
    if (
        opening is None
        or definition is None
        or opening.group("name") != definition.group("name")
        or not significant[-1][1]
        or _ENDIF.fullmatch(significant[-1][0]) is None
    ):
        return None

    depth = 0
    for index, (text, directive_allowed) in enumerate(significant):
        if not directive_allowed:
            continue
        if _CONDITIONAL_START.match(text):
            depth += 1
        elif _CONDITIONAL_END.match(text):
            depth -= 1
            if depth == 0 and index != len(significant) - 1:
                return None
    return opening.group("name") if depth == 0 else None


def _has_unconditional_pragma_once(source: str) -> bool:
    depth = 0
    for line, masked, directive_allowed in _masked_lines(source):
        if not directive_allowed:
            continue
        stripped = masked.strip()
        if _CONDITIONAL_END.match(stripped):
            depth = max(0, depth - 1)
            continue
        if depth == 0 and _active_pragma_once(line, masked) is not None:
            return True
        if _CONDITIONAL_START.match(stripped):
            depth += 1
    return False


def _once_macro(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24].upper()
    return f"YUKITOOLS_RIME_ONCE_{digest}"


def _function_name(call: ast.Call) -> str | None:
    return call.func.id if isinstance(call.func, ast.Name) else None


def _literal(node: ast.expr, *, label: str) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        line = getattr(node, "lineno", "?")
        raise ConfigError(f"{label} must be a literal (line {line})") from exc


def parse_project_library_dir(source: str) -> str:
    """Read Rime Plus' top-level project(library_dir=...) without executing it."""

    if source.startswith("\ufeff"):
        source = source[1:]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        location = f" at line {exc.lineno}" if exc.lineno is not None else ""
        raise ConfigError(f"invalid PROJECT syntax{location}: {exc.msg}") from exc

    calls = [
        statement.value
        for statement in tree.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and _function_name(statement.value) == "project"
    ]
    top_level_ids = {id(call) for call in calls}
    nested = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _function_name(node) == "project"
        and id(node) not in top_level_ids
    ]
    if nested:
        line = getattr(nested[0], "lineno", "?")
        raise ConfigError(f"project() must be a top-level call (line {line})")
    if len(calls) != 1:
        qualifier = "missing" if not calls else "duplicated"
        raise ConfigError(f"PROJECT project(library_dir=...) declaration is {qualifier}")

    call = calls[0]
    if len(call.args) > 2:
        raise ConfigError("project() has too many positional arguments")
    value_nodes: list[ast.expr] = list(call.args[:1])
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ConfigError("project() must not use ** keyword expansion")
        if keyword.arg == "library_dir":
            value_nodes.append(keyword.value)
    if len(value_nodes) != 1:
        qualifier = "missing" if not value_nodes else "specified more than once"
        raise ConfigError(f"project() library_dir is {qualifier}")
    value = _literal(value_nodes[0], label="project() library_dir")
    if not isinstance(value, str) or not value:
        raise ConfigError("project() library_dir must be a non-empty string")
    return value


def _dependency_names(options: Mapping[str, object]) -> tuple[str, ...]:
    if "dependency" not in options:
        return ()
    value = options["dependency"]
    if not isinstance(value, (list, tuple)):
        raise ConfigError("rime_options dependency must be a list or tuple of file names")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(f"rime_options dependency[{index}] must be a string")
        name = validate_basename(item, label=f"rime_options dependency[{index}]")
        folded = name.casefold()
        if folded in seen:
            raise ConfigError(f"rime_options dependency contains a duplicate: {name!r}")
        seen.add(folded)
        result.append(name)
    return tuple(result)


def _library_path(project_root: Path, configured: str) -> Path:
    if (
        configured.startswith("/")
        or configured in {".", ".."}
        or configured.endswith("/")
        or "//" in configured
        or "\\" in configured
        or "\x00" in configured
    ):
        raise ConfigError("project() library_dir must be a safe relative directory")
    pure = PurePosixPath(configured)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ConfigError("project() library_dir must be a safe relative directory")

    current = project_root
    for index, part in enumerate(pure.parts):
        validate_basename(part, label=f"project() library_dir component {index}")
        current /= part
        if current.is_symlink():
            raise ConfigError(f"project() library_dir must not contain a symlink: {current}")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(project_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ConfigError(
            f"project() library_dir is missing or escapes the project: {current}"
        ) from exc
    if not resolved.is_dir():
        raise ConfigError(f"project() library_dir is not a directory: {current}")
    return resolved


def _expanded_source(source: str, headers: Mapping[str, str]) -> str:
    pragma_once = {
        name for name, header in headers.items() if _has_unconditional_pragma_once(header)
    }
    include_guarded = {
        name for name, header in headers.items() if _include_guard(header) is not None
    }
    protected = pragma_once | include_guarded

    def expand(
        text: str,
        stack: tuple[str, ...],
        *,
        current_name: str | None = None,
    ) -> str:
        state = _LexicalState()
        result: list[str] = []
        for line in text.splitlines(keepends=True):
            directive_allowed = not state.continued_line
            masked = state.mask(line)

            pragma = (
                _active_pragma_once(line, masked)
                if directive_allowed and current_name in pragma_once
                else None
            )
            if pragma is not None:
                prefix, suffix, newline = pragma
                result.append(
                    f"{prefix}/* YUKITOOLS-RIME REMOVED PRAGMA ONCE: "
                    f"{current_name} */{suffix}{newline}"
                )
                continue

            include = _active_include(line, masked) if directive_allowed else None
            if include is None:
                result.append(line)
                continue
            include_name, prefix, suffix, newline = include
            dependency_name = (
                include_name[2:]
                if include_name.startswith("./") and "/" not in include_name[2:]
                else include_name
            )
            header = headers.get(dependency_name)
            if header is None:
                result.append(line)
                continue

            if dependency_name in stack:
                if dependency_name in protected:
                    result.append(
                        f"{prefix}/* YUKITOOLS-RIME GUARDED RECURSIVE INCLUDE: "
                        f"{dependency_name} */{suffix}{newline}"
                    )
                    continue
                cycle_start = stack.index(dependency_name)
                if not any(name in protected for name in stack[cycle_start + 1 :]):
                    chain = " -> ".join((*stack, dependency_name))
                    raise ConfigError(f"unguarded cyclic Rime dependency include: {chain}")

            body = expand(
                header,
                (*stack, dependency_name),
                current_name=dependency_name,
            )
            if body and not body.endswith("\n"):
                body += "\n"
            if dependency_name in pragma_once:
                macro = _once_macro(dependency_name)
                body = f"#ifndef {macro}\n#define {macro}\n{body}#endif /* {macro} */\n"
            result.append(
                f"{prefix}/* BEGIN YUKITOOLS-RIME DEPENDENCY: {dependency_name} */\n"
                f"{body}"
                f"/* END YUKITOOLS-RIME DEPENDENCY: {dependency_name} */"
                f"{suffix}{newline}"
            )
        return "".join(result)

    return expand(source, ())


def bundle_program_source(
    project_root: Path,
    source_path: Path,
    rime_options: Mapping[str, object],
    *,
    rime_kind: str | None = None,
) -> str:
    """Read a program and inline each declared Rime Plus dependency for upload."""

    project_root = Path(project_root).resolve(strict=True)
    source_path = Path(source_path)
    try:
        source_path.resolve(strict=False).relative_to(project_root)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"program source escapes the Rime project: {source_path}") from exc
    source = read_text(source_path)
    if not source.strip():
        return source

    dependencies = _dependency_names(rime_options)
    if not dependencies:
        return source
    kind = rime_kind
    if kind is None:
        suffix = source_path.suffix.casefold()
        if suffix == ".c":
            kind = "c"
        elif suffix in {
            ".cc",
            ".cpp",
            ".cxx",
            ".c++",
            ".h",
            ".hh",
            ".hpp",
            ".hxx",
        }:
            kind = "cxx"
    if kind not in {"c", "cxx"}:
        raise ConfigError("Rime dependencies can only be bundled for C or C++ programs")
    project_source = read_text(project_root / "PROJECT")
    library = _library_path(project_root, parse_project_library_dir(project_source))
    headers = {name: read_text(library / name) for name in dependencies}
    return _expanded_source(source, headers)


__all__ = ["bundle_program_source", "parse_project_library_dir"]
