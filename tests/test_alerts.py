"""Tests for ntfy alerting and the capture watchdog."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import text

from skyjam.common import alerts


@pytest.fixture
def ntfy(monkeypatch):
    """A local stand-in for ntfy.sh that records every request."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            n = int(self.headers.get("Content-Length", 0))
            received.append(
                {
                    "title": self.headers.get("Title"),
                    "priority": self.headers.get("Priority"),
                    "auth": self.headers.get("Authorization"),
                    "body": self.rfile.read(n).decode(),
                }
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("SKYJAM_NTFY_URL", f"http://127.0.0.1:{server.server_port}/topic")
    yield received
    server.shutdown()


def test_send_posts_title_priority_and_body(ntfy, monkeypatch):
    monkeypatch.setenv("SKYJAM_NTFY_TOKEN", "tok")
    assert alerts.send("skyjam test", "hello", priority="urgent")
    expected = {"title": "skyjam test", "priority": "urgent", "auth": "Bearer tok", "body": "hello"}
    assert ntfy == [expected]


def test_send_is_a_noop_without_a_topic(monkeypatch):
    monkeypatch.delenv("SKYJAM_NTFY_URL", raising=False)
    assert alerts.send("t", "b") is False


def test_send_never_raises_when_ntfy_is_unreachable(monkeypatch):
    monkeypatch.setenv("SKYJAM_NTFY_URL", "http://127.0.0.1:9/unreachable")
    assert alerts.send("t", "b") is False


@pytest.fixture
def engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    from skyjam.common.db import get_engine
    from test_db import DDL  # same schema as the store tests (tests/ is on rootdir path)

    eng = get_engine(url)
    with eng.begin() as conn:
        conn.execute(text(DDL))
        conn.execute(text("TRUNCATE observations"))
    return eng


def _insert_slot(engine, slot: datetime):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO observations (observed_at, slot_start, hex, lat, lon, "
                "altitude_ft, nic, sample_point, is_control_region, ingest_run_id) "
                "VALUES (:t, :t, 'abc', 1, 1, 35000, 8, 'p', false, 'r')"
            ),
            {"t": slot},
        )


def test_watchdog_is_quiet_when_capture_is_fresh(engine):
    now = datetime(2026, 11, 9, 14, 20, tzinfo=UTC)
    _insert_slot(engine, datetime(2026, 11, 9, 14, tzinfo=UTC))
    assert alerts.check_capture(engine, now) is None


def test_watchdog_flags_a_dead_ingestor(engine):
    now = datetime(2026, 11, 9, 14, 20, tzinfo=UTC)
    # Newest slot 14:00 minus 5 h = 09:00, seen at 14:20: 5.3 h old.
    _insert_slot(engine, datetime(2026, 11, 9, 9, tzinfo=UTC))
    problem = alerts.check_capture(engine, now)
    assert problem is not None and "5.3 h old" in problem


def test_watchdog_flags_an_empty_database(engine):
    assert "no observations" in alerts.check_capture(engine)


def test_failed_build_sends_an_alert(ntfy, engine, monkeypatch):
    """End to end through the CLI: empty database -> exit 1 -> two alerts."""
    from skyjam.features import compute

    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setattr("sys.argv", ["skyjam-features"])
    with pytest.raises(SystemExit) as exc:
        compute.main()
    assert exc.value.code == 1
    titles = [m["title"] for m in ntfy]
    assert titles == ["skyjam capture STALE", "skyjam features FAILED"]
