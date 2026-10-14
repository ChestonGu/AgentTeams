"""Tests for room-media archiving to team shared/knowledge/matrix/.

Pins down the contract of the media archiving pipeline:

- only the designated archiver (``member.role`` in runtime.yaml:
  team_leader / standalone) downloads and mc-cp's room media to the
  team shared tree — team workers record a download-free history line;
- every member's history line carries the deterministic shared path
  (``_media_shared_rel_path``), so the line alone is enough to
  filesync-pull the archived file later;
- archiving success posts exactly one m.notice from the single archiver
  (the path also lives in every member's history line);
- ``AGENTTEAMS_MEDIA_ARCHIVE=0`` opts out; a missing MinIO environment
  disables archiving instead of failing; archive failures never break
  the message flow;
- in ``_record_media_history`` the download is decoupled from the vision
  gate: images archive even when the model cannot see them, while the
  turn still gets no image part.
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
EXPECTED_REL_PATH = "shared/knowledge/matrix/teamroo/mediaev_方案文档_v1.md"


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


def _be_archiver(monkeypatch, module, role: str = "team_leader"):
    """Pin the role gate for the module under test (runtime.yaml absent
    in the test env would otherwise default every instance to worker)."""
    monkeypatch.setattr(
        module,
        "_should_archive_media",
        lambda: role in ("team_leader", "standalone"),
    )
    monkeypatch.setattr(
        module,
        "_runtime_config_field",
        lambda section, key: role if (section, key) == ("member", "role") else "",
    )


def _be_worker(monkeypatch, module):
    _be_archiver(monkeypatch, module, role="worker")


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


def test_archive_pushes_to_team_shared_with_single_notice(tmp_path, monkeypatch):
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
    # Exactly one success notice (single archiver — workers never reach this
    # method; the old N-notices-per-file flood is gone).
    assert len(ch._client.sent) == 1
    _room, _mtype, sent_content, _kw = ch._client.sent[0]
    assert sent_content["msgtype"] == "m.notice"
    assert sent_content["body"] == (
        "已归档文件 shared/knowledge/matrix/teamroo/"
        "mediaev_方案文档_v1.md（团队成员可通过 filesync pull 获取）"
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

    # Must not raise; nothing is sent to the room.
    asyncio.run(
        ch._archive_media_to_shared(
            str(local), "!teamroom:hs.local", _event(), "x.md",
        ),
    )
    assert ch._client.sent == []


def test_should_archive_media_role_gate(monkeypatch):
    for role, expected in (
        ("team_leader", True),
        ("standalone", True),
        ("worker", False),
        ("", False),
    ):
        monkeypatch.setattr(
            worker_matrix_channel,
            "_runtime_config_field",
            lambda section, key, _role=role: (
                _role if (section, key) == ("member", "role") else ""
            ),
        )
        assert worker_matrix_channel._should_archive_media() is expected, (
            f"role={role!r}"
        )


def test_record_media_history_archives_file(tmp_path, monkeypatch):
    _set_archive_env(monkeypatch)
    _be_archiver(monkeypatch, matrix_channel)
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
    # History line carries the deterministic shared path …
    assert recorded["entry"].body == (
        "[sent a file: report.pdf | archived: "
        "shared/knowledge/matrix/teamroo/mediaev_report.pdf]"
    )
    # … and the file part still lands in the history entry for the next turn.
    assert recorded["entry"].media_parts


def test_record_media_history_worker_records_line_without_download(
    tmp_path, monkeypatch,
):
    """Team workers: download-free, archive-free, path-bearing line only."""
    _set_archive_env(monkeypatch)
    _be_worker(monkeypatch, matrix_channel)
    mc_calls = _mock_mc(monkeypatch)

    class _FakeFileEvent:
        def __init__(self):
            self.event_id = "$mediaevt:hs.local"
            self.body = "report.pdf"
            self.url = "mxc://hs.local/media1"
            self.server_timestamp = None

    monkeypatch.setattr(matrix_channel, "RoomMessageFile", _FakeFileEvent)

    ch = _make_channel()
    ch._cfg = SimpleNamespace(vision_enabled=True)
    ch._get_display_name = lambda _room, sender: sender

    downloads = []

    async def fake_download(_mxc, _filename):
        downloads.append(_mxc)
        return str(tmp_path / "mediaevt_report.pdf")

    ch._download_mxc = fake_download

    room = SimpleNamespace(room_id="!teamroom:hs.local")
    recorded = {}
    ch._record_history = lambda room_id, entry: recorded.update(entry=entry)

    asyncio.run(
        ch._record_media_history(
            room, _FakeFileEvent(), "@alice:hs.local", "!teamroom:hs.local",
        ),
    )

    # No download, no MinIO write, no media parts — the line says it all.
    assert downloads == []
    assert mc_calls == []
    assert recorded["entry"].media_parts is None
    assert recorded["entry"].body == (
        "[sent a file: report.pdf | archived: "
        "shared/knowledge/matrix/teamroo/mediaev_report.pdf]"
    )


def test_record_media_history_image_archives_without_vision(
    tmp_path, monkeypatch,
):
    _set_archive_env(monkeypatch)
    _be_archiver(monkeypatch, matrix_channel)
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


def test_worker_channel_archive_posts_single_notice(
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
    # Single archiver, single notice — never N per file.
    assert len(ch._client.sent) == 1
    assert ch._client.sent[0][2]["msgtype"] == "m.notice"
    assert "mediaev_方案文档_v1.md" in ch._client.sent[0][2]["body"]


def test_worker_channel_record_media_history_file_downloads_and_archives(
    tmp_path, monkeypatch,
):
    """Leader on the non-mention path: download + archive + path in line."""
    _set_archive_env(monkeypatch)
    _be_archiver(monkeypatch, worker_matrix_channel)
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
    assert recorded["entry"].body == (
        "[sent a file: report.pdf | archived: "
        "shared/knowledge/matrix/teamroo/mediaev_report.pdf]"
    )
    file_parts = recorded["entry"].media_parts
    assert file_parts and file_parts[0]["type"] == "file"


def test_worker_channel_record_media_history_worker_skips_download(
    tmp_path, monkeypatch,
):
    """Runtime channel, team worker: line only — no download, no archive."""
    _set_archive_env(monkeypatch)
    _be_worker(monkeypatch, worker_matrix_channel)
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
    ch._user_id = "@t-1-w1:hs.local"
    ch._client = _FakeClient()
    ch._media_filesync = None
    ch._cfg = SimpleNamespace(vision_enabled=False)
    ch._get_display_name = lambda _room, sender: sender

    downloads = []

    async def fake_download(_mxc, _filename):
        downloads.append(_mxc)
        return str(tmp_path / "mediaevt_report.pdf")

    ch._download_mxc = fake_download

    room = SimpleNamespace(room_id="!teamroom:hs.local")
    recorded = {}
    ch._record_history = lambda room_id, entry: recorded.update(entry=entry)

    asyncio.run(
        ch._record_media_history(
            room, _FakeFileEvent(), "@alice:hs.local", "!teamroom:hs.local",
        ),
    )

    assert downloads == []
    assert mc_calls == []
    assert recorded["entry"].media_parts is None
    assert recorded["entry"].body == (
        "[sent a file: report.pdf | archived: "
        "shared/knowledge/matrix/teamroo/mediaev_report.pdf]"
    )
