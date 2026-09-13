from __future__ import annotations

import json
from pathlib import Path

from cimicode_bridge.app import BridgeApp
from cimicode_bridge.bootstrap import S3Bootstrap, WorkerBootstrapConfig
from cimicode_bridge.config import load_config
from cimicode_bridge.runtime.cimicode_pod_adapter import CimicodePodAdapter
from cimicode_bridge.runtime.cimicode_stateless_adapter import CimicodeStatelessAdapter


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
    """runtime=worker-bridge workers have runtime.yaml but no openclaw.json."""

    def test_runtime_yaml_alone_bootstraps(self, monkeypatch):
        objects = {"agents/w1/runtime/runtime.yaml": "member:\n  runtime: worker-bridge\n"}
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

    def test_runtime_yaml_retry_wins_startup_race(self, monkeypatch):
        """The controller pushes runtime/runtime.yaml after the bridge pod
        started: load must retry until the object appears instead of giving
        up after a single read (which wedged the worker with an incomplete
        bootstrap and made every later turn fail)."""
        reads = {"runtime_yaml": 0}

        class LateMinio(FakeMinio):
            def get_object(self, bucket: str, key: str):
                if key.endswith("runtime.yaml"):
                    reads["runtime_yaml"] += 1
                    if reads["runtime_yaml"] < 3:
                        raise KeyError(key)
                return super().get_object(bucket, key)

        sleeps: list[float] = []
        monkeypatch.setattr("cimicode_bridge.bootstrap.time.sleep", sleeps.append)
        monkeypatch.setenv("AGENTTEAMS_WORKER_NAME", "w1")
        boot = S3Bootstrap(
            client=LateMinio({"agents/w1/runtime/runtime.yaml": "member:\n  runtime: worker-bridge\n"}),
            bucket="bkt",
            prefix="",
        )
        cfg = boot.load(retries=5, retry_interval_seconds=0)
        assert cfg is not None
        assert cfg.runtime_yaml.startswith("member:")
        assert reads["runtime_yaml"] == 3   # two misses, then the object
        assert len(sleeps) == 2             # slept between attempts, not after success

    def test_inline_config_soul_extracted(self, monkeypatch):
        # spec.soul / spec.identity land in desired.inlineConfig for managed
        # runtimes — the bootstrap must surface them as soul_md / profile_md
        # so the v2.4 generator gets its --soul-file / --profile-file inputs.
        runtime_yaml = (
            "member:\n"
            "  runtime: worker-bridge\n"
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
        boot = _bootstrap({"agents/w1/runtime/runtime.yaml": "member:\n  runtime: worker-bridge\n"}, monkeypatch)
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


class TestPublish:
    def test_publish_writes_agent_md_object(self, monkeypatch):
        class PutCapture(FakeMinio):
            def __init__(self, objects):
                super().__init__(objects)
                self.puts = []

            def put_object(self, bucket, key, data, length, content_type=None):
                self.puts.append((bucket, key, data.read(), content_type))

        store = PutCapture({})
        monkeypatch.setenv("AGENTTEAMS_WORKER_NAME", "w1")
        boot = S3Bootstrap(client=store, bucket="bkt", prefix="")
        key = boot.publish("agent-md/latest.md", "# agent md content")
        assert key == "agents/w1/agent-md/latest.md"
        assert store.puts == [
            ("bkt", "agents/w1/agent-md/latest.md", b"# agent md content", "text/markdown")
        ]

    def test_publish_failure_returns_none(self, monkeypatch):
        class BrokenMinio(FakeMinio):
            def put_object(self, *args, **kwargs):
                raise RuntimeError("s3 down")

        monkeypatch.setenv("AGENTTEAMS_WORKER_NAME", "w1")
        boot = S3Bootstrap(client=BrokenMinio({}), bucket="bkt", prefix="")
        assert boot.publish("agent-md/latest.md", "x") is None


class TestEnvOverrides:
    def test_env_routes_to_cimicode_pod(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_RUNTIME_ADAPTER", "cimicode-pod")
        monkeypatch.setenv("BRIDGE_RUNTIME_BASE_URL", "http://cimicode-svc:8080")
        monkeypatch.delenv("AGENTTEAMS_FS_ENDPOINT", raising=False)
        app = BridgeApp()
        app.start()
        assert app.config.runtime.adapter == "cimicode-pod"
        assert app.config.runtime.base_url == "http://cimicode-svc:8080"
        assert isinstance(app.runtime_client, CimicodePodAdapter)

    def test_defaults_without_env_undetermined(self, monkeypatch):
        """无 env、无 S3：adapter 未定、client 不建（fail-loud，
        绝不静默回落写死的 mock 地址）。"""
        for key in ("BRIDGE_RUNTIME_ADAPTER", "BRIDGE_RUNTIME_BASE_URL"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("AGENTTEAMS_FS_ENDPOINT", raising=False)
        app = BridgeApp()
        app.start()
        assert app.config.runtime.adapter == ""
        assert app.config.runtime.base_url == ""
        assert app.runtime_client is None


class TestAdapterResolution:
    """判定顺序（§3.3）：显式 env > runtime.yaml bridge.adapterMode > 未定。"""

    BRIDGE_YAML = (
        "member:\n"
        "  runtime: worker-bridge\n"
        "bridge:\n"
        "  adapterMode: cimicode-stateless\n"
        "  baseUrl: http://cimicode.internal:8080\n"
        "  sessionId: sess-1\n"
        "  sandboxId: sbx-1\n"
        "  templateId: tpl-1\n"
    )

    def _app_with_files(self, runtime_yaml: str) -> BridgeApp:
        app = BridgeApp()
        app.config = load_config("config/does-not-exist.yaml")
        app.worker_files = WorkerBootstrapConfig(openclaw={}, runtime_yaml=runtime_yaml)
        return app

    def test_bridge_section_mode_resolves_stateless(self, monkeypatch):
        monkeypatch.delenv("BRIDGE_RUNTIME_ADAPTER", raising=False)
        app = self._app_with_files(self.BRIDGE_YAML)
        assert app._resolve_adapter_mode() == "cimicode-stateless"
        # bridge 段绑定先应用（base_url 等），再构建 adapter
        app._apply_bridge_section(app.worker_files)
        client = app._build_runtime_client()
        assert isinstance(client, CimicodeStatelessAdapter)
        assert app.config.runtime.base_url == "http://cimicode.internal:8080"
        assert app.config.runtime.session_id == "sess-1"
        assert app.config.runtime.sandbox_id == "sbx-1"
        assert app.config.runtime.template_id == "tpl-1"

    def test_explicit_env_wins_over_bridge_section(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_RUNTIME_ADAPTER", "cimicode-pod")
        app = self._app_with_files(self.BRIDGE_YAML)
        assert app._resolve_adapter_mode() == "cimicode-pod"

    def test_undetermined_when_both_missing(self, monkeypatch):
        monkeypatch.delenv("BRIDGE_RUNTIME_ADAPTER", raising=False)
        app = self._app_with_files("member:\n  runtime: worker-bridge\n")
        assert app._resolve_adapter_mode() == ""
        assert app._build_runtime_client() is None

    def test_base_url_missing_leaves_client_unbuilt(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_RUNTIME_ADAPTER", "cimicode-pod")
        monkeypatch.delenv("BRIDGE_RUNTIME_BASE_URL", raising=False)
        app = self._app_with_files("member:\n  runtime: worker-bridge\n")
        # adapter 有（env）但 base_url 无任何来源 → 不建 client
        assert app._build_runtime_client() is None

    def test_managed_runtime_type(self):
        from cimicode_bridge.bootstrap import managed_runtime_type

        assert managed_runtime_type("member:\n  runtime: worker-bridge\n") == "worker-bridge"
        assert managed_runtime_type("member: [broken") == ""
        assert managed_runtime_type("") == ""


class TestBridgeSectionParsing:
    """runtime.yaml 顶层 bridge 段解析（worker-bridge 统一绑定通道）。"""

    def test_bridge_section_parsed_and_normalized(self, monkeypatch):
        boot = _bootstrap({"agents/w1/runtime/runtime.yaml": TestAdapterResolution.BRIDGE_YAML}, monkeypatch)
        cfg = boot.load(retries=1)
        assert cfg is not None
        assert cfg.bridge_adapter_mode == "cimicode-stateless"
        assert cfg.runtime_bridge_section == {
            "adapter_mode": "cimicode-stateless",
            "base_url": "http://cimicode.internal:8080",
            "session_id": "sess-1",
            "sandbox_id": "sbx-1",
            "template_id": "tpl-1",
        }

    def test_snake_case_keys_accepted(self, monkeypatch):
        runtime_yaml = (
            "bridge:\n"
            "  adapter_mode: cimicode-pod\n"
            "  base_url: http://cimicode:8080\n"
        )
        boot = _bootstrap({"agents/w1/runtime/runtime.yaml": runtime_yaml}, monkeypatch)
        cfg = boot.load(retries=1)
        assert cfg.bridge_adapter_mode == "cimicode-pod"

    def test_bridge_section_absent(self, monkeypatch):
        boot = _bootstrap({"agents/w1/runtime/runtime.yaml": "member:\n  runtime: worker-bridge\n"}, monkeypatch)
        cfg = boot.load(retries=1)
        assert cfg.runtime_bridge_section == {}
        assert cfg.bridge_adapter_mode == ""
