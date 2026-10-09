"""A job's log lines keep what they were given (the error of a failed job)."""

import logging

from argus.worker.runner import JobLog


def test_the_job_log_keeps_the_extras_of_each_line(caplog):
    jlog = JobLog(logging.getLogger("argus.worker"), {"job": "J1", "plugin": "p", "workflow": "w"})
    with caplog.at_level(logging.ERROR, logger="argus.worker"):
        jlog.error("job failed", extra={"error": "ValueError: boom"})
    rec = caplog.records[-1]
    assert rec.error == "ValueError: boom" and rec.job == "J1" and rec.workflow == "w"
