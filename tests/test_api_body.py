from __future__ import annotations

import gzip

import pytest

from yukitools_rime.api.body import Part, multipart, prepare_body, validate_testcase_name


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
