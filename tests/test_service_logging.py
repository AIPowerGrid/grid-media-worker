"""Service-log volume: an always-on rig must log state changes and faults,
not the same few lines every few seconds for as long as it runs."""
import asyncio
import logging

import pytest

from bridge import cli
from bridge import ws_worker
from bridge.web import app as web_app


def _record(msg, level=logging.INFO, name="bridge.test", args=None):
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_repeat_throttle_suppresses_repeats_and_reports_the_count():
    clock = FakeClock()
    throttle = cli._RepeatThrottle(window_s=600, clock=clock)

    assert throttle.filter(_record("Waiting for ComfyUI"))
    for _ in range(5):
        clock.now += 3
        assert not throttle.filter(_record("Waiting for ComfyUI"))
    # A different message is never held back by another one's window.
    assert throttle.filter(_record("Advertising 'x' — ok"))

    clock.now += 600
    summary = _record("Waiting for ComfyUI")
    assert throttle.filter(summary)
    assert summary.getMessage() == (
        "Waiting for ComfyUI (repeated 5 more times in the last 10 min)"
    )


def test_repeat_throttle_never_hides_errors():
    throttle = cli._RepeatThrottle(window_s=600, clock=FakeClock())
    for _ in range(3):
        assert throttle.filter(_record("render failed", logging.ERROR))


def test_status_polls_are_dropped_from_the_access_log_only():
    quiet = cli._QuietStatusPolls()
    access = '%s - "%s %s HTTP/%s" %d'
    poll = _record(access, name="uvicorn.access",
                   args=("127.0.0.1:5000", "GET", "/api/status", "1.1", 200))
    save = _record(access, name="uvicorn.access",
                   args=("127.0.0.1:5000", "POST", "/api/settings", "1.1", 200))
    assert not quiet.filter(poll)
    assert quiet.filter(save)

    config = cli._uvicorn_log_config()
    assert config["handlers"]["access"]["filters"] == ["quiet_status_polls"]
    # uvicorn's shared default must not be mutated by our copy.
    from uvicorn.config import LOGGING_CONFIG
    assert "filters" not in LOGGING_CONFIG["handlers"]["access"]


def test_httpx_request_lines_are_quieted(monkeypatch):
    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    root = logging.getLogger()
    before = list(root.handlers)
    handler = logging.NullHandler()
    root.addHandler(handler)
    try:
        cli._quiet_chatty_loggers()
        assert logging.getLogger("httpx").level == logging.WARNING
        assert any(isinstance(f, cli._RepeatThrottle) for f in handler.filters)
    finally:
        root.removeHandler(handler)
        for h in before:
            h.filters = [f for f in h.filters
                         if not isinstance(f, cli._RepeatThrottle)]


def test_model_mapper_reports_through_logging_not_print(caplog, capsys):
    """print() bypassed log levels and the repeat throttle."""
    from bridge import model_mapper

    with caplog.at_level(logging.INFO, logger="bridge.model_mapper"):
        model_mapper.logger.warning("probe")
    assert any(r.name == "bridge.model_mapper" for r in caplog.records)
    source = open(model_mapper.__file__, encoding="utf-8").read()
    assert "print(" not in source
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_waiting_for_comfyui_is_logged_as_warning_not_error(
    monkeypatch, caplog
):
    retried = asyncio.Event()
    calls = []

    class FakeComfy:
        async def aclose(self):
            pass

    class FakeWorker:
        def __init__(self):
            self.comfy = FakeComfy()
            calls.append(self)

        async def run(self):
            if len(calls) == 1:
                raise ws_worker.StartupPending("Waiting for ComfyUI at x")
            retried.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(ws_worker, "WSWorker", FakeWorker)
    monkeypatch.setattr(web_app, "WORKER_START_RETRY_SECONDS", 0)
    with caplog.at_level(logging.INFO, logger="bridge.web.app"):
        task = asyncio.create_task(web_app._run_worker())
        await asyncio.wait_for(retried.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    waiting = [r for r in caplog.records if "Waiting for ComfyUI" in r.getMessage()]
    assert waiting and all(r.levelno == logging.WARNING for r in waiting)
