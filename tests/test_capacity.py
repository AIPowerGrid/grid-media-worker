# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime

import pytest

from bridge.capacity import (
    effective_concurrency,
    load_schedule,
    validate_max_concurrency,
    validate_schedule,
)


def test_schedule_is_canonical_and_applies_local_window():
    schedule = validate_schedule(
        '[ { "days": "mon-fri", "start": "08:00", "end": "18:00", "concurrency": 0 } ]'
    )
    assert schedule == '[{"days":"mon-fri","start":"08:00","end":"18:00","concurrency":0}]'
    assert effective_concurrency(schedule, now=datetime(2026, 9, 4, 12, 0)) == 0
    assert effective_concurrency(schedule, now=datetime(2026, 9, 5, 12, 0)) == 1


def test_overnight_window_uses_the_starting_day():
    schedule = validate_schedule(
        '[{"days":"mon","start":"22:00","end":"02:00","concurrency":0}]'
    )
    assert effective_concurrency(schedule, now=datetime(2026, 9, 7, 23, 0)) == 0
    assert effective_concurrency(schedule, now=datetime(2026, 9, 8, 1, 0)) == 0
    assert effective_concurrency(schedule, now=datetime(2026, 9, 8, 3, 0)) == 1


@pytest.mark.parametrize(
    "value",
    [
        '[{"days":"funday","concurrency":0}]',
        '[{"days":"daily","start":"25:00","concurrency":0}]',
        '[{"days":"daily","concurrency":2}]',
        '[{"days":"daily"}]',
        '{"days":"daily","concurrency":0}',
    ],
)
def test_schedule_rejects_unsupported_values(value):
    with pytest.raises(ValueError):
        validate_schedule(value)


def test_media_concurrency_rejects_dead_parallel_setting():
    assert validate_max_concurrency("1") == 1
    with pytest.raises(ValueError, match="exactly one"):
        validate_max_concurrency("2")


def test_capacity_file_overrides_static_schedule_and_rejects_symlinks(tmp_path):
    capacity_file = tmp_path / "capacity.json"
    capacity_file.write_text('[{"days":"daily","concurrency":0}]', encoding="utf-8")

    assert load_schedule("", str(capacity_file)) == (
        '[{"days":"daily","concurrency":0}]'
    )
    capacity_file.write_text("", encoding="utf-8")
    assert load_schedule('[{"days":"daily","concurrency":0}]', str(capacity_file)) == ""

    target = tmp_path / "target.json"
    target.write_text("", encoding="utf-8")
    capacity_file.unlink()
    capacity_file.symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        load_schedule("", str(capacity_file))
