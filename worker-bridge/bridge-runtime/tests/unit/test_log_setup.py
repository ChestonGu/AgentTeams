"""log.py 单测：双通道装配、幂等重配、env 旋钮边界、三方噪音隔离。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from cimicode_bridge.log import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    _bounded_int,
    _FILE_HANDLER_MARK,
    setup_logging,
)

import pytest


@pytest.fixture()
def clean_logging():
    """测试后摘掉本模块挂的标记 handler（Windows 下文件句柄不闭会锁住 tmp_path）。"""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _FILE_HANDLER_MARK, False) or getattr(handler, "_bridge_console_handler", False):
            root.removeHandler(handler)
            handler.close()


def _marked_file_handlers() -> list[RotatingFileHandler]:
    return [h for h in logging.getLogger().handlers if getattr(h, _FILE_HANDLER_MARK, False)]


def test_console_only_when_file_env_absent(clean_logging, monkeypatch):
    monkeypatch.delenv("BRIDGE_LOG_FILE", raising=False)
    result = setup_logging()
    assert result is None
    assert _marked_file_handlers() == []


def test_file_channel_writes_and_applies_env_knobs(clean_logging, monkeypatch, tmp_path):
    log_path = tmp_path / "bridge.log"
    monkeypatch.setenv("BRIDGE_LOG_FILE", str(log_path))
    monkeypatch.setenv("BRIDGE_LOG_MAX_BYTES", "200")
    monkeypatch.setenv("BRIDGE_LOG_BACKUP_COUNT", "2")
    result = setup_logging()
    assert result == log_path
    handlers = _marked_file_handlers()
    assert len(handlers) == 1
    assert handlers[0].maxBytes == 200
    assert handlers[0].backupCount == 2
    logging.getLogger("cimicode_bridge.test").info("hello file channel")
    handlers[0].flush()
    assert "hello file channel" in log_path.read_text(encoding="utf-8")


def test_setup_idempotent_reuses_marked_handler(clean_logging, monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_LOG_FILE", str(tmp_path / "bridge.log"))
    setup_logging()
    first = _marked_file_handlers()[0]
    setup_logging()  # 重复装配（如 lifespan 重入）
    handlers = _marked_file_handlers()
    assert len(handlers) == 1
    assert handlers[0] is first


def test_invalid_knobs_fall_back_to_defaults(clean_logging, monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_LOG_FILE", str(tmp_path / "bridge.log"))
    monkeypatch.setenv("BRIDGE_LOG_MAX_BYTES", "0")          # 低于下限 → 默认
    monkeypatch.setenv("BRIDGE_LOG_BACKUP_COUNT", "not-a-number")
    setup_logging()
    handler = _marked_file_handlers()[0]
    assert handler.maxBytes == DEFAULT_LOG_MAX_BYTES
    assert handler.backupCount == DEFAULT_LOG_BACKUP_COUNT


def test_quiet_loggers_suppressed_to_warning(clean_logging, monkeypatch):
    monkeypatch.delenv("BRIDGE_LOG_FILE", raising=False)
    setup_logging()
    for name in ("nio", "httpx", "httpcore", "asyncio"):
        assert logging.getLogger(name).level == logging.WARNING


def test_bounded_int_boundaries():
    assert _bounded_int("7", 5, minimum=1, maximum=10) == 7
    assert _bounded_int("99", 5, minimum=1, maximum=10) == 10      # 超上限截断
    assert _bounded_int("0", 5, minimum=1, maximum=10) == 5        # 低于下限回落默认
    assert _bounded_int(None, 5, minimum=1, maximum=10) == 5
    assert _bounded_int("garbage", 5, minimum=1, maximum=10) == 5
