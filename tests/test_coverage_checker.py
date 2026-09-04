from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from runpy import run_path
from typing import Any, cast

import pytest

_CHECKER = run_path(str(Path(__file__).resolve().parents[1] / "tools" / "check_coverage.py"))
REQUIRED_FILES = cast(tuple[str, ...], _CHECKER["REQUIRED_FILES"])
CoverageReportError = cast(type[ValueError], _CHECKER["CoverageReportError"])
validate_coverage = cast(
    Callable[[Mapping[str, Any]], tuple[list[str], list[str]]],
    _CHECKER["validate_coverage"],
)
main = cast(Callable[[Sequence[str] | None], int], _CHECKER["main"])


def _summary(
    *,
    covered_lines: int = 9,
    num_statements: int = 10,
    covered_branches: int = 9,
    num_branches: int = 10,
) -> dict[str, int]:
    return {
        "covered_lines": covered_lines,
        "num_statements": num_statements,
        "covered_branches": covered_branches,
        "num_branches": num_branches,
    }


def _valid_report(*, windows_paths: bool = False) -> dict[str, Any]:
    files: dict[str, object] = {}
    for required in REQUIRED_FILES:
        if windows_paths:
            name = "C:\\actions\\work\\repo\\" + required.replace("/", "\\")
        else:
            name = required
        files[name] = {"summary": _summary()}
    return {
        "totals": {
            **_summary(
                covered_lines=95,
                num_statements=100,
                covered_branches=95,
                num_branches=100,
            ),
            "percent_covered_display": "100",
        },
        "files": files,
    }


def test_exact_total_and_per_file_thresholds_pass() -> None:
    results, failures = validate_coverage(_valid_report())

    assert failures == []
    assert "total statements: 95/100 (95.000000%)" in results
    assert f"{REQUIRED_FILES[0]} branches: 9/10 (90.000000%)" in results


def test_unrounded_total_thresholds_fail_despite_display_value() -> None:
    report = _valid_report()
    report["totals"] = {
        **_summary(
            covered_lines=949,
            num_statements=999,
            covered_branches=949,
            num_branches=999,
        ),
        "percent_covered_display": "100",
    }

    _, failures = validate_coverage(report)

    assert any("total statements: 949/999" in failure for failure in failures)
    assert any("total branches: 949/999" in failure for failure in failures)


def test_per_file_statement_and_branch_thresholds_are_independent() -> None:
    report = _valid_report()
    files = report["files"]
    assert isinstance(files, dict)
    files[REQUIRED_FILES[0]] = {
        "summary": _summary(
            covered_lines=9,
            num_statements=10,
            covered_branches=8,
            num_branches=10,
        )
    }

    _, failures = validate_coverage(report)

    assert not any(failure.startswith(f"{REQUIRED_FILES[0]}:") for failure in failures)
    assert any(failure.startswith(f"{REQUIRED_FILES[0]} branches:") for failure in failures)


def test_windows_absolute_paths_are_normalized() -> None:
    _, failures = validate_coverage(_valid_report(windows_paths=True))

    assert failures == []


def test_missing_required_file_is_reported() -> None:
    report = _valid_report()
    files = report["files"]
    assert isinstance(files, dict)
    files.pop(REQUIRED_FILES[-1])

    _, failures = validate_coverage(report)

    assert f"required coverage file is missing: {REQUIRED_FILES[-1]}" in failures


def test_ambiguous_required_file_is_reported() -> None:
    report = _valid_report()
    files = report["files"]
    assert isinstance(files, dict)
    files[f"/second/checkout/{REQUIRED_FILES[0]}"] = {"summary": _summary()}

    _, failures = validate_coverage(report)

    assert f"coverage file is ambiguous: {REQUIRED_FILES[0]}" in failures


def test_zero_totals_and_zero_file_branches_are_rejected() -> None:
    report = _valid_report()
    report["totals"] = _summary(
        covered_lines=0,
        num_statements=0,
        covered_branches=0,
        num_branches=0,
    )
    files = report["files"]
    assert isinstance(files, dict)
    files[REQUIRED_FILES[0]] = {"summary": _summary(covered_branches=0, num_branches=0)}

    _, failures = validate_coverage(report)

    assert "total statements has no measurable items" in failures
    assert "total branches has no measurable items" in failures
    assert f"{REQUIRED_FILES[0]} branches has no measurable items" in failures


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"totals": [], "files": {}},
        {"totals": _summary(), "files": []},
        {
            "totals": _summary(),
            "files": {required: None for required in REQUIRED_FILES},
        },
    ],
)
def test_malformed_coverage_schemas_raise(report: dict[str, object]) -> None:
    with pytest.raises(CoverageReportError):
        validate_coverage(report)


def test_main_reports_malformed_json(tmp_path: Path) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": []}), encoding="utf-8")

    assert main([str(report)]) == 2
