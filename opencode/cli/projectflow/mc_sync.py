"""mc/MinIO sync backend for the taskflow CLI.

Vendored from ``AgentTeams/copaw/src/copaw_worker/sync.py`` (dev-v1.2.2),
shared-path subset only: the pieces the copaw ``taskflow`` tool relies on —
alias management (k8s / cloud-STS / static), shared-path resolution, and
pull/push/stat/list via the ``mc`` CLI.

Removed from the original (copaw worker-runtime concerns that stay with the
sandbox bootstrap, not this task CLI): mirror_all startup restore, pull_all
config refresh, push_local watermark push, push_loop/sync_loop background
tasks, openclaw.json merging, skills mirroring, health-state reporting.

Semantics kept line-equivalent where retained:
  - remote layout: ``alias/{bucket}/teams/{team}/shared/`` for team workers
    (team resolved from the controller via ``agt get workers``), else
    ``alias/{bucket}/shared/``;
  - directory pull/push = ``mc mirror --overwrite`` (push appends one
    ``--exclude`` pair per pattern);
  - verify = ``mc stat``;
  - k8s mode never sets an alias (mc-wrapper owns credentials), cloud mode
    refreshes STS into MC_HOST_<alias>, local mode sets a static alias once.

The task-level guardrail from copaw's taskflow tool lives here as the
default: pushing a task dir always excludes ``spec.md`` and ``base/`` —
those are Leader-owned and must never be overwritten by a worker push.

T3 (agentteams-sync CLI) reuses ``McFileSync`` directly as the filesync
pull/push/stat engine.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class McSyncError(ValueError):
    """Expected user-facing mc/filesync error.

    Self-contained on purpose: mc_sync.py deploys alongside EITHER
    taskflow.py OR agentteams_sync.py, so it must not import either.
    """


# ---------------------------------------------------------------------------
# mc plumbing (vendored)
# ---------------------------------------------------------------------------

def _storage_alias() -> str:
    explicit = os.environ.get("AGENTTEAMS_STORAGE_ALIAS")
    if explicit:
        return explicit
    prefix = os.environ.get("AGENTTEAMS_STORAGE_PREFIX") or ""
    if "/" in prefix:
        return prefix.split("/", 1)[0]
    return "agentteams"


# mc alias name used for this worker session
_MC_ALIAS = _storage_alias()


def _redact_url_userinfo(value: str) -> str:
    if "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    if "@" not in rest:
        return value
    return f"{scheme}://<redacted>@{rest.split('@', 1)[1]}"


def _redacted_mc_command(cmd: list[str]) -> list[str]:
    redacted = [_redact_url_userinfo(part) for part in cmd]
    args = redacted[1:]
    if len(args) >= 6 and args[0] == "alias" and args[1] == "set":
        redacted[5] = "<redacted-access-key>"
        redacted[6] = "<redacted-secret-key>"
    return redacted


def _preview_text(value: str | None, limit: int = 2000) -> str:
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"


def _looks_like_remote_directory_error(exc: subprocess.CalledProcessError) -> bool:
    """Return True when mc cp failed because the remote path is a prefix."""
    stderr = str(exc.stderr or "")
    stdout = str(exc.stdout or "")
    text = f"{stderr}\n{stdout}"
    return "--recursive flag is required" in text


def _looks_like_missing_object_error(stderr: str | None) -> bool:
    text = stderr or ""
    return "Object does not exist" in text or "The specified key does not exist" in text


def _team_storage_name_from_worker_team(bucket: str, team_ref: str) -> str:
    """Derive the temporary storage team name from a WorkerResponse team ref."""
    team_name = team_ref.strip()
    bucket_name = (bucket or "").strip()
    prefixes = [bucket_name]
    if bucket_name.startswith("agentteams-"):
        prefixes.append(bucket_name.removeprefix("agentteams-"))

    for prefix in prefixes:
        if prefix and team_name.startswith(f"{prefix}-"):
            return team_name[len(prefix) + 1 :]
    return team_name


def _mc(
    *args: str,
    check: bool = True,
    warn_on_error: bool = True,
    log_output: bool = True,
) -> subprocess.CompletedProcess:
    """Run an mc command and return the result."""
    mc_bin = shutil.which("mc")
    if not mc_bin:
        raise RuntimeError("mc binary not found on PATH. Please install mc first.")
    cmd = [mc_bin, *args]
    redacted_cmd = _redacted_mc_command(cmd)
    logger.info("mc cmd: %s", " ".join(redacted_cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=check)
    except subprocess.CalledProcessError as exc:
        exc.cmd = redacted_cmd
        log = logger.warning if warn_on_error else logger.debug
        log(
            "mc command failed returncode=%s cmd=%s stdout=%r stderr=%r",
            exc.returncode,
            " ".join(redacted_cmd),
            _preview_text(exc.stdout),
            _preview_text(exc.stderr),
        )
        raise
    if log_output:
        logger.info("mc stdout (%d chars): %r", len(result.stdout), result.stdout[:200])
        if result.stderr:
            logger.info("mc stderr: %r", result.stderr[:200])
    return result


# ---------------------------------------------------------------------------
# FileSync, shared-path subset (vendored)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SharedPath:
    """Resolved local and remote paths for a shared file operation."""

    kind: str
    subpath: str
    local: Path
    remote: str


class McFileSync:
    """MinIO shared-path sync using the mc CLI (copaw FileSync subset)."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        worker_name: str,
        worker_cr_name: Optional[str] = None,
        secure: bool = False,
        local_dir: Optional[Path] = None,
        shared_dir: Optional[Path] = None,
        global_shared_dir: Optional[Path] = None,
    ) -> None:
        self.endpoint = (endpoint or "").rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.worker_name = worker_name
        self.worker_cr_name = worker_cr_name or worker_name
        self._secure = secure
        self.local_dir = Path(local_dir) if local_dir is not None else Path.cwd()
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.shared_dir = shared_dir or self.local_dir / "shared"
        self.global_shared_dir = global_shared_dir or self.local_dir / "global-shared"
        self._alias_set = False
        runtime = os.environ.get("AGENTTEAMS_RUNTIME")
        self._cloud_mode = runtime == "aliyun"
        self._k8s_mode = runtime == "k8s"
        self._worker_info: dict[str, Any] | None = None

    # -- mc alias management ---------------------------------------------

    def _refresh_cloud_credentials(self) -> None:
        """Refresh STS credentials by calling the shared shell function.

        The shell function is lazy: it checks /tmp/mc-oss-credentials.env
        and only hits the STS endpoint when the token is within 10 minutes
        of expiring.  Cheap no-op when credentials are still valid.
        """
        result = subprocess.run(
            ["bash", "-c",
             "source /opt/agentteams/scripts/lib/agentteams-env.sh && "
             "ensure_mc_credentials && "
             f"_mc_host_var=MC_HOST_{_MC_ALIAS} && "
             "printf '%s' \"${!_mc_host_var}\""],
            capture_output=True, text=True, check=True,
        )
        mc_host = result.stdout.strip()
        if mc_host:
            os.environ[f"MC_HOST_{_MC_ALIAS}"] = mc_host
        else:
            logger.warning("ensure_mc_credentials returned empty MC_HOST_%s", _MC_ALIAS)

    def _ensure_alias(self) -> None:
        """Set up mc alias, refreshing STS credentials in cloud mode.

        Cloud mode (RRSA/STS): refresh credentials before every mc batch
        via the shared shell function (lazy, no-op when token is valid).
        Local mode: set mc alias once with static credentials.
        """
        if self._k8s_mode:
            logger.info("_ensure_alias: k8s mode, skipping mc alias set (mc-wrapper handles credentials)")
            self._alias_set = True
            return
        if self._cloud_mode:
            logger.info("_ensure_alias: credential path=sts, refreshing MC_HOST_%s", _MC_ALIAS)
            self._refresh_cloud_credentials()
            self._alias_set = True
            return
        if self._alias_set:
            logger.info("_ensure_alias: credential path=static, alias already set")
            return
        # Local mode: static credentials, set alias once
        if self.endpoint.startswith("http"):
            url = self.endpoint
        else:
            scheme = "https" if self._secure else "http"
            url = f"{scheme}://{self.endpoint}"
        _mc("alias", "set", _MC_ALIAS, url, self.access_key, self.secret_key)
        self._alias_set = True
        logger.info("_ensure_alias: static alias ready alias_set=%s", self._alias_set)

    # -- controller metadata ----------------------------------------------

    def _get_worker_info(self) -> dict[str, Any]:
        """Return authoritative worker metadata from the AgentTeams controller."""
        if self._worker_info is not None:
            return self._worker_info

        agentteams_bin = shutil.which("agt")
        if not agentteams_bin:
            raise RuntimeError("AgentTeams CLI not found; cannot resolve worker storage scope")

        try:
            result = subprocess.run(
                [agentteams_bin, "get", "workers", self.worker_cr_name, "-o", "json"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            worker = json.loads(result.stdout)
        except Exception as exc:
            raise RuntimeError(
                f"failed to query worker metadata for {self.worker_cr_name}: {exc}",
            ) from exc

        if not isinstance(worker, dict):
            raise RuntimeError(f"invalid worker metadata for {self.worker_cr_name}")
        self._worker_info = worker
        return worker

    def _get_team_id(self) -> Optional[str]:
        """Resolve the storage team name.

        opencode contract: ``AGENTTEAMS_TEAM`` is injected by the orchestrator
        (storage team name, bucket prefix already stripped) so the sandbox
        needs no controller access. copaw compat: when unset, query the
        controller via ``agt`` like the original FileSync.
        """
        from_env = (os.environ.get("AGENTTEAMS_TEAM") or "").strip()
        if from_env:
            return from_env
        worker = self._get_worker_info()
        team_ref = worker.get("team")
        if not isinstance(team_ref, str) or not team_ref.strip():
            return None
        return _team_storage_name_from_worker_team(self.bucket, team_ref)

    def _get_shared_remote(self) -> str:
        """Return the MinIO remote path for the shared/ directory.

        Team members sync from teams/{team}/shared/ instead of global shared/.
        Non-team workers sync from global shared/.
        """
        team_id = self._get_team_id()
        if team_id:
            return f"{_MC_ALIAS}/{self.bucket}/teams/{team_id}/shared/"
        return f"{_MC_ALIAS}/{self.bucket}/shared/"

    def _get_global_shared_remote(self) -> str:
        """Return the MinIO remote path for global-shared/ directory."""
        return f"{_MC_ALIAS}/{self.bucket}/shared/"

    # -- path resolution ---------------------------------------------------

    def resolve_shared_path(self, path: str) -> SharedPath:
        """Resolve a user-facing shared path to local and remote paths."""
        raw = (path or "").strip()
        if not raw:
            raise ValueError("path is required")
        if raw.startswith("/") or "\\" in raw:
            raise ValueError("path must be a relative shared path")

        normalized = raw.strip("/")
        parts = Path(normalized).parts
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise ValueError("path must not contain empty, '.', or '..' segments")

        if parts[0] == "shared":
            subpath = "/".join(parts[1:])
            local = self.shared_dir.joinpath(*parts[1:]) if len(parts) > 1 else self.shared_dir
            remote = self._get_shared_remote()
            if subpath:
                remote = f"{remote}{subpath}"
                if raw.endswith("/"):
                    remote += "/"
            return SharedPath("shared", subpath, local, remote)

        if parts[0] == "global-shared":
            subpath = "/".join(parts[1:])
            local = (
                self.global_shared_dir.joinpath(*parts[1:])
                if len(parts) > 1
                else self.global_shared_dir
            )
            remote = self._get_global_shared_remote()
            if subpath:
                remote = f"{remote}{subpath}"
                if raw.endswith("/"):
                    remote += "/"
            return SharedPath("global-shared", subpath, local, remote)

        raise ValueError("path must start with shared/ or global-shared/")

    # -- public API ---------------------------------------------------------

    def pull_shared_path(self, path: str) -> SharedPath:
        """Pull a shared path from MinIO into the local workspace."""
        resolved = self.resolve_shared_path(path)
        self._ensure_alias()
        if (path or "").strip().endswith("/"):
            resolved.local.mkdir(parents=True, exist_ok=True)
            _mc("mirror", resolved.remote, str(resolved.local) + "/", "--overwrite", check=True)
            return resolved

        resolved.local.parent.mkdir(parents=True, exist_ok=True)
        try:
            _mc("cp", resolved.remote, str(resolved.local), check=True)
        except subprocess.CalledProcessError as exc:
            if not _looks_like_remote_directory_error(exc):
                raise
            remote = resolved.remote if resolved.remote.endswith("/") else f"{resolved.remote}/"
            resolved.local.mkdir(parents=True, exist_ok=True)
            _mc("mirror", remote, str(resolved.local) + "/", "--overwrite", check=True)
        return resolved

    def push_shared_path(
        self,
        path: str,
        *,
        exclude: Optional[list[str]] = None,
    ) -> SharedPath:
        """Push a local shared path to MinIO."""
        resolved = self.resolve_shared_path(path)
        if resolved.kind == "global-shared":
            raise ValueError("global-shared/ is read-only")
        if not resolved.local.exists():
            raise FileNotFoundError(f"local path does not exist: {resolved.local}")

        self._ensure_alias()
        if resolved.local.is_dir():
            remote = resolved.remote if resolved.remote.endswith("/") else f"{resolved.remote}/"
            args = ["mirror", str(resolved.local) + "/", remote, "--overwrite"]
            for item in exclude or []:
                args.extend(["--exclude", item])
            _mc(*args, check=True)
        else:
            _mc("cp", str(resolved.local), resolved.remote, check=True)
        return resolved

    def stat_shared_path(self, path: str) -> SharedPath:
        """Check that a shared path exists in MinIO."""
        resolved = self.resolve_shared_path(path)
        self._ensure_alias()
        _mc("stat", resolved.remote, check=True)
        return resolved

    def list_shared_path(self, path: str) -> tuple[SharedPath, list[str]]:
        """List a shared path in MinIO."""
        resolved = self.resolve_shared_path(path)
        self._ensure_alias()
        result = _mc("ls", "--recursive", resolved.remote, check=True)
        entries = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return resolved, entries


# ---------------------------------------------------------------------------
# taskflow SyncBackend adapter
# ---------------------------------------------------------------------------

def filesync_from_env(root: Path) -> McFileSync:
    """Build McFileSync from the copaw filesync env contract (§4.1).

    Mirrors copaw's ``create_sync()`` (hooks/tools/filesync.py) with one
    relaxation: the static credential trio is only required in local mode —
    k8s (mc-wrapper) and cloud (STS) runtimes carry credentials in
    MC_HOST_<alias> instead of the endpoint/key variables.
    """
    worker_name = (os.environ.get("AGENTTEAMS_WORKER_NAME") or "").strip()
    if not worker_name:
        raise McSyncError(
            "AGENTTEAMS_WORKER_NAME is required for mc sync "
            "(see contract §4.1 env variables)"
        )
    endpoint = os.environ.get("AGENTTEAMS_FS_ENDPOINT") or ""
    access_key = os.environ.get("AGENTTEAMS_FS_ACCESS_KEY") or ""
    secret_key = os.environ.get("AGENTTEAMS_FS_SECRET_KEY") or ""
    bucket = os.environ.get("AGENTTEAMS_FS_BUCKET") or "agentteams-storage"
    worker_cr_name = os.environ.get("AGENTTEAMS_WORKER_CR_NAME") or worker_name

    runtime = os.environ.get("AGENTTEAMS_RUNTIME")
    if runtime not in ("k8s", "aliyun") and not (endpoint and access_key and secret_key):
        raise McSyncError(
            "AGENTTEAMS_FS_ENDPOINT / ACCESS_KEY / SECRET_KEY are required "
            "for mc sync in local mode (runtime="
            f"{runtime or 'local'})"
        )
    return McFileSync(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        worker_name=worker_name,
        worker_cr_name=worker_cr_name,
        secure=endpoint.startswith("https://"),
        local_dir=root,
        shared_dir=root / "shared",
        global_shared_dir=root / "global-shared",
    )


# copaw's taskflow tool always pushes a task dir with these excludes:
# spec.md and base/ are Leader-owned; a worker push must never overwrite them.
TASK_PUSH_EXCLUDES = ["spec.md", "base/"]


class McSyncBackend:
    """SyncBackend over McFileSync, mirroring copaw's taskflow tool calls:

      ack/check  pull_shared_path("shared/tasks/<id>/")
      ack/submit push_shared_path("shared/tasks/<id>/", exclude=TASK_PUSH_EXCLUDES)
      submit     stat_shared_path("shared/tasks/<id>/result.md")  (verify)
    """

    def __init__(self, root: Path | str, fs: McFileSync | None = None) -> None:
        self.root = Path(root)
        self._fs = fs if fs is not None else self._fs_from_env(self.root)

    @classmethod
    def _fs_from_env(cls, root: Path) -> McFileSync:
        """Build McFileSync from the copaw filesync env contract (§4.1)."""
        return filesync_from_env(root)

    # -- SyncBackend protocol ----------------------------------------------

    def pull_task(self, task_id: str) -> None:
        try:
            self._fs.pull_shared_path(f"shared/tasks/{task_id}/")
        except subprocess.CalledProcessError as exc:
            if _looks_like_missing_object_error(
                getattr(exc, "stderr", None) or getattr(exc, "stdout", None)
            ):
                raise McSyncError(
                    f"task {task_id} not found in shared storage"
                ) from exc
            raise

    def push_task(self, task_id: str, exclude: list[str] | None = None) -> None:
        self._fs.push_shared_path(
            f"shared/tasks/{task_id}/",
            exclude=TASK_PUSH_EXCLUDES if exclude is None else exclude,
        )

    def verify_result(self, task_id: str) -> None:
        self._fs.stat_shared_path(f"shared/tasks/{task_id}/result.md")

    def describe(self) -> str:
        remote = "<unresolved>"
        try:
            remote = self._fs._get_shared_remote()
        except Exception:
            pass
        return f"mc: alias={_MC_ALIAS} bucket={self._fs.bucket} remote={remote}"
