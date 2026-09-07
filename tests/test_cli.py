"""CLI surface: help, version, config, clear, status."""

import pytest
from click.testing import CliRunner

from awecompress import __version__
from awecompress.cli import cli


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


def test_clear_removes_store(runner, tmp_config_dir):
    runner.invoke(cli, ["status"])  # creates the db via Store()
    db = tmp_config_dir / "summaries.db"
    assert db.exists()
    out = runner.invoke(cli, ["clear", "--yes"])
    assert out.exit_code == 0
    assert not db.exists()


def test_clear_nothing_to_do(runner, tmp_config_dir):
    out = runner.invoke(cli, ["clear", "--yes"])
    assert out.exit_code == 0
    assert "nothing to clear" in out.output
