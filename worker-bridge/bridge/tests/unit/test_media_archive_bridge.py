"""bridge 侧媒体归档：对齐 copaw matrix_channel 的 team_leader 归档行为。

门禁（开关/msgtype/role/DM）→ 下载 → S3 put → m.notice 全链；
失败仅降级不断流；worker bridge 与 DM 不归档；audio/video 不归档。
"""
import asyncio
from types import SimpleNamespace

from cimicode_bridge.bootstrap import WorkerBootstrapConfig
from cimicode_bridge.media_archive import (
    MediaArchiver,
    media_shared_rel_path,
    spawn_media_archive,
)

ROOM = "!teamroom:hs"
EVENT = "$mediaevt:hs"

RUNTIME_YAML = """\
apiVersion: agentteams.io/v1beta1
kind: MemberRuntimeConfig
member:
  name: t1-lead
  role: team_leader
  personalRoomId: '!personal:hs'
team:
  name: t-l1
  teamRoomId: '!teamroom:hs'
  leaderDmRoomId: '!leaderdm:hs'
storage:
  sharedPrefix: teams/t-l1/shared
"""


def _worker_files(role: str = "team_leader") -> WorkerBootstrapConfig:
    return WorkerBootstrapConfig(
        openclaw={},
        runtime_yaml=RUNTIME_YAML.replace("team_leader", role),
    )


class _FakeS3Client:
    def __init__(self, fail: bool = False) -> None:
        self.puts: list[tuple[str, str, int]] = []
        self.fail = fail

    def put_object(self, bucket, key, data, length, content_type=None):
        if self.fail:
            raise RuntimeError("put boom")
        self.puts.append((bucket, key, length))


def _archiver(fail_put: bool = False) -> tuple[MediaArchiver, _FakeS3Client]:
    client = _FakeS3Client(fail=fail_put)
    s3 = SimpleNamespace(client=client, bucket="agentteams-storage")
    return MediaArchiver(s3=s3), client  # type: ignore[arg-type]


class _FakeGateway:
    def __init__(self, data: bytes | None = b"x") -> None:
        self.data = data
        self.notices: list[tuple[str, str]] = []

    async def download_media(self, mxc_url: str) -> bytes | None:
        return self.data

    async def send_notice(self, room_id: str, body: str) -> None:
        self.notices.append((room_id, body))


# ---- 门禁矩阵 ----

def test_should_archive_leader_group_image_file():
    archiver, _ = _archiver()
    files = _worker_files()
    assert archiver.should_archive(files, ROOM, "m.image") is True
    assert archiver.should_archive(files, ROOM, "m.file") is True


def test_should_archive_worker_rejected():
    archiver, _ = _archiver()
    assert archiver.should_archive(_worker_files("worker"), ROOM, "m.file") is False


def test_should_archive_missing_role_rejected():
    archiver, _ = _archiver()
    assert archiver.should_archive(_worker_files(""), ROOM, "m.file") is False


def test_should_archive_none_files_rejected():
    archiver, _ = _archiver()
    assert archiver.should_archive(None, ROOM, "m.file") is False


def test_should_archive_dm_rooms_skipped():
    archiver, _ = _archiver()
    files = _worker_files()
    # personalRoomId 与 leaderDmRoomId 都是 DM：不归档（对齐 copaw not is_dm）
    assert archiver.should_archive(files, "!personal:hs", "m.file") is False
    assert archiver.should_archive(files, "!leaderdm:hs", "m.file") is False


def test_should_archive_audio_video_skipped():
    archiver, _ = _archiver()
    files = _worker_files()
    assert archiver.should_archive(files, ROOM, "m.audio") is False
    assert archiver.should_archive(files, ROOM, "m.video") is False


def test_should_archive_env_off(monkeypatch):
    monkeypatch.setenv("AGENTTEAMS_MEDIA_ARCHIVE", "0")
    archiver, _ = _archiver()
    assert archiver.should_archive(_worker_files(), ROOM, "m.file") is False


# ---- bootstrap property ----

def test_member_role_and_team_object_key():
    files = _worker_files()
    assert files.member_role == "team_leader"
    key = files.team_object_key("shared/knowledge/matrix/teamroo/mediaev_x.md")
    assert key == "teams/t-l1/shared/knowledge/matrix/teamroo/mediaev_x.md"


