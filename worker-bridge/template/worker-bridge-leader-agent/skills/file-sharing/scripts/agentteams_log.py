#!/usr/bin/env python3
"""agentteams_log — unified JSONL logging for the AgentTeams bash CLIs.

Every tool (taskflow / agentteams-sync / projectflow / convert_agent_md)
logs through this module into ONE file, so a whole delegation can be
traced end-to-end: pick a task id (or run_id) and every line about it —
command start, pulls, pushes, mc traffic, state transitions, verify,
rollback, command end — is in one place. Each line is one JSON object:

  {"ts": "2026-09-02T03:12:44.123Z", "level": "INFO",
   "tool": "taskflow", "worker": "bob", "run_id": "a1b2c3d4",
   "logger": "taskflow", "event": "status_change",
   "task": "t-101", "from": "assigned", "to": "in_progress"}

Sinks (both write the same JSON lines):

  * file — resolution order:
      $AGENTTEAMS_LOG_FILE            explicit path ("none" disables)
      $AGENTTEAMS_LOG_DIR/agentteams.log
      $AGENTTEAMS_FS_ROOT/logs/agentteams.log   (FS_ROOT defaults to
                                                /root/agentteams-fs)
    The logs/ directory sits NEXT TO shared/, not inside it, so logs are
    never pushed to MinIO. If the path is unwritable the tool keeps
    working (stderr only) — logging must never break a command.
  * stderr — always on, so `kubectl logs` / the bridge's process capture
    see the same stream. AGENTTEAMS_LOG_STDERR=0 silences it.

Level: $AGENTTEAMS_LOG_LEVEL (default INFO; DEBUG also picks up the mc
command snippets the vendored mc_sync module already logs).

Context stamped on every record (also for records from other loggers,
e.g. mc_sync, via a root-logger filter):
  tool    the CLI name passed to setup()
  worker  $AGENTTEAMS_WORKER_NAME
  run_id  fresh short id per process invocation — correlate one command
          with its mc traffic and with the bridge session log

Safety: argv values are truncated by clip() (default 200 chars) so huge
--spec/--tasks-json payloads don't flood the log; mc_sync already
redacts credentials in what it logs. Never log file contents wholesale.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time

_CONTEXT: dict = {}
_CLIP_LIMIT = 200


def clip(value, limit=_CLIP_LIMIT):
    """Truncate long argv/payload values for logging."""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...(+{len(text) - limit} chars)"


def clip_argv(argv):
    return [clip(a) for a in argv]


def resolve_log_file(environ=None):
    env = environ if environ is not None else os.environ
    explicit = env.get("AGENTTEAMS_LOG_FILE", "")
    if explicit == "none":
        return None
    if explicit:
        return explicit
    log_dir = env.get("AGENTTEAMS_LOG_DIR", "")
    if not log_dir:
        root = env.get("AGENTTEAMS_FS_ROOT", "/root/agentteams-fs")
        log_dir = os.path.join(root, "logs")
    return os.path.join(log_dir, "agentteams.log")


class _ContextFilter(logging.Filter):
    """Stamp tool/worker/run_id on every record crossing the root logger."""

    def filter(self, record):
        record.tool = _CONTEXT.get("tool", "-")
        record.worker = _CONTEXT.get("worker", "-")
        record.run_id = _CONTEXT.get("run_id", "-")
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "tool": getattr(record, "tool", "-"),
            "worker": getattr(record, "worker", "-"),
            "run_id": getattr(record, "run_id", "-"),
            "logger": record.name,
        }
        if hasattr(record, "event"):
            entry["event"] = record.event
            for key, value in getattr(record, "fields", {}).items():
                entry[key] = value
        else:
            entry["msg"] = record.getMessage()
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup(tool, argv=None):
    """Configure the root logger once per process; return the tool logger.

    Idempotent: a second call (e.g. from a library entry point) just
    returns the existing logger. Logs `cmd_start` with the (clipped)
    argv so every subsequent line can be tied back to the invocation."""
    if _CONTEXT:
        return logging.getLogger(_CONTEXT["tool"])
    _CONTEXT.update(
        tool=tool,
        worker=os.environ.get("AGENTTEAMS_WORKER_NAME", "-"),
        run_id=secrets.token_hex(4),
    )
    root = logging.getLogger()
    root.setLevel(os.environ.get("AGENTTEAMS_LOG_LEVEL", "INFO").upper())
    context = _ContextFilter()
    formatter = JsonFormatter()

    stderr_on = os.environ.get("AGENTTEAMS_LOG_STDERR", "1") != "0"
    path = resolve_log_file()
    if path:
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(formatter)
            handler.addFilter(context)
            root.addHandler(handler)
        except OSError as exc:
            # Degraded mode (stderr only). The warning itself goes to stderr,
            # so it is suppressed when stderr is explicitly silenced —
            # otherwise dev boxes without write access to the default
            # /root/agentteams-fs path would pollute every CLI's stderr
            # contract (e.g. convert_agent_md's byte-clean output).
            if stderr_on:
                print(f"agentteams_log: file sink unavailable ({exc}); "
                      f"stderr only", file=sys.stderr)

    if stderr_on:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        handler.addFilter(context)
        root.addHandler(handler)

    log = logging.getLogger(tool)
    log_event(log, "cmd_start", argv=clip_argv(sys.argv[1:] if argv is None else argv))
    return log


def log_event(logger, event, level=logging.INFO, **fields):
    """Emit one JSONL line: log_event(log, "status_change", task=t, from_=..).

    `from`/`to` are python keywords territory only for `from`; callers
    pass from_=... and it lands as "from" in the JSON."""
    if "from_" in fields:
        fields["from"] = fields.pop("from_")
    logger.log(level, event, extra={"event": event, "fields": fields})


def log_cmd_end(logger, exit_code, started, **fields):
    """Emit the closing line with exit code and wall-clock duration."""
    log_event(logger, "cmd_end", exit=exit_code,
              duration_ms=int((time.monotonic() - started) * 1000), **fields)
