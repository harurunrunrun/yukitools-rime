from __future__ import annotations

import re
from pathlib import PurePath

from hypothesis import given, settings
from hypothesis import strategies as st

from yukitools_rime.errors import ValidationError
from yukitools_rime.files import normalize_text
from yukitools_rime.layout import TestCaseData as CaseData
from yukitools_rime.models import (
    ProblemConfig,
    ProblemSettings,
    validate_basename,
    validate_testcase_name,
)
from yukitools_rime.rime_config import parse_problem_config, render_problem_block
from yukitools_rime.testcase_sync import (
    batch_upload_files,
    compare_snapshots,
    estimate_upload_size,
)

WINDOWS_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)),
}
CASE_PATTERN = re.compile(r"[A-Za-z0-9._-]+\Z")
CASE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"

safe_case_names = st.builds(
    lambda first, tail: first + "".join(tail),
    st.sampled_from(CASE_ALPHABET.replace(".", "")),
    st.lists(st.sampled_from(CASE_ALPHABET), max_size=24),
).filter(
    lambda name: not name.endswith(".") and name.split(".", 1)[0].upper() not in WINDOWS_DEVICES
)

json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**31), max_value=2**31 - 1)
    | st.floats(allow_nan=False, allow_infinity=False, width=32)
    | st.text(max_size=20)
)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(
            st.text(min_size=1, max_size=12),
            children,
            max_size=4,
        )
    ),
    max_leaves=12,
)


@settings(max_examples=150, deadline=None)
@given(st.text(max_size=48))
def test_accepted_basename_always_satisfies_portability_invariants(name: str) -> None:
    try:
        accepted = validate_basename(name)
    except ValidationError:
        return

    assert accepted == name
    assert name not in {"", ".", ".."}
    assert "\x00" not in name
    assert PurePath(name).name == name
    assert "/" not in name and "\\" not in name
    assert not name.endswith((".", " "))
    assert not any(ord(character) < 32 or character in '<>:"|?*' for character in name)
    assert name.split(".", 1)[0].upper() not in WINDOWS_DEVICES


@settings(max_examples=150, deadline=None)
@given(safe_case_names)
def test_generated_portable_testcase_names_round_trip(name: str) -> None:
    assert validate_testcase_name(name) == name


@settings(max_examples=150, deadline=None)
@given(st.text(max_size=48))
def test_accepted_testcase_name_matches_the_documented_grammar(name: str) -> None:
    try:
        accepted = validate_testcase_name(name)
    except ValidationError:
        return

    assert accepted == name
    assert CASE_PATTERN.fullmatch(name)
    assert not name.startswith(".")
    assert not name.endswith((".", " "))
    assert name.split(".", 1)[0].upper() not in WINDOWS_DEVICES


@settings(max_examples=100, deadline=None)
@given(st.dictionaries(st.text(min_size=1, max_size=12), json_values, max_size=6))
def test_literal_problem_configuration_round_trips(rime_options: dict[str, object]) -> None:
    config = ProblemConfig(
        problem_id=123,
        settings=ProblemSettings(
            title="Property",
            tags="test",
            level=1.5,
            time_limit_ms=2000,
            memory_limit=512,
            eps_mode="-",
            eps="0",
            wip=False,
            recruiting_tester=True,
            problem_type=0,
            judge_type=1,
            show_ans=True,
            enable_pure_judge=True,
            force_single_server_judge=False,
            allowed_langs=("cpp23",),
        ),
        rime_id="P",
        reference_solution="correct",
        rime_options=rime_options,
    )

    parsed = parse_problem_config(render_problem_block(config))

    assert parsed == config


case_snapshots = st.dictionaries(
    safe_case_names,
    st.tuples(st.binary(max_size=12), st.binary(max_size=12)),
    max_size=12,
)


def make_snapshot(raw: dict[str, tuple[bytes, bytes]]) -> dict[str, CaseData]:
    return {
        name: CaseData(name, input_data, output_data)
        for name, (input_data, output_data) in raw.items()
    }


@settings(max_examples=150, deadline=None)
@given(case_snapshots, case_snapshots)
def test_snapshot_comparison_partitions_all_names(
    local_raw: dict[str, tuple[bytes, bytes]],
    remote_raw: dict[str, tuple[bytes, bytes]],
) -> None:
    local = make_snapshot(local_raw)
    remote = make_snapshot(remote_raw)

    changes = compare_snapshots(local, remote)

    assert set(changes.added) == remote.keys() - local.keys()
    assert set(changes.removed) == local.keys() - remote.keys()
    assert set(changes.changed) == {
        name for name in local.keys() & remote.keys() if local[name] != remote[name]
    }
    assert not (set(changes.added) & set(changes.changed))
    assert not (set(changes.removed) & set(changes.changed))
    assert changes.counts == (
        len(changes.added),
        len(changes.changed),
        len(changes.removed),
    )


@settings(max_examples=150, deadline=None)
@given(
    st.dictionaries(safe_case_names, st.binary(max_size=2048), max_size=30),
    st.integers(min_value=1, max_value=10),
    st.integers(min_value=1, max_value=4096),
)
def test_upload_batches_preserve_every_file_and_both_limits(
    files: dict[str, bytes],
    max_files: int,
    max_bytes: int,
) -> None:
    batches = batch_upload_files(files, max_files=max_files, max_bytes=max_bytes)
    flattened = [(name, content) for batch in batches for name, content in batch.items()]

    assert [name for name, _ in flattened] == sorted(files)
    assert dict(flattened) == files
    assert all(1 <= len(batch) <= max_files for batch in batches)
    for batch in batches:
        estimated = sum(estimate_upload_size(name, content) for name, content in batch.items())
        assert estimated <= max_bytes or len(batch) == 1


@settings(max_examples=150, deadline=None)
@given(st.text(max_size=200))
def test_text_normalization_is_idempotent_and_removes_carriage_returns(text: str) -> None:
    normalized = normalize_text(text)

    assert normalize_text(normalized) == normalized
    assert "\r" not in normalized
    assert not normalized.startswith("\ufeff")
