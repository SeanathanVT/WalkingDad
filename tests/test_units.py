import json

import storage
from units import legacy_record

RECORDS = [
    {"date": "2026-07-08", "start_time": "11:37:02", "end_time": "11:42:31",
     "duration_seconds": 317, "distance_km": 0.38, "distance_mi": 0.236, "steps": 562,
     "calories": 22, "avg_speed_kmh": 4.3, "avg_speed_mph": 2.7},
    # Converting the rounded 4.9 km/h gives 3.0 mph; the original was 3.1 (from 4.911 km/h).
    {"date": "2026-07-15", "start_time": "12:07:04", "end_time": "12:34:26",
     "duration_seconds": 1356, "distance_km": 1.85, "distance_mi": 1.15, "steps": 2562,
     "calories": 109, "avg_speed_kmh": 4.9, "avg_speed_mph": 3.1},
    {"date": "2026-07-20", "start_time": "23:50:00", "end_time": "00:10:00",
     "duration_seconds": 1100, "distance_km": 1.5, "distance_mi": 0.932, "steps": 2000,
     "calories": 89, "avg_speed_kmh": 4.9, "avg_speed_mph": 3.1, "health_logged": True},
]


def test_migrated_records_display_exactly_as_before(tmp_path):
    storage.init_db(str(tmp_path / "test.db"))
    json_path = tmp_path / "session_history.json"
    json_path.write_text(json.dumps(RECORDS))
    storage.migrate_json(str(json_path))

    shown = [legacy_record(r) for r in storage.list_sessions()]

    expected = [{**r, "health_logged": r.get("health_logged", False)} for r in reversed(RECORDS)]
    for got, want in zip(shown, expected, strict=True):
        assert {k: got[k] for k in want} == want


def test_new_session_record_matches_old_builder():
    # Row as _save_session() writes it for 1.85 km over 1356 s of moving time.
    row = {
        "id": "x", "start_time": "2026-07-15T12:07:04-04:00", "end_time": "2026-07-15T12:34:26-04:00",
        "moving_s": 1356, "distance_m": 1850.0, "steps": 2562, "calories_kcal": 109.4,
        "health_logged": 0, "has_samples": 0,
    }

    rec = legacy_record(row)

    assert rec["date"] == "2026-07-15"
    assert rec["start_time"] == "12:07:04"
    assert rec["end_time"] == "12:34:26"
    assert rec["duration_seconds"] == 1356
    assert rec["distance_km"] == 1.85
    assert rec["distance_mi"] == 1.15
    assert rec["calories"] == 109
    assert rec["avg_speed_kmh"] == 4.9
    assert rec["avg_speed_mph"] == 3.1
    assert rec["health_logged"] is False


def test_zero_moving_time_does_not_divide_by_zero():
    row = {
        "id": "x", "start_time": "2026-07-15T12:00:00-04:00", "end_time": None,
        "moving_s": 0, "distance_m": 0.0, "steps": 0, "calories_kcal": 0.0,
        "health_logged": 0, "has_samples": 0,
    }

    rec = legacy_record(row)

    assert rec["avg_speed_mph"] == 0
    assert rec["end_time"] is None
