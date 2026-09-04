from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest


def _load_verifier() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "tools" / "verify_distribution.py"
    spec = importlib.util.spec_from_file_location("_verify_distribution_test_module", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_VERIFIER = _load_verifier()
VerificationError = cast(type[RuntimeError], _VERIFIER.VerificationError)
validate_archive_name = cast(Callable[..., str], _VERIFIER._validate_archive_name)
check_sensitive_content = cast(
    Callable[[str, bytes], None],
    _VERIFIER._check_sensitive_content,
)
parser_factory = cast(Callable[[], argparse.ArgumentParser], _VERIFIER._parser)


@pytest.mark.parametrize("name", ["/absolute", "../escape", r"a\b", "a/../b"])
def test_archive_names_reject_unsafe_paths(name: str) -> None:
    with pytest.raises(VerificationError):
        validate_archive_name(name)


def test_sensitive_content_allows_placeholder_and_rejects_token() -> None:
    check_sensitive_content("placeholder", b"YUKICODER_TOKEN=ypt_" + b"x" * 40)

    with pytest.raises(VerificationError):
        check_sensitive_content(
            "secret",
            b"YUKICODER_TOKEN=" + b"ypt_" + b"A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0",
        )


def test_reserved_backslash_unicode_crlf_and_init_smoke(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", cast(str, _VERIFIER._WINDOWS_PATH_SMOKE)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows drive semantics")
def test_real_windows_cross_drive_path_is_rejected() -> None:
    from yukitools_rime import rime_plugin

    assert rime_plugin._is_within(r"C:\root", r"D:\other") is False


def test_parser_accepts_wheel_only_install_mode() -> None:
    parsed = parser_factory().parse_args(["--skip-sdist-install"])

    assert parsed.skip_sdist_install is True
