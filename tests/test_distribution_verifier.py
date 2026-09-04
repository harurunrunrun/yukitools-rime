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
check_generated_case_name = cast(
    Callable[[str], None],
    _VERIFIER._check_generated_case_name,
)
is_exact_generated_setup_cfg = cast(
    Callable[[bytes], bool],
    _VERIFIER._is_exact_generated_setup_cfg,
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


def test_manifest_is_not_mistaken_for_a_generated_testcase() -> None:
    check_generated_case_name("MANIFEST.in")
    check_generated_case_name("yukitools_rime-1.2.3/MANIFEST.in")

    for name in ("sample.in", "problem/rime-out/sample.in", "sample.diff"):
        with pytest.raises(VerificationError):
            check_generated_case_name(name)


def test_generated_setup_cfg_accepts_only_exact_native_newlines() -> None:
    lf = b"[egg_info]\ntag_build = \ntag_date = 0\n\n"
    crlf = lf.replace(b"\n", b"\r\n")

    assert is_exact_generated_setup_cfg(lf)
    assert is_exact_generated_setup_cfg(crlf)
    assert not is_exact_generated_setup_cfg(lf.rstrip())
    assert not is_exact_generated_setup_cfg(crlf.rstrip())
    assert not is_exact_generated_setup_cfg(lf.replace(b"0", b"1"))
    assert not is_exact_generated_setup_cfg(b"[egg_info]\r\ntag_build = \ntag_date = 0\r\n\r\n")


def test_reserved_backslash_unicode_crlf_and_init_smoke(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-c", cast(str, _VERIFIER._WINDOWS_PATH_SMOKE)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
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


def test_support_sources_are_exactly_expected_in_sdist_not_wheel() -> None:
    root = Path(__file__).resolve().parents[1]
    spec = _VERIFIER._load_project(root)
    source_names = set(spec.source_files)

    support_sources = {
        "MANIFEST.in",
        "tools/check_coverage.py",
        "tools/verify_distribution.py",
    }
    assert support_sources <= source_names
    assert spec.test_files
    assert all(
        relative.startswith("tests/") and relative.endswith(".py") for relative in spec.test_files
    )

    sdist_files, _ = _VERIFIER._sdist_expected(spec)
    archive_root = f"{spec.archive_stem}-{spec.version}"
    for relative in support_sources | set(spec.test_files):
        assert f"{archive_root}/{relative}" in sdist_files

    wheel_files = _VERIFIER._wheel_expected_files(spec)
    assert "MANIFEST.in" not in wheel_files
    assert all(not member.startswith(("tests/", "tools/")) for member in wheel_files)
