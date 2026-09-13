"""日志装配：stderr console + 可选容器内轮转文件（对齐 qwenpaw_worker/log.py 形态）。

- 双通道：console（kubectl logs 实时看）+ RotatingFileHandler（BRIDGE_LOG_FILE
  指定时启用；部署清单挂 emptyDir 卷，容器重启不丢、pod 删除即弃）
- env 旋钮（带边界防误配）：BRIDGE_LOG_LEVEL / BRIDGE_LOG_FILE /
  BRIDGE_LOG_MAX_BYTES / BRIDGE_LOG_BACKUP_COUNT
- 幂等：handler 打标记，重复调用复用/摘除，不叠加不重复输出
- 三方噪音隔离：nio / httpx / httpcore / asyncio 压到 WARNING
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from typing import Optional

LOG_FORMAT = ("%(asctime)s [%(levelname)s] [%(threadName)s(%(thread)d)] "
              "%(name)s %(filename)s:%(lineno)d: %(message)s")
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024     # 5 MiB
MAX_LOG_MAX_BYTES = 20 * 1024 * 1024        # 20 MiB（超限回落默认，防误配打满卷）
DEFAULT_LOG_BACKUP_COUNT = 3
MAX_LOG_BACKUP_COUNT = 50
DEFAULT_LOG_FILE_NAME = "bridge.log"

_CONSOLE_HANDLER_MARK = "_bridge_console_handler"
_FILE_HANDLER_MARK = "_bridge_file_handler"

# matrix-nio 的 sync 细节、httpx/httpcore 的连接日志是 INFO 级洪水，
# 会淹没 bridge 自身的决策日志（消息判定、turn 起止）。
_QUIET_LOGGERS = ("nio", "httpx", "httpcore", "asyncio")


def setup_logging(level: str | int | None = None) -> Optional[Path]:
    """配置根日志，返回文件日志路径（未启用文件通道时返回 None）。

    level 参数 > ``BRIDGE_LOG_LEVEL`` env > INFO；支持数字或名称。
    ``BRIDGE_LOG_FILE`` 为空 → 纯 console（本地开发/测试路径行为不变）；
    路径不可写（只读卷/权限）→ 降级纯 console 并告警，不因此拒启。
    """
    root, formatter, resolved_level = _configure_console_logging(level)

    log_file = os.environ.get("BRIDGE_LOG_FILE", "").strip()
    if not log_file:
        _remove_marked_handlers(root, _FILE_HANDLER_MARK)
        return None

    path = Path(log_file)
    max_bytes = _bounded_int(
        os.environ.get("BRIDGE_LOG_MAX_BYTES"),
        DEFAULT_LOG_MAX_BYTES,
        minimum=1,
        maximum=MAX_LOG_MAX_BYTES,
    )
    backup_count = _bounded_int(
        os.environ.get("BRIDGE_LOG_BACKUP_COUNT"),
        DEFAULT_LOG_BACKUP_COUNT,
        minimum=0,
        maximum=MAX_LOG_BACKUP_COUNT,
    )

    file_handler = _find_marked_handler(root, _FILE_HANDLER_MARK)
    if file_handler is not None:
        # 幂等重配：复用已有 handler，仅刷新参数（不产生重复行/泄漏 fd）
        file_handler.setLevel(resolved_level)
        file_handler.setFormatter(formatter)
        if isinstance(file_handler, RotatingFileHandler):
            file_handler.maxBytes = max_bytes
            file_handler.backupCount = backup_count
        _log_configured(path, resolved_level, max_bytes, backup_count, reused=True)
        return Path(getattr(file_handler, "baseFilename", str(path)))

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        new_handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "bridge log file setup failed component=bridge stage=logging event=failed "
            "path=%s error_type=%s",
            path,
            type(exc).__name__,
        )
        return None

    setattr(new_handler, _FILE_HANDLER_MARK, True)
    new_handler.setLevel(resolved_level)
    new_handler.setFormatter(formatter)
    root.addHandler(new_handler)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    _log_configured(path, resolved_level, max_bytes, backup_count, reused=False)
    return path


def _configure_console_logging(level: str | int | None = None) -> tuple[logging.Logger, logging.Formatter, int]:
    """根 logger + 标记式 console handler（幂等）。"""
    root = logging.getLogger()
    resolved = _resolve_level(level)
    formatter = logging.Formatter(LOG_FORMAT)
    root.setLevel(resolved)

    handler = _find_marked_handler(root, _CONSOLE_HANDLER_MARK)
    if handler is None:
        handler = logging.StreamHandler()
        setattr(handler, _CONSOLE_HANDLER_MARK, True)
        root.addHandler(handler)
    handler.setLevel(resolved)
    handler.setFormatter(formatter)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return root, formatter, resolved


def _resolve_level(level: str | int | None) -> int:
    """级别解析：参数 > env > INFO；支持数字（"10"）或名称（"INFO"）。"""
    if level is None:
        level = os.environ.get("BRIDGE_LOG_LEVEL", "INFO").strip()
    if isinstance(level, int):
        return level
    if level.isdigit():
        return int(level)
    resolved = logging.getLevelName(level.upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def _log_configured(path: Path, level: int, max_bytes: int, backup_count: int, *, reused: bool) -> None:
    logging.getLogger(__name__).info(
        "bridge logging configured component=bridge stage=logging event=configured file_enabled=True "
        "path=%s max_bytes=%s backup_count=%s level=%s reused=%s",
        path,
        max_bytes,
        backup_count,
        logging.getLevelName(level),
        reused,
    )


def _find_marked_handler(root: logging.Logger, mark: str) -> Optional[logging.Handler]:
    for handler in root.handlers:
        if getattr(handler, mark, False):
            return handler
    return None


def _remove_marked_handlers(root: logging.Logger, mark: str) -> None:
    for handler in list(root.handlers):
        if getattr(handler, mark, False):
            root.removeHandler(handler)
            handler.close()


def _bounded_int(value: Optional[str], default: int, *, minimum: int, maximum: int) -> int:
    """带边界的 env 整数解析：非法/低于下限 → 默认值，超上限 → 截到上限。"""
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return default
    return min(parsed, maximum)
