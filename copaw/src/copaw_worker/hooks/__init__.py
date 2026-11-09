"""Runtime hooks for adapting upstream CoPaw behavior to AgentTeams."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)

_TOOL_HOOK_INSTALLED = False
_MESSAGE_FILTER_HOOK_INSTALLED = False


def _builtin_tool_config(agent: Any, name: str) -> Any | None:
    try:
        builtin_tools = agent._agent_config.tools.builtin_tools
        return builtin_tools.get(name)
    except Exception:
        return None


def _builtin_tool_enabled(agent: Any, name: str) -> bool:
    config = _builtin_tool_config(agent, name)
    return bool(getattr(config, "enabled", True))


def _builtin_tool_async_execution(agent: Any, name: str) -> bool:
    config = _builtin_tool_config(agent, name)
    return bool(getattr(config, "async_execution", False))


def _register_tool_function(toolkit: Any, func: Any, **kwargs: Any) -> None:
    try:
        toolkit.register_tool_function(func, **kwargs)
    except TypeError:
        fallback = dict(kwargs)
        fallback.pop("async_execution", None)
        toolkit.register_tool_function(func, **fallback)


def _collaboration_mode() -> str:
    """Read team.collaboration from the member runtime.yaml ("" = taskflow).

    The controller projects Team.spec.collaboration into
    agents/<w>/runtime/runtime.yaml; the entrypoint mirrors it before the
    app starts, so it is readable at toolkit-creation time. Standalone
    pip-installed workers have no runtime.yaml and keep the default
    taskflow toolset. Any read error degrades to the default, never breaks
    tool registration.
    """
    try:
        import yaml

        from copaw_worker.hooks.tools.status_report import _working_dir

        path = _working_dir().parent / "runtime" / "runtime.yaml"
        if not path.exists():
            return ""
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        team = data.get("team") if isinstance(data, dict) else None
        if isinstance(team, dict):
            return str(team.get("collaboration") or "").strip().lower()
    except Exception:
        logger.debug("Unable to read collaboration mode from runtime.yaml", exc_info=True)
    return ""


def _existing_tool_schema(toolkit: Any, name: str) -> dict[str, Any] | None:
    tool = getattr(toolkit, "tools", {}).get(name)
    schema = getattr(tool, "json_schema", None)
    return deepcopy(schema) if isinstance(schema, dict) else None


def install_message_filter_hooks() -> None:
    """Leave Matrix reply filtering to final send/tool boundaries."""
    global _MESSAGE_FILTER_HOOK_INSTALLED
    if _MESSAGE_FILTER_HOOK_INSTALLED:
        return

    _MESSAGE_FILTER_HOOK_INSTALLED = True
    logger.info("AgentTeams CoPaw query-handler message filter hook is disabled")


def install_tool_hooks() -> None:
    """Install AgentTeams-owned CoPaw tool hooks.

    CoPaw creates a temporary CoPawAgent for every query, and each agent
    builds a fresh toolkit. Hooking _create_toolkit lets AgentTeams inject tools
    without modifying upstream CoPaw files.
    """
    global _TOOL_HOOK_INSTALLED
    install_message_filter_hooks()

    if _TOOL_HOOK_INSTALLED:
        return

    from copaw.agents.react_agent import CoPawAgent
    from copaw_worker.hooks.credential_guard import install_credential_guard_hook
    from copaw_worker.hooks.output_sanitizer import create_sanitizer_middleware
    from copaw_worker.hooks.tools.filesync import filesync
    from copaw_worker.hooks.tools.message import message
    from copaw_worker.hooks.tools.projectflow import projectflow
    from copaw_worker.hooks.tools.taskflow import taskflow
    from copaw_worker.hooks.tools.work_report import work_report

    original_create_toolkit = CoPawAgent._create_toolkit
    if getattr(original_create_toolkit, "_agentteams_message_hook", False):
        _TOOL_HOOK_INSTALLED = True
        return

    def create_toolkit_with_agentteams_tools(self: Any, *args: Any, **kwargs: Any):
        toolkit = original_create_toolkit(self, *args, **kwargs)
        try:
            _register_tool_function(
                toolkit,
                message,
                namesake_strategy="override",
            )
            logger.debug("Registered AgentTeams CoPaw message tool")
            _register_tool_function(
                toolkit,
                filesync,
                namesake_strategy="override",
            )
            logger.debug("Registered AgentTeams CoPaw filesync tool")
            if _collaboration_mode() == "direct":
                # Direct teams: the task protocol is not offered at all —
                # removing the tools (not just the docs) is what makes the
                # mode switch real. work_report is the only status channel.
                _register_tool_function(
                    toolkit,
                    work_report,
                    namesake_strategy="override",
                )
                logger.debug("Registered AgentTeams CoPaw work_report tool (direct mode)")
            else:
                _register_tool_function(
                    toolkit,
                    projectflow,
                    namesake_strategy="override",
                )
                logger.debug("Registered AgentTeams CoPaw projectflow tool")
                _register_tool_function(
                    toolkit,
                    taskflow,
                    namesake_strategy="override",
                )
                logger.debug("Registered AgentTeams CoPaw taskflow tool")
        except Exception:
            logger.exception("Failed to register AgentTeams CoPaw tool hooks")
        try:
            toolkit.register_middleware(create_sanitizer_middleware())
        except Exception:
            logger.exception("Failed to register output sanitizer middleware")
        return toolkit

    create_toolkit_with_agentteams_tools._agentteams_message_hook = True  # type: ignore[attr-defined]
    CoPawAgent._create_toolkit = create_toolkit_with_agentteams_tools
    _TOOL_HOOK_INSTALLED = True
    logger.info("Installed AgentTeams CoPaw tool hooks")

    install_credential_guard_hook()