def test_team_object_key_without_prefix():
    files = WorkerBootstrapConfig(openclaw={}, runtime_yaml="member:\n  role: team_leader\n")
    key = files.team_object_key("shared/knowledge/matrix/r/x.md")
    assert key == "shared/knowledge/matrix/r/x.md"


# ---- archive 主链 ----

def test_archive_success_puts_and_notices():
    archiver, s3_client = _archiver()
    gateway = _FakeGateway(data=b"file-bytes")
    rel = asyncio.run(archiver.archive(
        _worker_files(), gateway,
        room_id=ROOM, event_id=EVENT, body="spec.md",
        mxc_url="mxc://hs/m1",
    ))
    assert rel == "shared/knowledge/matrix/teamroo/mediaev_spec.md"
    assert s3_client.puts == [(
        "agentteams-storage",
        "teams/t-l1/shared/knowledge/matrix/teamroo/mediaev_spec.md",
        len(b"file-bytes"),
    )]
    # notice 文案与 copaw 逐字一致
    assert gateway.notices == [(
        ROOM,
        "已归档文件 shared/knowledge/matrix/teamroo/mediaev_spec.md"
        "（团队成员可通过 filesync pull 获取）",
    )]


def test_archive_download_failure_no_put_no_notice():
    archiver, s3_client = _archiver()
    gateway = _FakeGateway(data=None)
    rel = asyncio.run(archiver.archive(
        _worker_files(), gateway,
        room_id=ROOM, event_id=EVENT, body="spec.md", mxc_url="mxc://hs/m1",
    ))
    assert rel is None
    assert s3_client.puts == []
    assert gateway.notices == []


def test_archive_put_failure_no_notice():
    archiver, s3_client = _archiver(fail_put=True)
    gateway = _FakeGateway(data=b"x")
    rel = asyncio.run(archiver.archive(
        _worker_files(), gateway,
        room_id=ROOM, event_id=EVENT, body="spec.md", mxc_url="mxc://hs/m1",
    ))
    assert rel is None
    assert gateway.notices == []


def test_archive_non_mxc_rejected():
    archiver, s3_client = _archiver()
    gateway = _FakeGateway(data=b"x")
    rel = asyncio.run(archiver.archive(
        _worker_files(), gateway,
        room_id=ROOM, event_id=EVENT, body="spec.md", mxc_url="https://x",
    ))
    assert rel is None
    assert s3_client.puts == []
    assert gateway.notices == []


# ---- spawn 挂点 ----

def test_spawn_media_archive_runs_task():
    archiver, s3_client = _archiver()
    gateway = _FakeGateway(data=b"z")
    state = SimpleNamespace(
        media_archiver=archiver,
        worker_files=_worker_files(),
        matrix_gateway=gateway,
    )

    async def run():
        spawn_media_archive(
            state, room_id=ROOM, event_id=EVENT,
            body="spec.md", msgtype="m.file", mxc_url="mxc://hs/m1",
        )
        await asyncio.sleep(0)  # 让 fire-and-forget 任务跑完

    asyncio.run(run())
    assert len(s3_client.puts) == 1
    assert len(gateway.notices) == 1


def test_spawn_media_archive_worker_skipped():
    archiver, s3_client = _archiver()
    state = SimpleNamespace(
        media_archiver=archiver,
        worker_files=_worker_files("worker"),
        matrix_gateway=_FakeGateway(),
    )
    spawn_media_archive(
        state, room_id=ROOM, event_id=EVENT,
        body="spec.md", msgtype="m.file", mxc_url="mxc://hs/m1",
    )
    assert s3_client.puts == []


def test_spawn_media_archive_no_archiver_noop():
    # 本地模式（s3 未装配 / 裸实例）：getattr 默认值兜底，静默跳过
    state = SimpleNamespace(worker_files=None, matrix_gateway=None)
    spawn_media_archive(
        state, room_id=ROOM, event_id=EVENT,
        body="spec.md", msgtype="m.file", mxc_url="mxc://hs/m1",
    )


# ---- 公式与 copaw 逐字对齐（防回归漂移） ----

def test_formula_unchanged():
    # [:8] 含首字符（!/$ 去除后 7 位）——与 copaw 逐字对齐
    assert media_shared_rel_path("!roomaaaa:hs", "$eventaaaa:hs", "a b.md") == (
        "shared/knowledge/matrix/roomaaa/eventaa_a_b.md"
    )
