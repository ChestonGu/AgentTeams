"""媒体事件进群聊视野：gateway 分发 + app 记账行（带归档路径）。

bridge worker 不归档（team leader/copaw 侧负责）；媒体上传只落一行带
确定性归档路径的记账行——后续 mention 触发 turn 时 agent 顺着路径
filesync 拉取。文件名里的 "@xxx" 不是 mention（短路在 filter 之前）。
"""
import asyncio
from types import SimpleNamespace

from cimicode_bridge.app import (
    BridgeApp,
    _media_history_line,
    _media_shared_rel_path,
)
from cimicode_bridge.matrix.gateway import MatrixGateway
from cimicode_bridge.session import HistoryManager

ROOM = "!teamroom:hs"
EVENT = "$mediaevt:hs"


def test_media_shared_rel_path_matches_copaw_formula():
    # room_id[:8].lstrip("!") == "teamroo"; event_id[:8].lstrip("$") == "mediaev"
    path = _media_shared_rel_path(ROOM, EVENT, "方案文档 v1.md")
    assert path == "shared/knowledge/matrix/teamroo/mediaev_方案文档_v1.md"


def test_media_shared_rel_path_sanitizes_separators():
    path = _media_shared_rel_path(ROOM, EVENT, "a/b\\c:d.md")
    assert path.endswith("mediaev_a_b_c_d.md")


def test_media_history_line_types():
    rel = "shared/knowledge/matrix/teamroo/mediaev_x.md"
    assert _media_history_line("m.file", "x.md", ROOM, EVENT) == (
        f"[sent a file: x.md | archived: {rel}]"
    )
    assert _media_history_line("m.image", "p.png", ROOM, EVENT) == (
        "[sent an image: p.png | archived: "
        "shared/knowledge/matrix/teamroo/mediaev_p.png]"
    )
    assert _media_history_line("m.audio", "a.mp3", ROOM, EVENT) == (
        "[sent audio: a.mp3]"
    )
    assert _media_history_line("m.video", "v.mp4", ROOM, EVENT) == (
        "[sent a video: v.mp4]"
    )


def test_gateway_dispatches_media_events():
    received = []

    async def on_message(room_id, sender, event_id, content):
        received.append((room_id, sender, event_id, content))

    gateway = MatrixGateway.__new__(MatrixGateway)
    gateway.user_id = "@w1:hs"
    gateway.on_message = on_message

    event = SimpleNamespace(
        sender="@alice:hs",
        event_id=EVENT,
        body="spec.md",
        source={"content": {"msgtype": "m.file", "url": "mxc://hs/m1"}},
    )
    asyncio.run(gateway._on_room_message(SimpleNamespace(room_id=ROOM), event))

    assert len(received) == 1
    room_id, sender, event_id, content = received[0]
    assert (room_id, sender, event_id) == (ROOM, "@alice:hs", EVENT)
    assert content["msgtype"] == "m.file"
    assert content["body"] == "spec.md"


def test_gateway_skips_own_media_events():
    received = []

    async def on_message(*_args):
        received.append(1)

    gateway = MatrixGateway.__new__(MatrixGateway)
    gateway.user_id = "@w1:hs"
    gateway.on_message = on_message

    event = SimpleNamespace(sender="@w1:hs", event_id=EVENT, body="x", source={})
    asyncio.run(gateway._on_room_message(SimpleNamespace(room_id=ROOM), event))
    assert received == []


def _bare_app() -> BridgeApp:
    app = BridgeApp.__new__(BridgeApp)
    app.history_manager = HistoryManager()
    return app


def test_media_upload_records_ambient_line_without_turn():
    app = _bare_app()
    asyncio.run(app.handle_matrix_message(
        ROOM, "@alice:hs", EVENT,
        {"msgtype": "m.file", "body": "spec.md"},
    ))
    entries = app.history_manager.room(ROOM).all()
    assert len(entries) == 1
    assert entries[0]["content"] == (
        "[sent a file: spec.md | archived: "
        "shared/knowledge/matrix/teamroo/mediaev_spec.md]"
    )
    assert entries[0]["event_id"] == EVENT


def test_media_filename_mention_never_triggers_turn():
    # 文件名带 @leader：短路在 mention filter 之前——__new__ 裸实例没有
    # mention_filter 属性，若未短路会直接 AttributeError（负证）。
    app = _bare_app()
    asyncio.run(app.handle_matrix_message(
        ROOM, "@alice:hs", EVENT,
        {"msgtype": "m.file", "body": "给@leader的说明.docx"},
    ))
    entries = app.history_manager.room(ROOM).all()
    assert len(entries) == 1
    assert entries[0]["event_id"] == EVENT
    assert "给@leader的说明.docx" in entries[0]["content"]
