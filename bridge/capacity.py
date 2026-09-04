# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local-time capacity scheduling for the media worker."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


MAX_SCHEDULE_WINDOWS = 32
MAX_MEDIA_CONCURRENCY = 1
_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def validate_max_concurrency(value: object) -> int:
    """Validate the real media protocol limit instead of accepting a dead knob."""
    if isinstance(value, bool):
        raise ValueError("GRID_THREADS must be an integer")
    try:
        concurrency = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("GRID_THREADS must be an integer") from exc
    if concurrency != MAX_MEDIA_CONCURRENCY:
        raise ValueError(
            "media workers currently support exactly one simultaneous job"
        )
    return concurrency


def validate_schedule(value: object) -> str:
    """Validate and canonicalize a bounded local-time capacity schedule."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("Schedule must be JSON text")
    try:
        windows = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Schedule must be valid JSON") from exc
    if not isinstance(windows, list) or len(windows) > MAX_SCHEDULE_WINDOWS:
        raise ValueError(
            f"Schedule must be a list of at most {MAX_SCHEDULE_WINDOWS} windows"
        )

    normalized: list[dict[str, Any]] = []
    for window in windows:
        if not isinstance(window, dict):
            raise ValueError("Each schedule window must be an object")
        if set(window) - {"days", "start", "end", "concurrency"}:
            raise ValueError("Schedule window contains an unknown field")

        days = str(window.get("days") or "daily").strip().lower()
        _parse_days(days, strict=True)
        item: dict[str, Any] = {"days": days}
        for field in ("start", "end"):
            if field in window:
                text = str(window[field])
                if not _TIME_RE.fullmatch(text):
                    raise ValueError(f"Schedule {field} must use 24-hour HH:MM")
                item[field] = text

        concurrency = window.get("concurrency")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int):
            raise ValueError("Schedule concurrency must be 0 or 1")
        if concurrency not in {0, MAX_MEDIA_CONCURRENCY}:
            raise ValueError("Schedule concurrency must be 0 or 1")
        item["concurrency"] = concurrency
        normalized.append(item)
    return json.dumps(normalized, separators=(",", ":"))


def effective_concurrency(
    schedule: str,
    *,
    max_concurrency: int = MAX_MEDIA_CONCURRENCY,
    now: datetime | None = None,
) -> int:
    """Return 0 (paused) or 1 (available) for the current local time."""
    maximum = validate_max_concurrency(max_concurrency)
    canonical = validate_schedule(schedule)
    if not canonical:
        return maximum

    current = now or datetime.now()
    for window in json.loads(canonical):
        if _window_active(current, window):
            return int(window["concurrency"])
    return maximum


def load_schedule(default: str, capacity_file: str = "") -> str:
    """Load a locally managed schedule override without weakening validation."""
    path_text = str(capacity_file or "").strip()
    if not path_text:
        return validate_schedule(default)
    path = Path(path_text).expanduser()
    if not path.exists():
        return validate_schedule(default)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Capacity schedule file must be a regular file")
    if path.stat().st_size > 8192:
        raise ValueError("Capacity schedule file is too large")
    return validate_schedule(path.read_text(encoding="utf-8"))


def _parse_days(spec: str, *, strict: bool = False) -> set[int]:
    if spec in {"", "*", "all", "daily"}:
        return set(range(7))
    days: set[int] = set()
    for part in spec.split(","):
        bounds = [item.strip() for item in part.split("-")]
        if len(bounds) not in {1, 2} or any(item not in _DAYS for item in bounds):
            if strict:
                raise ValueError("Schedule days must use mon-sun names")
            return set()
        start = _DAYS[bounds[0]]
        end = _DAYS[bounds[-1]]
        day = start
        while True:
            days.add(day)
            if day == end:
                break
            day = (day + 1) % 7
    return days


def _minutes(value: str | None, default: int) -> int:
    if value is None:
        return default
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _window_active(now: datetime, window: dict[str, Any]) -> bool:
    days = _parse_days(window["days"])
    start = _minutes(window.get("start"), 0)
    end = _minutes(window.get("end"), 24 * 60)
    current = now.hour * 60 + now.minute
    if start <= end:
        return now.weekday() in days and start <= current < end

    # An overnight window belongs to its starting day. Tuesday at 01:00 is
    # therefore inside a Monday 22:00-02:00 window.
    return (now.weekday() in days and current >= start) or (
        (now.weekday() - 1) % 7 in days and current < end
    )
