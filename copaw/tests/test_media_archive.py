"""Tests for room-media archiving to team shared/knowledge/matrix/.

Pins down the contract of ``MatrixChannel._archive_media_to_shared`` and its
hook points:

- every group-room media event (mentioned or not) is mc-cp'd to the team
  shared tree and announced via m.notice;
- ``AGENTTEAMS_MEDIA_ARCHIVE=0`` opts out;
- a missing MinIO environment disables archiving instead of failing;
- archive failures never break the message flow;
- in ``_record_media_history`` the download is decoupled from the vision
  gate: images archive even when the model cannot see them, while the turn
  still gets no image part.
"""

import asyncio
import subprocess
from types import SimpleNamespace

import matrix.channel as matrix_channel
from matrix.channel import MatrixChannel

import copaw_worker.matrix_channel as worker_matrix_channel
from copaw_worker.matrix_channel import MatrixChannel as WorkerMatrixChannel

from copaw_worker.sync import FileSync

TEAM_SHARED_REMOTE = "agentteams/agentteams-storage/teams/t-1/shared/"


class _FakeClient:
    def __init__(self):
        self.sent = []

    async def room_send(self, room_id, message_type, content, **kwargs):
        self.sent.append((room_id, message_type, content, kwargs))
        return SimpleNamespace(event_id=f"$sent{len(self.sent)}")


def _make_channel() -> MatrixChannel:
    ch = MatrixChannel.__new__(MatrixChannel)
    ch._user_id = "@t-1-lead:hs.local"
    ch._client = _FakeClient()
    ch._media_filesync = None
    return ch


