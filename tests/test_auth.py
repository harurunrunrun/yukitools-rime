from __future__ import annotations

from pathlib import Path

import pytest

from yukitools_rime.auth import DotenvError, MissingTokenError, load_dotenv, resolve_token


def test_load_dotenv_parses_supported_strict_syntax(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# comment\nTOKEN=bare\nexport QUOTED=\"x y\"\nSINGLE='z'\nEMPTY=\n",
        encoding="utf-8",
    )

    assert load_dotenv(path) == {
        "TOKEN": "bare",
        "QUOTED": "x y",
        "SINGLE": "z",
        "EMPTY": "",
    }


@pytest.mark.parametrize(
    "text",
    [
        "BROKEN\n",
        "1INVALID=x\n",
        "A='unterminated\n",
        "A=x\nA=y\n",
    ],
)
def test_load_dotenv_rejects_ambiguous_lines(tmp_path: Path, text: str) -> None:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(DotenvError):
        load_dotenv(path)


def test_missing_dotenv_is_empty(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / ".env") == {}


def test_token_precedence_is_key_first_then_environment(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "YUKICODER_TOKEN_42=per-problem-file\n"
        "YUKICODER_TOKEN=shared-file\n"
        "YUKICODER_API_KEY=api-file\n",
        encoding="utf-8",
    )
    # A per-problem dotenv value beats a lower-priority shared environment key.
    assert (
        resolve_token(tmp_path, 42, environ={"YUKICODER_TOKEN": "shared-env"}) == "per-problem-file"
    )
    # Within the same key, the process environment wins.
    assert (
        resolve_token(
            tmp_path,
            42,
            environ={"YUKICODER_TOKEN_42": "per-problem-env"},
        )
        == "per-problem-env"
    )


def test_empty_tokens_are_skipped(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "YUKICODER_TOKEN_1=  \nYUKICODER_API_KEY=api-key\n", encoding="utf-8"
    )
    assert resolve_token(tmp_path, 1, environ={}) == "api-key"


def test_malformed_dotenv_does_not_expose_or_block_environment_token(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("broken\n", encoding="utf-8")
    with pytest.warns(RuntimeWarning) as caught:
        token = resolve_token(tmp_path, 1, environ={"YUKICODER_TOKEN_1": "secret"})
    assert token == "secret"
    assert "secret" not in str(caught[0].message)


def test_missing_token_error_names_keys_but_never_a_value(tmp_path: Path) -> None:
    with pytest.raises(MissingTokenError) as caught:
        resolve_token(tmp_path, 99, environ={"YUKICODER_TOKEN": "  "})
    assert "YUKICODER_TOKEN_99" in str(caught.value)


def test_load_dotenv_rejects_symlink_without_reading_target(tmp_path: Path) -> None:
    secret = "must-not-leak"
    target = tmp_path / "outside.env"
    target.write_text(f"YUKICODER_TOKEN={secret}\n", encoding="utf-8")
    link = tmp_path / ".env"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(DotenvError) as caught:
        load_dotenv(link)

    assert secret not in str(caught.value)
