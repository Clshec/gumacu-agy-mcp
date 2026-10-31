"""Revision-triggered MCP reload never interrupts an in-flight backend operation."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from alf_control import reload_guard


class _Child:
    pid = 424242

    def poll(self):
        return None


def test_request_id_remains_pending_until_mcp_result():
    track = reload_guard.RequestTracker()
    request = b'{"jsonrpc":"2.0","method":"tools/call","id":42,"params":{}}'
    track.request(request)
    assert '"42"' not in track.pending
    assert "42" in track.pending
    track.response(b'{"jsonrpc":"2.0","method":"notifications/message"}')
    assert "42" in track.pending
    track.response(b'{"jsonrpc":"2.0","id":42,"result":{}}')
    assert not track.pending


def test_idle_reload_refuses_inflight_and_running_jobs(monkeypatch):
    track = reload_guard.RequestTracker()
    track.last_activity -= 60
    monkeypatch.setattr(reload_guard, "_child_has_descendants", lambda pid: False)
    monkeypatch.setenv("ALF_CONTROL_RELOAD_IDLE_SECONDS", "0.1")
    if sys.platform != "linux":
        assert not reload_guard.can_recycle(_Child(), track)
        return
    assert reload_guard.can_recycle(_Child(), track)
    track.request(b'{"jsonrpc":"2.0","method":"tools/call","id":1}')
    assert not reload_guard.can_recycle(_Child(), track)
    track.response(b'{"jsonrpc":"2.0","id":1,"result":{}}')
    track.last_activity -= 60
    monkeypatch.setattr(reload_guard, "_child_has_descendants", lambda pid: True)
    assert not reload_guard.can_recycle(_Child(), track)
    monkeypatch.setattr(reload_guard, "_child_has_descendants", lambda pid: False)


def test_revision_marker_missing_fails_closed(monkeypatch, tmp_path):
    marker = tmp_path / "git-revision"
    monkeypatch.setenv("ALF_CONTROL_RELOAD_REVISION_FILE", str(marker))
    assert reload_guard.source_revision() is None
    marker.write_text("previous", encoding="utf-8")
    assert reload_guard.source_revision() == "previous"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc and child idle refresh")
def test_revision_change_terminates_only_idle_disposable_child(monkeypatch, tmp_path):
    marker = tmp_path / "git-revision"
    marker.write_text("revision-1", encoding="utf-8")
    monkeypatch.setenv("ALF_CONTROL_RELOAD_REVISION_FILE", str(marker))
    monkeypatch.setenv("ALF_CONTROL_RELOAD_CHECK_SECONDS", "0.2")
    monkeypatch.setenv("ALF_CONTROL_RELOAD_IDLE_SECONDS", "0.1")
    monkeypatch.setenv("ALF_CONTROL_AUTO_REFRESH", "1")
    monkeypatch.setattr(reload_guard, "_child_has_descendants", lambda _: False)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    stopped = threading.Event()
    tracker = reload_guard.RequestTracker()
    tracker.last_activity -= 30
    watcher = threading.Thread(
        target=reload_guard.watch_revision,
        args=(child, tracker, stopped),
    )
    try:
        watcher.start()
        time.sleep(0.35)
        assert child.poll() is None
        marker.write_text("revision-2", encoding="utf-8")
        watcher.join(4)
        assert not watcher.is_alive()
        assert child.wait(timeout=2) is not None
    finally:
        stopped.set()
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=2)
        watcher.join(timeout=2)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc and child idle refresh")
def test_live_request_blocks_reload_until_result(monkeypatch, tmp_path):
    marker = tmp_path / "rev"
    marker.write_text("a", encoding="utf-8")
    monkeypatch.setenv("ALF_CONTROL_RELOAD_REVISION_FILE", str(marker))
    monkeypatch.setenv("ALF_CONTROL_RELOAD_CHECK_SECONDS", "0.2")
    monkeypatch.setenv("ALF_CONTROL_RELOAD_IDLE_SECONDS", "0.1")
    monkeypatch.setenv("ALF_CONTROL_AUTO_REFRESH", "1")
    monkeypatch.setattr(reload_guard, "_child_has_descendants", lambda _: False)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    stopped = threading.Event()
    tracker = reload_guard.RequestTracker()
    tracker.request(b'{"jsonrpc":"2.0","method":"tools/call","id":987}')
    watcher = threading.Thread(
        target=reload_guard.watch_revision,
        args=(child, tracker, stopped),
    )
    # Wait for the watcher to capture its original revision before modifying
    # the marker. Otherwise a scheduling race can make it start on "b".
    baseline = threading.Event()
    read_revision = reload_guard.source_revision

    def observed_revision():
        value = read_revision()
        baseline.set()
        return value

    monkeypatch.setattr(reload_guard, "source_revision", observed_revision)
    try:
        watcher.start()
        assert baseline.wait(2), "watcher failed to read the baseline revision"
        marker.write_text("b", encoding="utf-8")
        time.sleep(0.6)
        assert child.poll() is None
        tracker.response(b'{"jsonrpc":"2.0","id":987,"result":{}}')
        tracker.last_activity -= 30
        watcher.join(4)
        assert child.wait(timeout=2) is not None
    finally:
        stopped.set()
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=2)
        watcher.join(timeout=2)
