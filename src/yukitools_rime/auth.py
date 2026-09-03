"""Credential loading for yukicoder API calls.

Credentials deliberately come only from the process environment or the project
``.env`` file. Keeping command line options out of this module also keeps tokens
out of shell histories and process listings.
"""

from __future__ import annotations

import os
import re
import warnings
from collections.abc import Mapping
from pathlib import Path

DOTENV_FILE = ".env"
_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class AuthError(ValueError):
    """Base class for errors while loading credentials."""


class DotenvError(AuthError):
    """Raised when a ``.env`` file is not in the supported strict format."""


class MissingTokenError(AuthError):
    """Raised when no usable yukicoder token can be found."""


def load_dotenv(path: str | Path) -> dict[str, str]:
    """Read a small, deterministic subset of dotenv syntax.

    Blank lines and lines whose first non-space character is ``#`` are ignored.
    Every other line must be ``KEY=VALUE`` (optionally prefixed by ``export ``).
    A value may be wholly enclosed in matching single or double quotes. Variable
    expansion, escapes, inline comments, and multiline values are intentionally
    not interpreted.
    """

    dotenv_path = Path(path)
    if not dotenv_path.is_file():
        return {}
    try:
        text = dotenv_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise DotenvError(f"{dotenv_path} を読めませんでした: {exc}") from exc

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise DotenvError(
                f"{dotenv_path}:{line_number} は KEY=VALUE の形式ではありません"
            )
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip()
        if not _KEY_RE.fullmatch(key):
            raise DotenvError(f"{dotenv_path}:{line_number} のキーが不正です: {key!r}")
        if key in values:
            raise DotenvError(f"{dotenv_path}:{line_number} でキー {key!r} が重複しています")

        value = raw_value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise DotenvError(
                    f"{dotenv_path}:{line_number} の引用符が閉じられていません"
                )
            value = value[1:-1]
        elif value.endswith(("'", '"')):
            raise DotenvError(f"{dotenv_path}:{line_number} の引用符が対応していません")
        if "\x00" in value:
            raise DotenvError(f"{dotenv_path}:{line_number} の値にNUL文字があります")
        values[key] = value
    return values


def token_keys(problem_id: int) -> tuple[str, str, str]:
    """Return credential variable names in descending priority."""

    return (
        f"YUKICODER_TOKEN_{problem_id}",
        "YUKICODER_TOKEN",
        "YUKICODER_API_KEY",
    )


def _select_token(
    keys: tuple[str, ...],
    environ: Mapping[str, str],
    dotenv: Mapping[str, str],
) -> str | None:
    # Key priority is stronger than source priority. For the same key, a CI
    # secret in the environment overrides the checked-out/local dotenv value.
    for key in keys:
        for source in (environ, dotenv):
            value = source.get(key)
            if value is not None and value.strip():
                return value.strip()
    return None


def resolve_token(
    project_root: str | Path,
    problem_id: int,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve a problem token without ever including its value in an error.

    Priority is ``YUKICODER_TOKEN_<id>``, ``YUKICODER_TOKEN``, then
    ``YUKICODER_API_KEY``. For each name the process environment wins over
    ``.env``. If the dotenv file is malformed but an environment credential is
    usable, that credential is returned and a token-free warning is emitted.
    """

    environment = os.environ if environ is None else environ
    keys = token_keys(problem_id)
    dotenv_error: DotenvError | None = None
    try:
        dotenv = load_dotenv(Path(project_root) / DOTENV_FILE)
    except DotenvError as exc:
        dotenv = {}
        dotenv_error = exc

    token = _select_token(keys, environment, dotenv)
    if token is not None:
        if dotenv_error is not None:
            warnings.warn(
                f"{dotenv_error}。環境変数のトークンを使います。",
                RuntimeWarning,
                stacklevel=2,
            )
        return token
    if dotenv_error is not None:
        raise dotenv_error
    names = ", ".join(keys)
    raise MissingTokenError(
        f"トークンが見つかりません。環境変数または {DOTENV_FILE} に "
        f"次のいずれかを設定してください: {names}"
    )


__all__ = [
    "DOTENV_FILE",
    "AuthError",
    "DotenvError",
    "MissingTokenError",
    "load_dotenv",
    "resolve_token",
    "token_keys",
]