def _set_archive_env(monkeypatch):
    monkeypatch.setenv("AGENTTEAMS_WORKER_NAME", "t-1-lead")
    monkeypatch.setenv("AGENTTEAMS_FS_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("AGENTTEAMS_FS_ACCESS_KEY", "minio")
    monkeypatch.setenv("AGENTTEAMS_FS_SECRET_KEY", "password")
    monkeypatch.setenv("AGENTTEAMS_FS_BUCKET", "agentteams-storage")
    monkeypatch.delenv("AGENTTEAMS_MEDIA_ARCHIVE", raising=False)


def _mock_mc(monkeypatch):
    calls = []

    def fake_mc(*args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("copaw_worker.sync._mc", fake_mc)
    return calls


def _mock_shared_remote(monkeypatch):
    monkeypatch.setattr(
        FileSync,
        "_get_shared_remote",
        lambda self: TEAM_SHARED_REMOTE,
    )


def _event():
    return SimpleNamespace(
        event_id="$mediaevt:hs.local",
        body="方案文档 v1.md",
        url="mxc://hs.local/media1",
        server_timestamp=None,
    )


def test_archive_pushes_to_team_shared_and_notifies(tmp_path, monkeypatch):
    _set_archive_env(monkeypatch)
    _mock_shared_remote(monkeypatch)
    mc_calls = _mock_mc(monkeypatch)

    ch = _make_channel()
    local = tmp_path / "mediaevt_media.doc"
    local.write_bytes(b"doc")

    asyncio.run(
        ch._archive_media_to_shared(
            str(local), "!teamroom:hs.local", _event(), "方案文档 v1.md",
        ),
    )

    # room_id[:8].lstrip("!") == "teamroo"; event_id[:8].lstrip("$") == "mediaev"
    assert mc_calls and mc_calls[0][0] == "cp"
    assert mc_calls[0][1] == str(local)
    assert mc_calls[0][2] == (
        f"{TEAM_SHARED_REMOTE}knowledge/matrix/teamroo/"
        "mediaev_方案文档_v1.md"
    )
    room_id, _type, content, _kw = ch._client.sent[0]
    assert room_id == "!teamroom:hs.local"
    assert content["msgtype"] == "m.notice"
    assert "shared/knowledge/matrix/teamroo/mediaev_方案文档_v1.md" in (
        content["body"]
    )


def test_archive_disabled_by_env(monkeypatch, tmp_path):
    _set_archive_env(monkeypatch)
    monkeypatch.setenv("AGENTTEAMS_MEDIA_ARCHIVE", "0")
    mc_calls = _mock_mc(monkeypatch)

    ch = _make_channel()
    local = tmp_path / "mediaevt_media.doc"
    local.write_bytes(b"doc")

    asyncio.run(
        ch._archive_media_to_shared(
            str(local), "!teamroom:hs.local", _event(), "x.md",
        ),
    )

    assert mc_calls == []
    assert ch._client.sent == []


def test_archive_missing_env_disables_archiving(monkeypatch, tmp_path):
    for var in (
        "AGENTTEAMS_WORKER_NAME",
        "COPAW_WORKER_NAME",
        "AGENTTEAMS_FS_ENDPOINT",
        "COPAW_MINIO_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)
    mc_calls = _mock_mc(monkeypatch)

    ch = _make_channel()
    local = tmp_path / "mediaevt_media.doc"
    local.write_bytes(b"doc")

    asyncio.run(
        ch._archive_media_to_shared(
            str(local), "!teamroom:hs.local", _event(), "x.md",
        ),
    )

    assert mc_calls == []
    assert ch._client.sent == []


def test_archive_failure_does_not_break_flow(monkeypatch, tmp_path):
    _set_archive_env(monkeypatch)
    _mock_shared_remote(monkeypatch)

    def failing_mc(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "mc cp")

    monkeypatch.setattr("copaw_worker.sync._mc", failing_mc)

    ch = _make_channel()
    local = tmp_path / "mediaevt_media.doc"
    local.write_bytes(b"doc")

    # Must not raise; no notice is sent.
    asyncio.run(
        ch._archive_media_to_shared(
            str(local), "!teamroom:hs.local", _event(), "x.md",
        ),
    )
    assert ch._client.sent == []


def test_record_media_history_archives_file(tmp_path, monkeypatch):
    _set_archive_env(monkeypatch)
    _mock_shared_remote(monkeypatch)
    mc_calls = _mock_mc(monkeypatch)

    # Stub content classes for envs without agentscope_runtime; on the
    # production image these replace the real (structurally identical)
    # classes with same-shaped stand-ins.
    class _StubContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _StubContentType:
        FILE = "file"

    monkeypatch.setattr(
        matrix_channel, "ContentType", _StubContentType, raising=False,
    )
    monkeypatch.setattr(
        matrix_channel, "FileContent", _StubContent, raising=False,
    )

    class _FakeFileEvent:
        def __init__(self):
            self.event_id = "$mediaevt:hs.local"
            self.body = "report.pdf"
            self.url = "mxc://hs.local/media1"
            self.server_timestamp = None

    monkeypatch.setattr(matrix_channel, "RoomMessageFile", _FakeFileEvent)

    ch = _make_channel()
    ch._cfg = SimpleNamespace(vision_enabled=False)
    ch._get_display_name = lambda _room, sender: sender
    dest = tmp_path / "mediaevt_report.pdf"
    dest.write_bytes(b"pdf")

    async def fake_download(_mxc, _filename):
        return str(dest)

    ch._download_mxc = fake_download

    room = SimpleNamespace(room_id="!teamroom:hs.local")
    recorded = {}
    ch._record_history = lambda room_id, entry: recorded.update(
        room_id=room_id, entry=entry,
    )

    asyncio.run(
        ch._record_media_history(
            room, _FakeFileEvent(), "@alice:hs.local", "!teamroom:hs.local",
        ),
    )

    assert mc_calls and mc_calls[0][2] == (
        f"{TEAM_SHARED_REMOTE}knowledge/matrix/teamroo/mediaev_report.pdf"
    )
    # File part still lands in the history entry for the next turn.
    assert recorded["entry"].media_parts


def test_record_media_history_image_archives_without_vision(
    tmp_path, monkeypatch,
):
    _set_archive_env(monkeypatch)
    _mock_shared_remote(monkeypatch)
    mc_calls = _mock_mc(monkeypatch)

    class _FakeImageEvent:
        def __init__(self):
            self.event_id = "$mediaevt:hs.local"
            self.body = "photo.png"
            self.url = "mxc://hs.local/media1"
            self.server_timestamp = None

    monkeypatch.setattr(matrix_channel, "RoomMessageImage", _FakeImageEvent)

    ch = _make_channel()
    ch._cfg = SimpleNamespace(vision_enabled=False)
    ch._get_display_name = lambda _room, sender: sender
    dest = tmp_path / "mediaevt_photo.png"
    dest.write_bytes(b"png")

    async def fake_download(_mxc, _filename):
        return str(dest)

    ch._download_mxc = fake_download

    room = SimpleNamespace(room_id="!teamroom:hs.local")
    recorded = {}
    ch._record_history = lambda room_id, entry: recorded.update(entry=entry)

    asyncio.run(
        ch._record_media_history(
            room, _FakeImageEvent(), "@alice:hs.local", "!teamroom:hs.local",
        ),
    )

    # Archived even though the model cannot see images...
    assert mc_calls and mc_calls[0][2] == (
        f"{TEAM_SHARED_REMOTE}knowledge/matrix/teamroo/mediaev_photo.png"
    )
    # ...but no image part enters the turn (vision gate still applies).
    assert recorded["entry"].media_parts is None


# ---------------------------------------------------------------------------
# copaw_worker.matrix_channel — the channel actually installed at runtime
# (worker.py copies it into custom_channels/), so the archive hooks must
# exist there too.
# ---------------------------------------------------------------------------


def test_worker_channel_archive_pushes_to_team_shared_and_notifies(
    tmp_path, monkeypatch,
):
    _set_archive_env(monkeypatch)
    _mock_shared_remote(monkeypatch)
    mc_calls = _mock_mc(monkeypatch)

    ch = WorkerMatrixChannel.__new__(WorkerMatrixChannel)
    ch._user_id = "@t-1-lead:hs.local"
    ch._client = _FakeClient()
    ch._media_filesync = None

    local = tmp_path / "mediaevt_media.doc"
    local.write_bytes(b"doc")

    asyncio.run(
        ch._archive_media_to_shared(
            str(local), "!teamroom:hs.local", _event(), "方案文档 v1.md",
        ),
    )

    assert mc_calls and mc_calls[0][2] == (
        f"{TEAM_SHARED_REMOTE}knowledge/matrix/teamroo/"
        "mediaev_方案文档_v1.md"
    )
    room_id, _type, content, _kw = ch._client.sent[0]
    assert room_id == "!teamroom:hs.local"
    assert content["msgtype"] == "m.notice"
    assert "shared/knowledge/matrix/teamroo/mediaev_方案文档_v1.md" in (
        content["body"]
    )


def test_worker_channel_record_media_history_file_downloads_and_archives(
    tmp_path, monkeypatch,
):
    """Non-mentioned files: previously text-only, now downloaded + archived."""
    _set_archive_env(monkeypatch)
    _mock_shared_remote(monkeypatch)
    mc_calls = _mock_mc(monkeypatch)

    class _FakeFileEvent:
        def __init__(self):
            self.event_id = "$mediaevt:hs.local"
            self.body = "report.pdf"
            self.url = "mxc://hs.local/media1"
            self.server_timestamp = None

    monkeypatch.setattr(
        worker_matrix_channel, "RoomMessageFile", _FakeFileEvent,
    )

    ch = WorkerMatrixChannel.__new__(WorkerMatrixChannel)
    ch._user_id = "@t-1-lead:hs.local"
    ch._client = _FakeClient()
    ch._media_filesync = None
    ch._cfg = SimpleNamespace(vision_enabled=False)
    ch._get_display_name = lambda _room, sender: sender
    dest = tmp_path / "mediaevt_report.pdf"
    dest.write_bytes(b"pdf")

    async def fake_download(_mxc, _filename):
        return str(dest)

    ch._download_mxc = fake_download

    room = SimpleNamespace(room_id="!teamroom:hs.local")
    recorded = {}
    ch._record_history = lambda room_id, entry: recorded.update(
        room_id=room_id, entry=entry,
    )

    asyncio.run(
        ch._record_media_history(
            room, _FakeFileEvent(), "@alice:hs.local", "!teamroom:hs.local",
        ),
    )

    assert mc_calls and mc_calls[0][2] == (
        f"{TEAM_SHARED_REMOTE}knowledge/matrix/teamroo/mediaev_report.pdf"
    )
    # History entry still lands for the next turn, now with a file part.
    assert recorded["entry"].body == "[sent a file: report.pdf]"
    file_parts = recorded["entry"].media_parts
    assert file_parts and file_parts[0]["type"] == "file"
