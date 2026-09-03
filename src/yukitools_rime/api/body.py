"""Byte-exact JSON and multipart request body preparation."""

from __future__ import annotations

import builtins
import gzip
import re
import secrets
from dataclasses import dataclass

MIN_COMPRESS_BYTES = 1024
_FIELD_RE = re.compile(r"[A-Za-z0-9_]+\Z")
_FILENAME_RE = re.compile(r"[A-Za-z0-9._]+\Z")


@dataclass(frozen=True, slots=True)
class Body:
    """A request body and whether ``Content-Encoding: gzip`` is required."""

    data: bytes
    gzipped: bool = False

    @property
    def bytes(self) -> bytes:
        """Compatibility spelling matching the reference implementation."""

        return self.data

    @classmethod
    def new(cls, data: builtins.bytes) -> Body:
        return prepare_body(data)


@dataclass(frozen=True, slots=True)
class Part:
    """One manually encoded ``multipart/form-data`` part."""

    name: str
    content: bytes
    filename: str | None = None

    @classmethod
    def text(cls, name: str, value: str) -> Part:
        return cls(name=name, content=value.encode("utf-8"))

    @classmethod
    def file(cls, name: str, filename: str, content: bytes) -> Part:
        return cls(name=name, filename=filename, content=bytes(content))


MultipartPart = Part


def is_safe_testcase_name(name: str) -> bool:
    """Whether *name* is safe both as a local basename and an API path segment."""

    return bool(
        name
        and name not in {".", ".."}
        and not name.startswith(".")
        and _FILENAME_RE.fullmatch(name)
    )


def validate_testcase_name(name: str) -> str:
    if not is_safe_testcase_name(name):
        raise ValueError(f"テストケース名が不正です: {name!r}")
    return name


def prepare_body(data: bytes) -> Body:
    """Compress a complete body only when it is at least 1 KiB and gets smaller."""

    raw = bytes(data)
    if len(raw) < MIN_COMPRESS_BYTES:
        return Body(raw)
    compressed = gzip.compress(raw, mtime=0)
    return Body(compressed, True) if len(compressed) < len(raw) else Body(raw)


def _pick_boundary(parts: tuple[Part, ...]) -> str:
    for _ in range(64):
        candidate = f"yukitoolsrime{secrets.token_hex(16)}"
        marker = candidate.encode("ascii")
        if all(marker not in part.content for part in parts):
            return candidate
    raise RuntimeError("multipart境界を安全に選べませんでした")


def multipart(
    parts: list[Part] | tuple[Part, ...], *, boundary: str | None = None
) -> tuple[str, bytes]:
    """Build one complete multipart body suitable for optional gzip compression."""

    normalized = tuple(parts)
    for part in normalized:
        if not _FIELD_RE.fullmatch(part.name):
            raise ValueError(f"multipartフィールド名が不正です: {part.name!r}")
        if part.filename is not None:
            validate_testcase_name(part.filename)

    selected = boundary or _pick_boundary(normalized)
    if not selected or any(ch in selected for ch in '\r\n"'):
        raise ValueError("multipart境界が不正です")
    marker = selected.encode("ascii", errors="strict")
    if any(marker in part.content for part in normalized):
        raise ValueError("multipart境界がパート本文に含まれています")

    chunks: list[bytes] = []
    for part in normalized:
        chunks.append(b"--" + marker + b"\r\n")
        disposition = f'Content-Disposition: form-data; name="{part.name}"'
        if part.filename is not None:
            disposition += f'; filename="{part.filename}"'
        chunks.append(disposition.encode("ascii") + b"\r\n")
        if part.filename is not None:
            chunks.append(b"Content-Type: text/plain\r\n")
        chunks.append(b"\r\n")
        chunks.append(part.content)
        chunks.append(b"\r\n")
    chunks.append(b"--" + marker + b"--\r\n")
    return selected, b"".join(chunks)


build_multipart = multipart


__all__ = [
    "MIN_COMPRESS_BYTES",
    "Body",
    "MultipartPart",
    "Part",
    "build_multipart",
    "is_safe_testcase_name",
    "multipart",
    "prepare_body",
    "validate_testcase_name",
]
