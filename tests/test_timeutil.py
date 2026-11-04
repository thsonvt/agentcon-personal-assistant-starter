from datetime import datetime, time

import pytest

from app.timeutil import SGT, compute_free_slots, fmt_dt, fmt_until, parse_dt

WORK_START, WORK_END = time(9), time(21)


def sgt(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=SGT)


def test_parse_naive_time_is_singapore_time():
    # Review Focus: the model often omits the offset; it must mean SGT, not UTC.
    assert parse_dt("2026-09-28T15:00") == sgt(2026, 9, 28, 15)


def test_parse_utc_time_is_converted_to_singapore():
    assert parse_dt("2026-09-28T07:00:00Z") == sgt(2026, 9, 28, 15)


def test_parse_garbage_gives_readable_error():
    with pytest.raises(ValueError, match="ISO 8601"):
        parse_dt("tomorrow at 3")


def test_fmt_dt():
    assert fmt_dt(sgt(2026, 9, 28, 15)) == "Mon 28 Sep, 3:00pm"
    assert fmt_dt(sgt(2026, 9, 28, 9, 5)) == "Mon 28 Sep, 9:05am"


@pytest.mark.parametrize(
    "start,expected",
    [
        (sgt(2026, 9, 28, 16, 15), "in 2h 15m"),
        (sgt(2026, 9, 28, 14, 30), "in 30m"),
        (sgt(2026, 9, 30, 18), "in 2d 4h"),
        (sgt(2026, 9, 28, 13, 50), "started 10m ago"),
    ],
)
def test_fmt_until(start, expected):
    assert fmt_until(start, now=sgt(2026, 9, 28, 14)) == expected


def test_free_slots_in_empty_morning():
    slots = compute_free_slots([], sgt(2026, 9, 28, 9), sgt(2026, 9, 28, 12), 60, WORK_START, WORK_END)
    assert slots == [
        (sgt(2026, 9, 28, 9), sgt(2026, 9, 28, 10)),
        (sgt(2026, 9, 28, 10), sgt(2026, 9, 28, 11)),
        (sgt(2026, 9, 28, 11), sgt(2026, 9, 28, 12)),
    ]


def test_free_slots_skip_busy_time():
    busy = [(sgt(2026, 9, 28, 10), sgt(2026, 9, 28, 11))]
    slots = compute_free_slots(busy, sgt(2026, 9, 28, 9), sgt(2026, 9, 28, 12), 60, WORK_START, WORK_END)
    assert slots == [
        (sgt(2026, 9, 28, 9), sgt(2026, 9, 28, 10)),
        (sgt(2026, 9, 28, 11), sgt(2026, 9, 28, 12)),
    ]


def test_free_slots_respect_working_hours():
    slots = compute_free_slots([], sgt(2026, 9, 28, 6), sgt(2026, 9, 28, 11), 60, WORK_START, WORK_END)
    assert slots[0][0] == sgt(2026, 9, 28, 9)


def test_free_slots_roll_over_to_next_day():
    slots = compute_free_slots([], sgt(2026, 9, 28, 20), sgt(2026, 9, 29, 11), 60, WORK_START, WORK_END)
    assert slots == [
        (sgt(2026, 9, 28, 20), sgt(2026, 9, 28, 21)),
        (sgt(2026, 9, 29, 9), sgt(2026, 9, 29, 10)),
        (sgt(2026, 9, 29, 10), sgt(2026, 9, 29, 11)),
    ]


def test_free_slots_round_up_to_half_hour():
    slots = compute_free_slots([], sgt(2026, 9, 28, 9, 10), sgt(2026, 9, 28, 11), 30, WORK_START, WORK_END)
    assert slots[0][0] == sgt(2026, 9, 28, 9, 30)


def test_free_slots_capped():
    slots = compute_free_slots(
        [], sgt(2026, 9, 28, 9), sgt(2026, 9, 30, 21), 30, WORK_START, WORK_END, max_slots=4
    )
    assert len(slots) == 4


def test_free_slots_reject_bad_input():
    with pytest.raises(ValueError):
        compute_free_slots([], sgt(2026, 9, 28, 12), sgt(2026, 9, 28, 9), 30, WORK_START, WORK_END)
    with pytest.raises(ValueError):
        compute_free_slots([], sgt(2026, 9, 28, 9), sgt(2026, 9, 28, 12), 0, WORK_START, WORK_END)
