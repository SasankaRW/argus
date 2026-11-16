"""`argus send-agent` under pythonw (the Windows logon task): no console, so it logs to a file instead of crashing."""

from __future__ import annotations

import logging
import sys

from argus.send import cli


def test_send_agent_without_a_console_logs_to_a_file(tmp_path, monkeypatch):
    cfg = tmp_path / "argus.yaml"
    data = tmp_path / "send-data"
    cfg.write_text(
        "send:\n"
        "  enabled: true\n"
        "  device_id: pc\n"
        "  name: PC\n"
        f"  data_dir: {data.as_posix()}\n"
        f"  root: {(tmp_path / 'shared').as_posix()}\n"
        "  mdns: false\n",
        encoding="utf-8",
    )

    async def fake_run_forever(self):
        logging.getLogger("argus.send").info("agent started without a console")

    monkeypatch.setattr(cli.SendAgent, "run_forever", fake_run_forever)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()  # basicConfig only acts when the root logger has no handlers
    try:
        assert cli.main(["--config", str(cfg), "send-agent"]) == 0
    finally:
        for h in root.handlers:
            h.flush()
        root.handlers[:] = saved
    assert "agent started without a console" in (data / "agent.log").read_text(encoding="utf-8")
