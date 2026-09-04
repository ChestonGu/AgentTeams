from __future__ import annotations

import json
from pathlib import Path

from cimicode_bridge.app import BridgeApp
from cimicode_bridge.bootstrap import S3Bootstrap, WorkerBootstrapConfig


class FakeMinio:
    def __init__(self, objects: dict[str, str]) -> None:
        self.objects = objects

    def get_object(self, bucket: str, key: str):
        if key not in self.objects:
            raise KeyError(key)
        return _FakeResponse(self.objects[key])


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> bytes:
        return self._text.encode("utf-8")

    def close(self) -> None:
        pass

    def release_conn(self) -> None:
        pass


def _bootstrap(objects: dict[str, str], monkeypatch, worker_name: str = "w1") -> S3Bootstrap:
    monkeypatch.setenv("AGENTTEAMS_WORKER_NAME", worker_name)
    return S3Bootstrap(client=FakeMinio(objects), bucket="bkt", prefix="")


class TestBootstrapManagedRuntime:
    """runtime=opencode workers have runtime.yaml but no openclaw.json."""

    def test_runtime_yaml_alone_bootstraps(self, monkeypatch):
        objects = {"agents/w1/runtime/runtime.yaml": "member:\n  runtime: opencode\n"}
        boot = _bootstrap(objects, monkeypatch)
        cfg = boot.load(retries=1)
        assert cfg is not None
        assert cfg.runtime_yaml.startswith("member:")
        assert cfg.openclaw == {}
        # matrix token falls back to env (AGENTTEAMS_WORKER_MATRIX_TOKEN)
        assert cfg.matrix_access_token == ""

    def test_openclaw_json_still_carries_bridge_section(self, monkeypatch):
        openclaw = {"channels": {"matrix": {"accessToken": "tok"}}, "bridge": {"runtime": {"helperUrl": "http://h:4097"}}}
        objects = {
            "agents/w1/openclaw.json": json.dumps(openclaw),
            "agents/w1/runtime/runtime.yaml": "member:\n  runtime: qwenpaw\n",
        }
        boot = _bootstrap(objects, monkeypatch)
        cfg = boot.load(retries=1)
        assert cfg is not None
        assert cfg.matrix_access_token == "tok"
        assert cfg.runtime_helper_url == "http://h:4097"
        assert cfg.runtime_yaml.startswith("member:")

    def test_neither_file_present_returns_none(self, monkeypatch):
        boot = _bootstrap({}, monkeypatch)
        assert boot.load(retries=1) is None

    def test_inline_config_soul_extracted(self, monkeypatch):
        # spec.soul / spec.identity land in desired.inlineConfig for managed
        # runtimes — the bootstrap must surface them as soul_md / profile_md
        # so the v2.4 generator gets its --soul-file / --profile-file inputs.
        runtime_yaml = (
            "member:\n"
            "  runtime: opencode\n"
            "desired:\n"
            "  inlineConfig:\n"
            "    soul: 你是验收 worker\n"
            "    identity: 资深后端工程师\n"
        )
        boot = _bootstrap({"agents/w1/runtime/runtime.yaml": runtime_yaml}, monkeypatch)
        cfg = boot.load(retries=1)
        assert cfg is not None
        assert cfg.soul_md == "你是验收 worker"
        assert cfg.profile_md == "资深后端工程师"

    def test_inline_config_absent_yields_empty_persona(self, monkeypatch):
        boot = _bootstrap({"agents/w1/runtime/runtime.yaml": "member:\n  runtime: opencode\n"}, monkeypatch)
        cfg = boot.load(retries=1)
        assert cfg is not None
        assert cfg.soul_md == ""
        assert cfg.profile_md == ""

    def test_inline_config_unparsable_degrades_to_empty(self, monkeypatch):
        boot = _bootstrap({"agents/w1/runtime/runtime.yaml": "::: not yaml ["}, monkeypatch)
        cfg = boot.load(retries=1)
        assert cfg is not None
        assert cfg.soul_md == ""
        assert cfg.profile_md == ""


class TestEnvOverrides:
    def test_env_routes_to_opencode(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_RUNTIME_ADAPTER", "opencode")
        monkeypatch.setenv("BRIDGE_RUNTIME_BASE_URL", "http://opencode-svc:4096")
        monkeypatch.setenv("BRIDGE_RUNTIME_HELPER_URL", "http://sandbox-svc:4097")
        monkeypatch.delenv("AGENTTEAMS_FS_ENDPOINT", raising=False)
        app = BridgeApp()
        app.start()
        assert app.config.runtime.adapter == "opencode"
        assert app.config.runtime.base_url == "http://opencode-svc:4096"
        assert app.config.runtime.helper_url == "http://sandbox-svc:4097"

    def test_defaults_without_env(self, monkeypatch):
        for key in ("BRIDGE_RUNTIME_ADAPTER", "BRIDGE_RUNTIME_BASE_URL", "BRIDGE_RUNTIME_HELPER_URL"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("AGENTTEAMS_FS_ENDPOINT", raising=False)
        app = BridgeApp()
        app.start()
        assert app.config.runtime.adapter == "cimicode"
        assert app.config.runtime.helper_url == ""
