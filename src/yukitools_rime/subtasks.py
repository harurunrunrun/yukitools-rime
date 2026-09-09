"""Local JSON partial-score configuration, shared by synchronization commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from yukitools_rime.api.types import SubtaskSet
from yukitools_rime.errors import ValidationError
from yukitools_rime.files import read_text


class SubtaskClient(Protocol):
    def get_subtask(self, problem_id: int) -> SubtaskSet: ...


def fetch_subtasks(client: object, problem_id: int) -> SubtaskSet:
    """Allow pre-v0.5 injected clients; actual HTTP errors must propagate."""
    getter = getattr(client, "get_subtask", None)
    if getter is None:
        return SubtaskSet()
    result = getter(problem_id)
    if not isinstance(result, SubtaskSet):
        raise ValidationError("subtask API returned an unexpected response type")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate JSON key")
        data[key] = value
    return data


def read_subtasks(problem_dir: Path) -> SubtaskSet | None:
    path = problem_dir / "subtask.json"
    if not path.exists() and not path.is_symlink():
        return None
    text = read_text(path)
    error: ValidationError | None = None
    try:
        raw = json.loads(text, object_pairs_hook=_unique_object)
        result = SubtaskSet.from_api_dict(raw)
        if set(raw) != {"subtasks"} or any(
            set(task) - {"name", "prefixes", "score", "description"} for task in raw["subtasks"]
        ):
            raise ValueError("unknown subtask keys")
    except (ValueError, RecursionError):
        error = ValidationError(
            f"{path}: invalid subtask JSON; expected subtasks with prefixes (string array), "
            "score (integer 0..100, total 100), optional name/description (strings); "
            'use {"subtasks": []} to clear the configuration'
        )
    if error is not None:
        raise error
    return result


def render_subtasks(config: SubtaskSet) -> str:
    return json.dumps(config.to_api_dict(), ensure_ascii=False, indent=2) + "\n"
