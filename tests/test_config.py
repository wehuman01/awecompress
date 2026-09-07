"""Config loading: defaults, overrides, validation."""

import json

import pytest

from dataclasses import replace

from awecompress import config as cfgmod
from awecompress.config import Config, load_config


@pytest.fixture(autouse=True)
def tmp_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AWECOMPRESS_CONFIG_DIR", str(tmp_path / "awecompress"))
    return tmp_path / "awecompress"


def test_first_run_writes_defaults(tmp_config_dir):
    cfg = load_config()
    assert cfg == replace(Config(), db_path=str(tmp_config_dir / "summaries.db"))
    path = tmp_config_dir / "config.json"
    assert path.exists()
    written = json.loads(path.read_text())
    assert written["upstream"] == cfgmod.DEFAULT_UPSTREAM
    assert written["thresholdTokens"] == cfgmod.DEFAULT_THRESHOLD_TOKENS


def test_overrides_honored(tmp_config_dir):
    path = tmp_config_dir / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "port": 9100, "upstream": "http://10.0.0.5:9000",
        "thresholdTokens": 30000, "keepRecentTurns": 6,
        "summaryModel": "cheap-model",
    }))
    cfg = load_config()
    assert cfg.port == 9100
    assert cfg.upstream == "http://10.0.0.5:9000"
    assert cfg.threshold_tokens == 30000
    assert cfg.keep_recent_turns == 6
    assert cfg.summary_model == "cheap-model"


def test_db_path_defaults_beside_config(tmp_config_dir):
    cfg = load_config()
    assert cfg.db_path == str(tmp_config_dir / "summaries.db")


def test_unknown_key_dies(tmp_config_dir):
    path = tmp_config_dir / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"threshold": 100}))  # typo'd key
    with pytest.raises(SystemExit, match="unknown key 'threshold'"):
        load_config()


def test_invalid_json_dies(tmp_config_dir):
    path = tmp_config_dir / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text("{nope")
    with pytest.raises(SystemExit, match="invalid JSON"):
        load_config()


def test_bad_values_die(tmp_config_dir):
    path = tmp_config_dir / "config.json"
    path.parent.mkdir(parents=True)
    cases = [
        {"port": 70000},
        {"port": "8808"},                       # string, not int
        {"upstream": "ftp://x"},
        {"thresholdTokens": 10},
        {"keepRecentTurns": 0},
        {"minSpanTokens": 5},
        {"summaryMaxTokens": 10},
        {"summaryTimeoutSeconds": 1},
    ]
    for case in cases:
        path.write_text(json.dumps(case))
        with pytest.raises(SystemExit):
            load_config()
