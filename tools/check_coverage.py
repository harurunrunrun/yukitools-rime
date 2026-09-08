"""Enforce the repository's unrounded coverage policy from coverage.py JSON."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TOTAL_STATEMENT_MINIMUM = 95
TOTAL_BRANCH_MINIMUM = 95
FILE_STATEMENT_MINIMUM = 90
FILE_BRANCH_MINIMUM = 90

REQUIRED_FILES = (
    "src/yukitools_rime/api/client.py",
    "src/yukitools_rime/files.py",
    "src/yukitools_rime/commands/scaffold.py",
    "src/yukitools_rime/commands/sync.py",
    "src/yukitools_rime/testcase_sync.py",
    "src/yukitools_rime/commands/push.py",
    "src/yukitools_rime/source_bundle.py",
)


class CoverageReportError(ValueError):
    """Raised when coverage JSON does not have the expected schema."""


def _mapping(value: object, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CoverageReportError(f"{location} must be an object")
    return value


def _count(summary: Mapping[str, Any], key: str, location: str) -> int:
    value = summary.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CoverageReportError(f"{location}.{key} must be a non-negative integer")
    return value


def _normalized_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _percentage(covered: int, total: int) -> str:
    if total == 0:
        return "not measurable"
    return f"{covered * 100 / total:.6f}%"


def _check_ratio(
    *,
    label: str,
    covered: int,
    total: int,
    minimum: int,
    failures: list[str],
) -> str:
    result = f"{label}: {covered}/{total} ({_percentage(covered, total)})"
    if total == 0:
        failures.append(f"{label} has no measurable items")
    elif covered * 100 < minimum * total:
        failures.append(f"{result}; required >= {minimum}%")
    return result


def validate_coverage(report: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Return informational result lines and all policy failures."""

    totals = _mapping(report.get("totals"), "totals")
    files = _mapping(report.get("files"), "files")
    failures: list[str] = []
    results: list[str] = []

    results.append(
        _check_ratio(
            label="total statements",
            covered=_count(totals, "covered_lines", "totals"),
            total=_count(totals, "num_statements", "totals"),
            minimum=TOTAL_STATEMENT_MINIMUM,
            failures=failures,
        )
    )
    results.append(
        _check_ratio(
            label="total branches",
            covered=_count(totals, "covered_branches", "totals"),
            total=_count(totals, "num_branches", "totals"),
            minimum=TOTAL_BRANCH_MINIMUM,
            failures=failures,
        )
    )

    normalized_files: dict[str, object] = {}
    for raw_name, details in files.items():
        if not isinstance(raw_name, str):
            raise CoverageReportError("every files key must be a string")
        normalized_files[_normalized_path(raw_name)] = details

    for required in REQUIRED_FILES:
        matches = [
            (name, details)
            for name, details in normalized_files.items()
            if name == required or name.endswith(f"/{required}")
        ]
        if not matches:
            failures.append(f"required coverage file is missing: {required}")
            continue
        if len(matches) != 1:
            failures.append(f"coverage file is ambiguous: {required}")
            continue
        name, details = matches[0]
        detail_mapping = _mapping(details, f"files[{name!r}]")
        summary = _mapping(detail_mapping.get("summary"), f"files[{name!r}].summary")
        results.append(
            _check_ratio(
                label=required,
                covered=_count(
                    summary,
                    "covered_lines",
                    f"files[{name!r}].summary",
                ),
                total=_count(
                    summary,
                    "num_statements",
                    f"files[{name!r}].summary",
                ),
                minimum=FILE_STATEMENT_MINIMUM,
                failures=failures,
            )
        )
        results.append(
            _check_ratio(
                label=f"{required} branches",
                covered=_count(
                    summary,
                    "covered_branches",
                    f"files[{name!r}].summary",
                ),
                total=_count(
                    summary,
                    "num_branches",
                    f"files[{name!r}].summary",
                ),
                minimum=FILE_BRANCH_MINIMUM,
                failures=failures,
            )
        )

    return results, failures


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enforce unrounded repository coverage thresholds."
    )
    parser.add_argument(
        "report",
        nargs="?",
        type=Path,
        default=Path("coverage.json"),
        help="coverage.py JSON report (default: coverage.json)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        raw_report = json.loads(args.report.read_text(encoding="utf-8"))
        report = _mapping(raw_report, "coverage report")
        results, failures = validate_coverage(report)
    except (OSError, json.JSONDecodeError, CoverageReportError) as exc:
        print(f"coverage validation error: {exc}", file=sys.stderr)
        return 2

    for result in results:
        print(result)
    if failures:
        for failure in failures:
            print(f"coverage policy failure: {failure}", file=sys.stderr)
        return 1
    print("coverage policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
