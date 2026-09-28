from __future__ import annotations

from pathlib import Path

import pytest

from argus.config import ConfigError, load_config
from argus.daemon import main

EXAMPLE = Path(__file__).resolve().parents[1] / "argus.example.yaml"


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "argus.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_example_config_is_valid(tmp_path):
    p = write(tmp_path, EXAMPLE.read_text(encoding="utf-8"))
    cfg = load_config(p)
    assert cfg.server.port == 8600
    assert cfg.jobs.backoff_seconds == [10, 60, 600]
    assert cfg.db_path == (tmp_path / "data" / "argus.db").resolve()


def test_empty_file_uses_defaults(tmp_path):
    cfg = load_config(write(tmp_path, ""))
    assert cfg.power.mode == "simulated"


def test_missing_file_explains_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="Copy argus.example.yaml"):
        load_config(tmp_path / "nope.yaml")


def test_typo_in_setting_is_named(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, "server:\n  prot: 8600\n"))
    assert "server.prot" in str(e.value)
    assert "unknown setting" in str(e.value)


def test_bad_value_is_named(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, "server:\n  port: 99999\n"))
    assert "server.port" in str(e.value)


def test_bad_yaml(tmp_path):
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(write(tmp_path, "server: [unclosed\n"))


def test_secrets_from_env_file_never_in_dump(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGUS_ADMIN_PASSWORD", raising=False)
    (tmp_path / ".env").write_text("# secrets\nARGUS_ADMIN_PASSWORD='s3cret'\n", encoding="utf-8")
    cfg = load_config(write(tmp_path, ""))
    assert cfg.secrets.admin_password == "s3cret"
    assert "s3cret" not in cfg.model_dump_json()


def test_daemon_exits_2_on_bad_config(tmp_path, capsys):
    code = main(["--config", str(write(tmp_path, "nonsense: true\n"))])
    assert code == 2
    assert "nonsense" in capsys.readouterr().err


def test_daemon_check_ok(tmp_path, capsys):
    code = main(["--config", str(write(tmp_path, "logging:\n  file: null\n")), "--check"])
    assert code == 0
    assert "OK" in capsys.readouterr().out
    assert (tmp_path / "data" / "argus.db").exists()
