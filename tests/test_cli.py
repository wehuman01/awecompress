"""CLI surface: help, version, config, clear, status."""

import json

import pytest
from click.testing import CliRunner

from awecompress import __version__
from awecompress.cli import cli
from awecompress.store import SessionRecord, Store


@pytest.fixture(autouse=True)
def tmp_config_dir(tmp_path, monkeypatch):
    d = tmp_path / "awecompress"
    monkeypatch.setenv("AWECOMPRESS_CONFIG_DIR", str(d))
    return d


@pytest.fixture
def runner():
    return CliRunner()


def test_version(runner):
    out = runner.invoke(cli, ["--version"])
    assert out.exit_code == 0
    assert out.output.strip() == f"awecompress {__version__}"


def test_help_format(runner):
    out = runner.invoke(cli, ["--help"])
    assert out.exit_code == 0
    assert "Usage: awecompress [OPTIONS] COMMAND [ARGS]..." in out.output
    for cmd in ("serve", "status", "config", "clear"):
        assert cmd in out.output


def test_serve_help(runner):
    out = runner.invoke(cli, ["serve", "--help"])
    assert out.exit_code == 0
    assert "--upstream" in out.output
    assert "--port" in out.output


def test_config_path(runner, tmp_config_dir):
    out = runner.invoke(cli, ["config", "path"])
    assert out.exit_code == 0
    assert str(tmp_config_dir / "config.json") in out.output


def test_config_show_writes_nothing_when_missing(runner, tmp_config_dir):
    out = runner.invoke(cli, ["config", "show"])
    assert out.exit_code == 0
    assert "no config file" in out.output


def test_config_show_after_first_load(runner, tmp_config_dir):
    runner.invoke(cli, ["status"])  # triggers default-config creation
    out = runner.invoke(cli, ["config", "show"])
    assert "thresholdTokens" in out.output


def test_status_not_running(runner, tmp_config_dir):
    # Port 1 is never listenable — the probe must fail fast, not hang.
    tmp_config_dir.mkdir(parents=True, exist_ok=True)
    (tmp_config_dir / "config.json").write_text('{"port": 1}\n')
    out = runner.invoke(cli, ["status"])
    assert out.exit_code == 0
    assert "not running" in out.output
    assert "upstream" in out.output


def test_clear_empties_store(runner, tmp_config_dir):
    runner.invoke(cli, ["status"])  # creates the db via Store()
    db = tmp_config_dir / "summaries.db"
    assert db.exists()
    store = Store(db)
    store.put(SessionRecord("k1", 8, "h", "frozen summary"))
    store.close()
    out = runner.invoke(cli, ["clear", "--yes"])
    assert out.exit_code == 0
    assert "1 session" in out.output
    store = Store(db)
    assert store.stats()["sessions"] == 0
    store.close()


def test_clear_honors_configured_db_path(runner, tmp_config_dir):
    custom = tmp_config_dir / "elsewhere.db"
    store = Store(custom)
    store.put(SessionRecord("k1", 8, "h", "frozen summary"))
    store.close()
    tmp_config_dir.mkdir(parents=True, exist_ok=True)
    (tmp_config_dir / "config.json").write_text(json.dumps({"dbPath": str(custom)}))
    out = runner.invoke(cli, ["clear", "--yes"])
    assert out.exit_code == 0
    store = Store(custom)
    assert store.stats()["sessions"] == 0
    store.close()
    assert not (tmp_config_dir / "summaries.db").exists()  # default path untouched


def test_clear_nothing_to_do(runner, tmp_config_dir):
    out = runner.invoke(cli, ["clear", "--yes"])
    assert out.exit_code == 0
    assert "nothing to clear" in out.output
