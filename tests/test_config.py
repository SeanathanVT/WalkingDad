import json

import pytest

import config
import storage


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    app_dir, data_dir = tmp_path, tmp_path / "data"
    monkeypatch.setattr(config, "APP_DIR", str(app_dir))
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "BACKUP_DIR", str(data_dir / "backups"))
    monkeypatch.setattr(config, "CONFIG_FILE", str(data_dir / "config.json"))
    monkeypatch.setattr(config, "SESSION_STATE_FILE", str(data_dir / "session_state.json"))
    monkeypatch.setattr(config, "DATABASE_PATH", "walkingdad.db")
    return app_dir, data_dir


def test_relocate_legacy_files(dirs):
    app_dir, data_dir = dirs
    for name in ("config.json", "session_state.json", "walkingdad.db", "walkingdad.db-wal",
                 "session_history.json.bak-20261005-111247", "session_history.json.migrated", "app.py"):
        (app_dir / name).write_text(name)

    config.relocate_legacy_files()
    config.relocate_legacy_files()  # Second run has nothing left to move.

    assert sorted(p.name for p in app_dir.iterdir()) == ["app.py", "data"]
    assert sorted(p.name for p in data_dir.iterdir()) == [
        "backups", "config.json", "session_state.json", "walkingdad.db", "walkingdad.db-wal"]
    assert sorted(p.name for p in (data_dir / "backups").iterdir()) == [
        "session_history.json.bak-20261005-111247", "session_history.json.migrated"]
    assert (data_dir / "walkingdad.db").read_text() == "walkingdad.db"


def test_relocate_never_overwrites(dirs):
    app_dir, data_dir = dirs
    data_dir.mkdir()
    (app_dir / "config.json").write_text("old")
    (data_dir / "config.json").write_text("new")

    config.relocate_legacy_files()

    assert (app_dir / "config.json").read_text() == "old"
    assert (data_dir / "config.json").read_text() == "new"


def test_relocate_keeps_database_with_its_wal(dirs):
    app_dir, data_dir = dirs
    data_dir.mkdir()
    (data_dir / "walkingdad.db-wal").write_text("stale")
    (app_dir / "walkingdad.db").write_text("db")
    (app_dir / "walkingdad.db-wal").write_text("wal")

    config.relocate_legacy_files()

    assert (app_dir / "walkingdad.db").exists() and (app_dir / "walkingdad.db-wal").exists()
    assert not (data_dir / "walkingdad.db").exists()
    assert (data_dir / "walkingdad.db-wal").read_text() == "stale"


def test_relocate_leaves_absolute_database_path(dirs, monkeypatch):
    app_dir, _ = dirs
    db = app_dir / "elsewhere.db"
    db.write_text("db")
    monkeypatch.setattr(config, "DATABASE_PATH", str(db))

    config.relocate_legacy_files()

    assert db.exists()


def test_load_overrides_falls_back_to_legacy(tmp_path):
    legacy = tmp_path / "config.json"
    legacy.write_text(json.dumps({"port": 6000}))
    assert config._load_overrides(str(tmp_path / "data" / "config.json"), str(legacy)) == {"port": 6000}
    assert config._load_overrides(str(tmp_path / "missing.json")) == {}


def test_prepare_data_refuses_newer_config(dirs, monkeypatch):
    monkeypatch.setattr(config, "_overrides", {"schema_version": 2})
    with pytest.raises(storage.DataVersionError,
                       match=r"settings format v2; this version understands up to v1\. .*delete .*config\.json"):
        config.prepare_data()
    assert not (dirs[1] / "walkingdad.db").exists()


def test_prepare_data_rejects_string_config_version(dirs, monkeypatch):
    monkeypatch.setattr(config, "_overrides", {"schema_version": "2"})
    with pytest.raises(storage.DataVersionError):
        config.prepare_data()


def test_prepare_data_refuses_newer_session_state(dirs, monkeypatch):
    monkeypatch.setattr(config, "_overrides", {})
    dirs[1].mkdir()
    (dirs[1] / "session_state.json").write_text('{"schema_version": 2, "session_active": true}')
    with pytest.raises(storage.DataVersionError,
                       match=r"\(an interrupted walk\) was written by format v2.*recorded samples"):
        config.prepare_data()
    assert not (dirs[1] / "walkingdad.db").exists()


def test_prepare_data_ignores_corrupt_session_state(dirs, monkeypatch):
    monkeypatch.setattr(config, "_overrides", {})
    dirs[1].mkdir()
    (dirs[1] / "session_state.json").write_text("{not json")
    config.prepare_data()
    assert (dirs[1] / "walkingdad.db").exists()


def test_prepare_data_unversioned_files_are_v1(dirs, monkeypatch):
    monkeypatch.setattr(config, "_overrides", {"port": 5001})
    dirs[1].mkdir()
    (dirs[1] / "session_state.json").write_text('{"session_active": true}')
    config.prepare_data()
    assert (dirs[1] / "walkingdad.db").exists()


def test_prepare_data_relocates_before_checking(dirs, monkeypatch):
    app_dir, _ = dirs
    monkeypatch.setattr(config, "_overrides", {})
    (app_dir / "session_state.json").write_text('{"schema_version": 2}')
    with pytest.raises(storage.DataVersionError):
        config.prepare_data()


def test_prepare_data_warns_unknown_keys(dirs, monkeypatch, caplog):
    monkeypatch.setattr(config, "_overrides", {"schema_version": 1, "port": 5001, "max_speed_kph": 9})
    config.prepare_data()
    assert "max_speed_kph" in caplog.text
    assert "schema_version" not in caplog.text and "port" not in caplog.text


@pytest.mark.parametrize("value", [0, -1])
def test_prepare_data_rejects_config_version_below_1(dirs, monkeypatch, value):
    monkeypatch.setattr(config, "_overrides", {"schema_version": value})
    with pytest.raises(storage.DataVersionError, match="unreadable schema_version"):
        config.prepare_data()


def test_prepare_data_rejects_session_state_version_below_1(dirs, monkeypatch):
    monkeypatch.setattr(config, "_overrides", {})
    dirs[1].mkdir()
    (dirs[1] / "session_state.json").write_text('{"schema_version": 0, "session_active": true}')
    with pytest.raises(storage.DataVersionError, match="unreadable schema_version"):
        config.prepare_data()


@pytest.mark.parametrize("value", [0, -1, "2", None])
def test_migrate_skips_unusable_versions(value):
    assert config.migrate({"schema_version": value}, {}, 1) == {"schema_version": value}
