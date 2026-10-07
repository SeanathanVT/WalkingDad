import json

import pytest

import config


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    app_dir, data_dir = tmp_path, tmp_path / "data"
    monkeypatch.setattr(config, "APP_DIR", str(app_dir))
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "BACKUP_DIR", str(data_dir / "backups"))
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
