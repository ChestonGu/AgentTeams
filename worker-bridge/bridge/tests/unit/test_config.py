from pathlib import Path

from cimicode_bridge.config import BridgeConfig, load_config


def test_default_config_loads():
    cfg = load_config(Path("does-not-exist.yaml"))
    assert isinstance(cfg, BridgeConfig)
    # adapter 默认未定（空）——bridge 等 env / runtime.yaml bridge 段裁决，
    # 绝不静默回落到任何写死的形态。
    assert cfg.runtime.adapter == ""
    assert cfg.history.max_entries == 50


def test_runtime_fields_have_no_hardcoded_defaults():
    """base_url / template_id 必须空默认：合法来源只有 bridge 段与
    BRIDGE_RUNTIME_* env（mock 时代的 "http://cimicode-gateway" /
    "default-template" 写死残留已清除）。"""
    cfg = BridgeConfig()
    assert cfg.runtime.base_url == ""
    assert cfg.runtime.template_id == ""
    # 透传袋空默认：未投影 runtimeParameter 时 chat 请求体零追加键
    assert cfg.runtime.runtime_parameters == {}
