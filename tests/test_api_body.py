from __future__ import annotations

import gzip
import hashlib

import pytest

from yukitools_rime.api.body import (
    Body,
    Part,
    is_safe_testcase_name,
    multipart,
    prepare_body,
    validate_testcase_name,
)


def test_small_body_stays_raw() -> None:
    body = prepare_body(b"short")
    assert body.data == b"short"
    assert not body.gzipped


def test_large_repetitive_body_is_complete_gzip_stream() -> None:
    original = b"1 2 3\n" * 1000
    body = prepare_body(original)
    assert body.gzipped
    assert gzip.decompress(body.data) == original
    assert len(body.data) < len(original)


def test_incompressible_body_is_never_kept_if_larger() -> None:
    original = bytes(range(256)) * 4
    body = prepare_body(original)
    if body.gzipped:
        assert len(body.data) < len(original)
        assert gzip.decompress(body.data) == original
    else:
        assert body.data == original


def test_multipart_has_exact_file_and_text_part_shape() -> None:
    boundary, body = multipart(
        [
            Part.file("newfiles", "sample.01.txt", b"1\n"),
            Part.text("lang", "cpp23"),
            Part.text("source", "int main() {}"),
        ],
        boundary="fixed",
    )
    assert boundary == "fixed"
    assert (
        b'Content-Disposition: form-data; name="newfiles"; '
        b'filename="sample.01.txt"\r\nContent-Type: text/plain\r\n\r\n1\n' in body
    )
    assert b'Content-Disposition: form-data; name="lang"\r\n\r\ncpp23\r\n' in body
    assert body.endswith(b"--fixed--\r\n")


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", ".hidden", "../evil", "a/b", "case-1.txt", "日本語.txt"],
)
def test_unsafe_testcase_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        validate_testcase_name(name)


@pytest.mark.parametrize("name", ["1.txt", "sample_01.txt", "many.dots.in"])
def test_safe_testcase_names_are_kept(name: str) -> None:
    assert validate_testcase_name(name) == name


def test_boundary_collision_is_rejected_when_explicit() -> None:
    with pytest.raises(ValueError):
        multipart([Part.text("source", "contains-fixed")], boundary="fixed")


@pytest.mark.parametrize(
    "name",
    [
        "CON",
        "con.txt",
        "AUX.data",
        "NUL",
        "COM1.log",
        "LPT9.txt",
        "case.",
        "case ",
        "case\\name",
        "case/name",
        "case:name",
    ],
)
def test_testcase_names_are_portable_to_windows(name: str) -> None:
    with pytest.raises(ValueError, match="テストケース名"):
        validate_testcase_name(name)


@pytest.mark.parametrize(
    "boundary",
    ["", "has space", 'quoted"', "slash/value", "colon:value", "日本語", "line\rbreak"],
)
def test_explicit_boundary_must_be_an_ascii_http_token(boundary: str) -> None:
    with pytest.raises(ValueError, match="multipart境界"):
        multipart([Part.text("field", "value")], boundary=boundary)


def test_explicit_boundary_accepts_all_http_token_punctuation() -> None:
    boundary = "Az09!#$%&'*+-.^_`|~"
    selected, body = multipart([Part.text("field", "value")], boundary=boundary)

    assert selected == boundary
    assert body.startswith(f"--{boundary}\r\n".encode())


def test_automatic_boundary_retries_after_a_content_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = iter(["collision", "safe"])
    monkeypatch.setattr(
        "yukitools_rime.api.body.secrets.token_hex",
        lambda _: next(generated),
    )

    boundary, _ = multipart([Part.text("source", "contains-yukitoolsrimecollision-marker")])

    assert boundary == "yukitoolsrimesafe"


def test_automatic_boundary_fails_after_64_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def colliding_token(_: int) -> str:
        nonlocal calls
        calls += 1
        return "collision"

    monkeypatch.setattr(
        "yukitools_rime.api.body.secrets.token_hex",
        colliding_token,
    )

    with pytest.raises(RuntimeError, match="multipart境界"):
        multipart([Part.text("source", "yukitoolsrimecollision")])

    assert calls == 64


def test_compression_threshold_is_exactly_one_kibibyte() -> None:
    below = prepare_body(b"x" * 1023)
    at_threshold = prepare_body(b"x" * 1024)

    assert below.data == b"x" * 1023
    assert not below.gzipped
    assert at_threshold.gzipped
    assert gzip.decompress(at_threshold.data) == b"x" * 1024


@pytest.mark.parametrize("name", ["", "bad-name", "bad.name"])
def test_multipart_field_names_are_restricted(name: str) -> None:
    with pytest.raises(ValueError, match="フィールド名"):
        multipart([Part.text(name, "value")], boundary="safe")


def test_body_compatibility_constructors_and_safe_name_predicate() -> None:
    body = Body.new(b"payload")

    assert body.bytes == b"payload"
    assert is_safe_testcase_name("sample.01")
    assert not is_safe_testcase_name("CON.txt")
    assert not is_safe_testcase_name("../unsafe")


def test_large_deterministic_high_entropy_body_stays_uncompressed() -> None:
    original = b"".join(hashlib.sha256(index.to_bytes(2, "big")).digest() for index in range(32))
    body = prepare_body(original)

    assert len(original) == 1024
    assert body.data == original
    assert not body.gzipped
