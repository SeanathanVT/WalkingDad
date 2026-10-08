import json
import logging

import pytest

import storage

NORMAL = {
    "date": "2026-07-08", "start_time": "11:37:02", "end_time": "11:42:31",
    "duration_seconds": 317, "distance_km": 0.38, "distance_mi": 0.236, "steps": 562,
    "calories": 22, "avg_speed_kmh": 4.3, "avg_speed_mph": 2.7,
}
MIDNIGHT = {
    "date": "2026-07-20", "start_time": "23:50:00", "end_time": "00:10:00",
    "duration_seconds": 1100, "distance_km": 1.5, "distance_mi": 0.932, "steps": 2000,
    "calories": 89, "avg_speed_kmh": 4.9, "avg_speed_mph": 3.1, "health_logged": True,
}
MALFORMED = {"date": "not-a-date", "start_time": "11:00:00"}


@pytest.fixture
def db(tmp_path):
    storage.init_db(str(tmp_path / "test.db"))
    return tmp_path


def _write(path, content):
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return str(path)


def test_migrates_records_and_skips_malformed(db, caplog):
    json_path = _write(db / "session_history.json", [NORMAL, MIDNIGHT, MALFORMED])

    with caplog.at_level(logging.INFO):
        storage.migrate_json(json_path)

    sessions = storage.list_sessions()
    assert len(sessions) == 2
    assert "skipped 1" in caplog.text
    for s in sessions:
        assert s["source"] == "migrated_json"
        assert s["tz_assumed"] == 1
        assert s["has_samples"] == 0
        assert s["status"] == "completed"


def test_field_mapping(db):
    storage.migrate_json(_write(db / "session_history.json", [NORMAL]))

    s = storage.list_sessions()[0]
    assert s["start_time"].startswith("2026-07-08T11:37:02")
    assert s["end_time"].startswith("2026-07-08T11:42:31")
    assert s["moving_s"] == 317
    assert s["elapsed_s"] == 329
    assert s["distance_m"] == pytest.approx(380)
    assert s["steps"] == 562
    assert s["calories_kcal"] == 22
    assert s["avg_speed_mps"] == pytest.approx(4.3 / 3.6)
    assert s["health_logged"] == 0
    assert s["max_speed_mps"] is None


def test_midnight_crossing_ends_next_day(db):
    storage.migrate_json(_write(db / "session_history.json", [MIDNIGHT]))

    s = storage.list_sessions()[0]
    assert s["end_time"].startswith("2026-07-21T00:10:00")
    assert s["elapsed_s"] == 1200
    assert s["health_logged"] == 1


def test_backup_and_rename(db):
    json_path = _write(db / "session_history.json", [NORMAL])

    storage.migrate_json(json_path)

    assert not (db / "session_history.json").exists()
    assert (db / "session_history.json.migrated").exists()
    backups = list(db.glob("session_history.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == [NORMAL]


def test_backup_dir(db, tmp_path):
    json_path = _write(db / "session_history.json", [NORMAL])
    backups = tmp_path / "backups"
    backups.mkdir()

    storage.migrate_json(json_path, str(backups))

    assert not (db / "session_history.json").exists()
    assert (backups / "session_history.json.migrated").exists()
    assert len(list(backups.glob("session_history.json.bak-*"))) == 1
    assert list(db.glob("session_history.json.*")) == []


def test_second_run_is_noop(db):
    storage.migrate_json(_write(db / "session_history.json", [NORMAL]))
    # A new legacy file appearing later must not be imported again.
    json_path = _write(db / "session_history.json", [MIDNIGHT])

    storage.migrate_json(json_path)

    assert len(storage.list_sessions()) == 1
    assert (db / "session_history.json").exists()


def test_empty_file_marks_migrated(db):
    json_path = _write(db / "session_history.json", "")

    storage.migrate_json(json_path)

    assert storage.list_sessions() == []
    assert (db / "session_history.json.migrated").exists()


def test_corrupt_file_left_untouched(db):
    json_path = _write(db / "session_history.json", "{not json")

    storage.migrate_json(json_path)

    assert (db / "session_history.json").read_text() == "{not json"
    assert not list(db.glob("session_history.json.*"))
    # Not marked migrated, so a fixed file still migrates later.
    _write(db / "session_history.json", [NORMAL])
    storage.migrate_json(json_path)
    assert len(storage.list_sessions()) == 1


def test_missing_file_is_noop(db):
    storage.migrate_json(str(db / "nope.json"))
    assert storage.list_sessions() == []


def test_non_list_top_level_left_untouched(db, caplog):
    json_path = _write(db / "session_history.json", {"sessions": [NORMAL]})

    storage.migrate_json(json_path)

    assert "top level is not a list" in caplog.text
    assert json.loads((db / "session_history.json").read_text()) == {"sessions": [NORMAL]}
    assert not list(db.glob("session_history.json.*"))
    assert storage.list_sessions() == []


@pytest.mark.parametrize("end_time", [None, "25:99:00"])
def test_missing_or_bad_end_time_falls_back_to_moving_time(db, end_time):
    record = {k: v for k, v in NORMAL.items() if k != "end_time"}
    if end_time is not None:
        record["end_time"] = end_time

    storage.migrate_json(_write(db / "session_history.json", [record]))

    s = storage.list_sessions()[0]
    assert s["end_time"] is None
    assert s["elapsed_s"] == s["moving_s"] == 317


def test_rename_failure_still_marks_migrated(db, monkeypatch, caplog):
    json_path = _write(db / "session_history.json", [NORMAL])

    def refuse(src, dst):
        raise OSError("read-only")

    monkeypatch.setattr(storage.os, "replace", refuse)
    storage.migrate_json(json_path)
    monkeypatch.undo()

    assert "could not rename it: read-only" in caplog.text
    assert (db / "session_history.json").exists()
    assert len(storage.list_sessions()) == 1
    storage.migrate_json(json_path)
    assert len(storage.list_sessions()) == 1
