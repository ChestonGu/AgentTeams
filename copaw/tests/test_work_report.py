"""Tests for the direct-collaboration work_report tool (copaw line).

Covers action/status validation, event envelope construction, and the shared
emit_event delivery path (log + webhook resolution), with the filesync
config pull and webhook POST mocked out.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import copaw_worker.hooks.tools.work_report as work_report_mod
from copaw_worker.hooks.tools.work_report import work_report


def _response_json(response):
    block = response.content[0]
    text = block.get("text") if isinstance(block, dict) else block.text
    return json.loads(text)


@pytest.fixture
def emit_mock():
    with patch.object(work_report_mod, "pull_shared_config", MagicMock()) as pull, \
            patch.object(work_report_mod, "emit_event", new=AsyncMock()) as mock:
        mock.pull = pull
        yield mock


@pytest.mark.asyncio
async def test_started_event_envelope(emit_mock):
    response = await work_report(
        action="started", summary="Collecting inputs",
        requester="@alice:example.org")
    assert _response_json(response)["ok"] is True
    emit_mock.assert_awaited_once()
    event = emit_mock.await_args.args[0]
    assert event["event"] == "direct_work_started"
    assert event["work"]["summary"] == "Collecting inputs"
    assert event["work"]["requester"] == "@alice:example.org"
    assert "deliverables" not in event["work"]
    assert "status" not in event["work"]
    assert "timestamp" in event


@pytest.mark.asyncio
async def test_completed_event_with_deliverables(emit_mock):
    response = await work_report(
        action="completed", summary="Report ready",
        deliverables=["shared/outputs/dev/report.md"],
        requester="@alice:example.org")
    assert _response_json(response)["ok"] is True
    event = emit_mock.await_args.args[0]
    assert event["event"] == "direct_work_completed"
    assert event["work"]["status"] == "SUCCESS"
    assert event["work"]["deliverables"] == ["shared/outputs/dev/report.md"]


@pytest.mark.asyncio
async def test_completed_blocked_status(emit_mock):
    response = await work_report(
        action="completed", summary="Missing API", status="BLOCKED")
    assert _response_json(response)["ok"] is True
    event = emit_mock.await_args.args[0]
    assert event["work"]["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_invalid_action_rejected(emit_mock):
    response = await work_report(action="paused", summary="x")
    payload = _response_json(response)
    assert payload["ok"] is False
    emit_mock.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_status_rejected(emit_mock):
    response = await work_report(action="completed", summary="x", status="FAILED")
    assert _response_json(response)["ok"] is False
    emit_mock.assert_not_called()


@pytest.mark.asyncio
async def test_empty_summary_rejected(emit_mock):
    response = await work_report(action="started", summary="   ")
    assert _response_json(response)["ok"] is False
    emit_mock.assert_not_called()


@pytest.mark.asyncio
async def test_config_pull_called_before_emit(emit_mock):
    await work_report(action="started", summary="Go")
    emit_mock.pull.assert_called()


def test_collaboration_mode_default_empty(tmp_path, monkeypatch):
    from copaw_worker import hooks

    monkeypatch.setenv("COPAW_WORKING_DIR", str(tmp_path / "wd"))
    monkeypatch.setenv("QWENPAW_WORKING_DIR", str(tmp_path / "wd"))
    assert hooks._collaboration_mode() == ""


def test_collaboration_mode_direct(tmp_path, monkeypatch):
    from copaw_worker import hooks

    monkeypatch.setenv("COPAW_WORKING_DIR", str(tmp_path / "wd"))
    monkeypatch.setenv("QWENPAW_WORKING_DIR", str(tmp_path / "wd"))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "runtime.yaml").write_text(
        "team:\n  name: t-1\n  collaboration: direct\n", encoding="utf-8")
    assert hooks._collaboration_mode() == "direct"


def test_collaboration_mode_taskflow_explicit(tmp_path, monkeypatch):
    from copaw_worker import hooks

    monkeypatch.setenv("COPAW_WORKING_DIR", str(tmp_path / "wd"))
    monkeypatch.setenv("QWENPAW_WORKING_DIR", str(tmp_path / "wd"))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "runtime.yaml").write_text(
        "team:\n  name: t-1\n  collaboration: taskflow\n", encoding="utf-8")
    assert hooks._collaboration_mode() == "taskflow"


@pytest.mark.asyncio
async def test_emit_event_delivery_env_webhook():
    """emit_event posts to the env webhook with the bearer token (log+POST)."""
    import copaw_worker.hooks.tools.status_report as sr

    posted = []

    def fake_post(url, event, token=""):
        posted.append((url, event, token))

    with patch.object(sr, "_post_webhook", side_effect=fake_post), \
            patch.object(sr, "_webhook_file_config", return_value={}), \
            patch.dict(sr.os.environ, {
                sr._WEBHOOK_URL_ENV: "http://sink.local/status",
                sr._WEBHOOK_TOKEN_ENV: "secret-token"}):
        await sr.emit_event({"event": "direct_work_started", "work": {"summary": "Go"}})
    assert len(posted) == 1
    url, event, token = posted[0]
    assert url == "http://sink.local/status"
    assert event["event"] == "direct_work_started"
    assert token == "secret-token"


@pytest.mark.asyncio
async def test_emit_event_file_channel_when_env_absent():
    import copaw_worker.hooks.tools.status_report as sr

    posted = []

    def fake_post(url, event, token=""):
        posted.append((url, event, token))

    with patch.object(sr, "_post_webhook", side_effect=fake_post), \
            patch.object(sr, "_webhook_file_config",
                         return_value={sr._WEBHOOK_URL_ENV: "http://file.local/status"}), \
            patch.dict(sr.os.environ, {}, clear=False):
        sr.os.environ.pop(sr._WEBHOOK_URL_ENV, None)
        await sr.emit_event({"event": "direct_work_completed"})
    assert posted[0][0] == "http://file.local/status"


@pytest.mark.asyncio
async def test_emit_event_log_only_without_target():
    import copaw_worker.hooks.tools.status_report as sr

    with patch.object(sr, "_post_webhook") as post, \
            patch.object(sr, "_webhook_file_config", return_value={}), \
            patch.dict(sr.os.environ, {}, clear=False):
        sr.os.environ.pop(sr._WEBHOOK_URL_ENV, None)
        await sr.emit_event({"event": "direct_work_started"})
    post.assert_not_called()
