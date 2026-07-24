from datetime import datetime, timedelta

from mem0.utils.timestamps import (
    BEIJING_TIMEZONE,
    beijing_now,
    beijing_now_iso,
    normalize_iso_timestamp_to_beijing,
)


def test_beijing_now_helpers_return_timezone_aware_beijing_time():
    now = beijing_now()
    serialized = datetime.fromisoformat(beijing_now_iso())

    assert now.utcoffset() == timedelta(hours=8)
    assert serialized.utcoffset() == timedelta(hours=8)


def test_normalize_iso_timestamp_to_beijing_converts_aware_timestamps():
    assert normalize_iso_timestamp_to_beijing("2026-07-23T09:24:26.716739+00:00") == (
        "2026-07-23T17:24:26.716739+08:00"
    )
    assert normalize_iso_timestamp_to_beijing("2026-07-23T10:24:26+01:00") == "2026-07-23T17:24:26+08:00"
    assert normalize_iso_timestamp_to_beijing("2026-07-23T09:24:26Z") == "2026-07-23T17:24:26+08:00"


def test_normalize_iso_timestamp_to_beijing_treats_naive_values_as_local():
    assert normalize_iso_timestamp_to_beijing("2026-07-23T17:24:26") == "2026-07-23T17:24:26+08:00"
    assert normalize_iso_timestamp_to_beijing(datetime(2026, 7, 23, 17, 24, 26)) == (
        "2026-07-23T17:24:26+08:00"
    )
    assert normalize_iso_timestamp_to_beijing("not-a-timestamp") == "not-a-timestamp"
    assert normalize_iso_timestamp_to_beijing(None) is None
    assert BEIJING_TIMEZONE.utcoffset(None) == timedelta(hours=8)
