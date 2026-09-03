from pathlib import Path

from cimicode_bridge.config import BridgeConfig, load_config


def test_default_config_loads():
    cfg = load_config(Path("does-not-exist.yaml"))
    assert isinstance(cfg, BridgeConfig)
    assert cfg.runtime.adapter == "cimicode"
    assert cfg.history.max_entries == 50


def test_runtime_fields_are_present():
    cfg = BridgeConfig()
    assert cfg.runtime.base_url == "http://cimicode-gateway"
    assert cfg.runtime.template_id == "default-template"
