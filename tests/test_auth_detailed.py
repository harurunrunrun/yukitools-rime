from __future__ import annotations

from pathlib import Path

import pytest

import yukitools_rime.auth as auth_module
from yukitools_rime.auth import (
    AuthError,
    DotenvError,
    load_dotenv,
    resolve_token,
    token_keys,
    validate_token,
)


def test_validate_token_strips_and_accepts_printable_ascii() -> None:
    assert validate_token("  abc-._~+/=  ") == "abc-._~+/="


@pytest.mark.parametrize("value", [None, 1, b"token"])
def test_validate_token_rejects_non_strings(value: object) -> None:
    with pytest.raises(AuthError, match="文字列"):
        validate_token(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", " ", "\t\n"])
def test_validate_token_rejects_empty_values(value: str) -> None:
    with pytest.raises(AuthError, match="空"):
        validate_token(value)


def test_token_key_order_is_problem_then_shared_then_legacy() -> None:
    assert token_keys(42) == (
        "YUKICODER_TOKEN_42",
        "YUKICODER_TOKEN",
        "YUKICODER_API_KEY",
    )


def test_dotenv_reads_bom_crlf_comments_and_export_spacing(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(
        b"\xef\xbb\xbf  # comment\r\n"
        b"export   FIRST = one \r\n"
        b'DOUBLE="two words"\r\n'
        b"SINGLE='three'\r\n"
    )
    assert load_dotenv(path) == {
        "FIRST": "one",
        "DOUBLE": "two words",
        "SINGLE": "three",
    }


@pytest.mark.parametrize(
    "text",
    [
        "A=value'\n",
        'A=value"\n',
        "A='x\"\n",
        "A=\"x'\n",
        "A=x\x00y\n",
        "export =x\n",
    ],
)
def test_dotenv_rejects_mismatched_quotes_nul_and_empty_key(tmp_path: Path, text: str) -> None:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(DotenvError):
        load_dotenv(path)


def test_dotenv_wraps_read_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_text("A=x\n", encoding="utf-8")
    original = Path.read_text

    def fail_read(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise PermissionError("denied")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(DotenvError, match="denied"):
        load_dotenv(path)


@pytest.mark.parametrize(
    ("environment", "dotenv_text", "expected"),
    [
        ({"YUKICODER_TOKEN_7": "problem-env"}, "", "problem-env"),
        (
            {"YUKICODER_TOKEN_7": "problem-env"},
            "YUKICODER_TOKEN_7=problem-file\n",
            "problem-env",
        ),
        (
            {"YUKICODER_TOKEN": "shared-env"},
            "YUKICODER_TOKEN_7=problem-file\n",
            "problem-file",
        ),
        (
            {"YUKICODER_API_KEY": "legacy-env"},
            "YUKICODER_TOKEN=shared-file\n",
            "shared-file",
        ),
        (
            {"YUKICODER_API_KEY": "legacy-env"},
            "YUKICODER_API_KEY=legacy-file\n",
            "legacy-env",
        ),
        (
            {"YUKICODER_TOKEN_7": "  ", "YUKICODER_TOKEN": "shared-env"},
            "YUKICODER_TOKEN_7= \n",
            "shared-env",
        ),
    ],
)
def test_token_precedence_matrix(
    tmp_path: Path,
    environment: dict[str, str],
    dotenv_text: str,
    expected: str,
) -> None:
    if dotenv_text:
        (tmp_path / ".env").write_text(dotenv_text, encoding="utf-8")
    assert resolve_token(tmp_path, 7, environ=environment) == expected


def test_resolve_token_uses_process_environment_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YUKICODER_TOKEN_3", "from-process")
    assert resolve_token(tmp_path, 3) == "from-process"


def test_malformed_dotenv_is_reraised_without_environment_fallback(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("broken\n", encoding="utf-8")
    with pytest.raises(DotenvError, match="KEY=VALUE"):
        resolve_token(tmp_path, 1, environ={})


def test_dotenv_unicode_decode_error_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("A=x\n", encoding="utf-8")

    def fail_decode(self: Path, *args: object, **kwargs: object) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

    monkeypatch.setattr(auth_module.Path, "read_text", fail_decode)
    with pytest.raises(DotenvError, match="読めません"):
        load_dotenv(path)
