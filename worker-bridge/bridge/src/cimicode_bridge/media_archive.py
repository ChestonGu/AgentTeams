"""群聊媒体归档：bridge 收侧对齐 copaw matrix_channel 的归档行为（4d5136ae）。

copaw 语义：group room 每个媒体事件（@ 与否）由 team_leader 下载并
mc cp 到 ``teams/<t>/shared/knowledge/matrix/<room8>/<eid>_<净化名>``，
成功后回 m.notice 告知 filesync pull 路径；``AGENTTEAMS_MEDIA_ARCHIVE=0``
可关；缺 MinIO env 静默禁用；失败仅 warning 不断消息流。bridge 是
cimicode 形态的 channel 等价物，归档是确定性代码路径，不靠 skill/模型。

归档范围对齐 copaw channel 的处理面：m.image / m.file 归档；
m.audio / m.video 仅进群聊视野记账行。E2EE 媒体在 copaw 侧另有解密
下载路径，bridge 通道不支持 E2EE，不在此列。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型引用，避免运行时循环导入
    from cimicode_bridge.bootstrap import S3Bootstrap, WorkerBootstrapConfig
    from cimicode_bridge.matrix.gateway import MatrixGateway

logger = logging.getLogger(__name__)

# 与 copaw matrix_channel 同公式的确定性归档路径（房间/事件/文件名三原料
# 全在 Matrix 事件里，无需下载即可推出归档位置）。
_MEDIA_SANITIZE_RE = re.compile(r"[^\w.\-]+")

# 进入归档的 msgtype（对齐 copaw channel 处理面：image/file）
ARCHIVED_MSGTYPES = frozenset({"m.image", "m.file"})

# 归档者角色：team_leader（copaw 的 standalone 场景 bridge 侧不存在，
# 见模块 docstring——无 team 段的本地模式缺 MinIO env 自然禁用）。
ARCHIVER_ROLES = frozenset({"team_leader"})

NOTICE_TEMPLATE = "已归档文件 {rel_path}（团队成员可通过 filesync pull 获取）"


def media_shared_rel_path(room_id: str, event_id: str, body: str) -> str:
    """shared/knowledge/matrix/<房间8>/<事件8>_<净化文件名>（对齐 copaw 公式）。"""
    eid = (event_id or "")[:8].lstrip("$")
    room_key = (room_id or "")[:8].lstrip("!")
    safe = _MEDIA_SANITIZE_RE.sub("_", body or "").strip("._")[:120]
    safe = safe or f"matrix_media_{eid}"
    return f"shared/knowledge/matrix/{room_key}/{eid}_{safe}"


@dataclass
class MediaArchiver:
    """媒体归档器：下载 → S3 put → m.notice，全程尽力而为不阻断消息流。"""

    s3: "S3Bootstrap"

    @staticmethod
    def archive_enabled() -> bool:
        """AGENTTEAMS_MEDIA_ARCHIVE 开关（默认开，对齐 copaw）。"""
        flag = os.environ.get("AGENTTEAMS_MEDIA_ARCHIVE", "1")
        return flag.strip().lower() not in ("0", "false", "no", "off")

    def should_archive(
        self,
        worker_files: "WorkerBootstrapConfig | None",
        room_id: str,
        msgtype: str,
    ) -> bool:
        """归档门禁：开关 + msgtype + leader 角色 + 群聊（非 DM）。

        role 判定读 runtime.yaml（worker_files 每 turn 重拉，事件回调用
        最近快照——与 mention 门禁的花名册同精度）。unknown/missing 角色
        降级为不归档：房保持安静，文件可重发（copaw 同语义）。
        """
        if not self.archive_enabled():
            return False
        if msgtype not in ARCHIVED_MSGTYPES:
            return False
        if worker_files is None:
            return False
        return (
            worker_files.member_role in ARCHIVER_ROLES
            and not worker_files.is_dm_room(room_id)
        )

    async def archive(
        self,
        worker_files: "WorkerBootstrapConfig",
        gateway: "MatrixGateway",
        *,
        room_id: str,
        event_id: str,
        body: str,
        mxc_url: str,
    ) -> str | None:
        """下载媒体并归档到 team shared，成功后回 m.notice。

        返回归档的 shared 相对路径；任一步失败仅 warning 返回 None，
        绝不断消息流（对齐 copaw）。幂等：同 key 覆盖写，重复事件安全。
        """
        if not mxc_url.startswith("mxc://"):
            return None
        rel_path = media_shared_rel_path(room_id, event_id, body)
        remote_key = worker_files.team_object_key(rel_path)
        data = await gateway.download_media(mxc_url)
        if data is None:
            return None
        try:
            self.s3.client.put_object(
                self.s3.bucket,
                remote_key,
                BytesIO(data),
                len(data),
                content_type="application/octet-stream",
            )
        except Exception as exc:
            logger.warning(
                "media archive put failed for %s (%s): %s", event_id, remote_key, exc
            )
            return None
        logger.info(
            "media archived room=%s event=%s -> %s (%d bytes)",
            room_id, event_id, remote_key, len(data),
        )
        await gateway.send_notice(room_id, NOTICE_TEMPLATE.format(rel_path=rel_path))
        return rel_path


def spawn_media_archive(
    app_state: Any,
    *,
    room_id: str,
    event_id: str,
    body: str,
    msgtype: str,
    mxc_url: str,
) -> None:
    """从消息回调拉起归档任务（fire-and-forget，异常全部吞在任务内）。

    app_state 提供 worker_files / matrix_gateway / media_archiver 三项；
    未装配（本地模式/无 MinIO env）时静默跳过——等价 copaw 的静默禁用。
    """
    archiver = getattr(app_state, "media_archiver", None)
    worker_files = getattr(app_state, "worker_files", None)
    gateway = getattr(app_state, "matrix_gateway", None)
    if archiver is None or gateway is None:
        return
    if not archiver.should_archive(worker_files, room_id, msgtype):
        return
    if not mxc_url:
        return

    async def _run() -> None:
        try:
            await archiver.archive(
                worker_files,
                gateway,
                room_id=room_id,
                event_id=event_id,
                body=body,
                mxc_url=mxc_url,
            )
        except Exception as exc:  # pragma: no cover - 兜底：归档永不阻断消息流
            logger.warning(
                "media archive task failed for %s: %s", event_id, exc
            )

    asyncio.create_task(_run())
