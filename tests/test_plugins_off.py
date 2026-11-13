"""G1: plugins.off switches a plugin fully off. It is not loaded (no schedules, triggers, Ari tools or buttons), jobs
for it are refused, and its folder is left alone."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from argus.ask import catalog
from argus.config import ConfigError, load_config
from argus.context import Argus
from argus.jobs import JobStore, PluginOff
from argus.plugins import PluginHost, discover
from argus.worker.client import ApiError
from conftest import run
from test_plugins import setup
from test_worker import Server, client

SCHEDULE = [{"manual": {"workflow": "sort", "label": "Sort now"}},
            {"schedule": {"workflow": "sort", "cron": "0 3 * * *"}}]


def switch_off(tmp_path: Path, off: str = "[tidy]", live: str = "[]") -> Path:
    p = tmp_path / "argus.yaml"
    text = p.read_text().replace("  live: []\n", f"  live: {live}\n") + f"  off: {off}\n"
    p.write_text(text)
    return p


def test_an_off_plugin_is_not_loaded(tmp_path, caplog):
    setup(tmp_path, triggers=SCHEDULE)
    cfg = load_config(switch_off(tmp_path))
    host = PluginHost(cfg)
    with caplog.at_level(logging.INFO, logger="argus.plugins"):
        host.load()
    assert host.plugins == {} and host.errors == {}
    assert host.for_worker(["desktop", "laptop", "gpu"]) == []
    assert not [s for s in cfg.schedules if s.plugin == "tidy"]  # its schedule is not wired
    assert not [a for a in catalog(host) if a["id"].startswith("run:tidy:")]  # no button
    assert host.list()["off"] == ["tidy"] and host.list()["plugins"] == []
    off_lines = [r for r in caplog.records if r.getMessage() == "plugins switched off"]
    assert len(off_lines) == 1 and off_lines[0].plugins == ["tidy"]
    assert (tmp_path / "plugins" / "tidy" / "plugin.py").exists()  # the code stays


def test_without_off_the_same_plugin_loads(tmp_path):
    argus, _, _ = setup(tmp_path, triggers=SCHEDULE)
    host = PluginHost(argus.cfg)
    host.load()
    assert list(host.plugins) == ["tidy"] and host.list()["off"] == []
    assert [s.plugin for s in argus.cfg.schedules if s.plugin == "tidy"] == ["tidy"]


def test_a_job_for_an_off_plugin_is_refused(tmp_path):
    setup(tmp_path)
    argus = Argus(load_config(switch_off(tmp_path)))
    with Server(argus.open()) as srv:
        cl = client(srv.url)
        with pytest.raises(ApiError) as e:
            cl.post("/jobs", {"plugin": "tidy", "workflow": "sort", "input": {}})
        assert e.value.status == 409 and e.value.code == "plugin_off"
        assert "switched off in plugins.off" in str(e.value)
        assert cl.get("/plugins")["off"] == ["tidy"]
        assert cl.get("/jobs?plugin=tidy") == []


def test_the_job_store_refuses_every_way_in(store):
    """Schedules, triggers, Ari's tools and buttons all queue through the job store, so one check covers them."""
    jobs = JobStore(store, off=["tidy"])
    with pytest.raises(PluginOff, match="tidy is switched off in plugins.off"):
        run(jobs.enqueue("tidy", "sort"))
    assert run(jobs.enqueue("other", "sort"))[1] is True


def test_live_and_off_together_is_a_config_error(tmp_path):
    setup(tmp_path)
    with pytest.raises(ConfigError) as e:
        load_config(switch_off(tmp_path, live="[tidy]"))
    assert "tidy listed in both live and off" in str(e.value)


def test_an_unknown_id_in_off_warns(tmp_path, caplog):
    setup(tmp_path)
    cfg = load_config(switch_off(tmp_path, off="[tidy, nope]"))
    assert cfg.plugins.off == ["tidy", "nope"]  # YAML reads a bare `off:` key as false; it still means plugins.off
    with caplog.at_level(logging.WARNING, logger="argus.plugins"):
        found, errors = discover(cfg)
    assert found == [] and errors == {}
    warned = [r for r in caplog.records if r.getMessage() == "plugins.off names no plugin folder"]
    assert [r.plugin for r in warned] == ["nope"]
