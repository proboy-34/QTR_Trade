"""Launcher helpers (RUN_QTR.bat / STOP_QTR.bat) and scheduled-job serialization."""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.events import EventBus
from app.jobs import JobContext, run_job

SPEC = importlib.util.spec_from_file_location("qtr_launcher", Path(__file__).resolve().parents[1] / "scripts" / "qtr_launcher.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def test_only_qtr_health_payloads_are_recognised():
    assert launcher.is_qtr_health({"status": "HEALTHY", "components": {}, "execution_mode": "paper", "data_mode": "REAL"})
    assert not launcher.is_qtr_health({"status": "ok"})  # an unrelated server on the same port
    assert not launcher.is_qtr_health(None)


def test_stop_without_launcher_state_touches_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "STATE_FILE", tmp_path / "launcher.json")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "BACKEND_URL", "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.setattr(launcher, "FRONTEND_PORT", 9)
    assert launcher.stop() == 0
    output = capsys.readouterr().out
    assert "nothing was running from RUN_QTR.bat" in output and "QTR_Trade stopped." in output


def test_process_check_rejects_unknown_pids():
    assert not launcher.process_alive(None, launcher.BACKEND_TITLE)
    assert not launcher.process_alive(2_147_000_000, launcher.BACKEND_TITLE)


@pytest.mark.asyncio
async def test_scheduled_jobs_never_interleave(session):
    """SQLite waits for locks synchronously; interleaved jobs on one event loop could stall each other."""
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    context = JobContext(factory, Settings(), EventBus())
    timeline: list[str] = []

    def job(name: str):
        async def run(_session):
            timeline.append(f"{name}:start")
            await asyncio.sleep(0.05)  # e.g. a network await inside a job
            timeline.append(f"{name}:end")
            return {}
        return run

    await asyncio.gather(run_job(context, "first", job("first")), run_job(context, "second", job("second")))
    assert timeline in (["first:start", "first:end", "second:start", "second:end"],
                        ["second:start", "second:end", "first:start", "first:end"])


def test_only_one_launcher_may_start_at_a_time(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(launcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(launcher, "START_LOCK", tmp_path / "starting.lock")
    assert launcher.acquire_start_lock()          # first launcher
    assert not launcher.acquire_start_lock()      # a second one while the first is running (its PID is alive)
    (tmp_path / "starting.lock").write_text("999999999")  # left behind by a launcher window that was closed
    assert launcher.acquire_start_lock()          # recovered automatically
    assert (tmp_path / "starting.lock").read_text() == str(os.getpid())
