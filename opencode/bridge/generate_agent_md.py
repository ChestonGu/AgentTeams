#!/usr/bin/env python3
"""agent.md generator for the Matrix bridge (contract §6, v2.4).

The controller's reconcile assembles a copaw-form AGENTS.md (builtin
template + injected Coordination block) for runtimes that consume it. The
opencode worker does not consume that document: this generator reproduces
the same assembly from structured inputs, so no markdown surgery on an
upstream-rendered file is ever needed.

Inputs:

  --template       source template (defaults to the repo copy at
                   template/opencode-worker-agent/AGENTS.md — the static
                   skeleton: marker fences + §2-§8 + exactly two
                   placeholders, {{COORDINATION}} and {{ENVIRONMENT}})
  --runtime-config runtime.yaml (MinIO agents/<name>/runtime/runtime.yaml,
                   the controller-maintained MemberRuntimeConfig snapshot —
                   the single source of identity AND team facts)
  --soul-file      SOUL.md if present  (agents/<name>/SOUL.md)
  --profile-file   PROFILE.md if present

Rendering:

  * {{COORDINATION}} — a Coordination block equivalent to what the
    controller's InjectCoordinationContext writes into copaw AGENTS.md
    (agentteams-controller internal/agentconfig/coordination.go): worker
    form renders Coordinator (the team_leader member), Team Admin, and
    Coordinator Members from the runtime.yaml team section; standalone
    form renders the Manager coordinator. The agentteams-team-context
    fences stay in the template, so the marker skeleton is identical to
    the copaw prompt form.
  * {{ENVIRONMENT}} — worker identity (name, Matrix ID, team, storage
    prefix, date) from the runtime.yaml member/storage sections.
  * SOUL.md / PROFILE.md are appended verbatim after the rendered
    content, in the copaw system-prompt order AGENTS.md -> SOUL.md ->
    PROFILE.md — the persona travels in the prompt, not as sandbox state.

Fail-loud policy: this generator validates instead of tolerating. A
runtime.yaml missing member identity, a team without a team_leader
member, a role this version does not know how to render, or a template
without exactly the two placeholders is a hard error (exit 1) — the
bridge must refuse to start a session rather than feed the agent a
mangled prompt.

Usage:
  python3 generate_agent_md.py --runtime-config <runtime.yaml> \
      [--soul-file SOUL.md] [--profile-file PROFILE.md] \
      [--template <AGENTS.md>] [--date 2026-09-01] [--output out.md]

  --output defaults to stdout, --date to today (UTC).
  Exit codes: 0 ok / 1 input error / 2 usage error.

The golden reference outputs live in bridge/tests/fixtures/;
bridge/tests/test_generate_agent_md.py asserts rendering stays in
lockstep with them.
"""
import argparse
import datetime
import logging
import os
import sys
import time

try:
    import yaml
except ImportError:  # the opencode image ships PyYAML; a bare host lacks it
    yaml = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import agentteams_log
except ImportError:  # module not deployed next door -> no-op shim, never break generation
    class _NoLog:
        @staticmethod
        def setup(*_a, **_k):
            return logging.getLogger("generate_agent_md")
        @staticmethod
        def log_event(*_a, **_k):
            pass
        @staticmethod
        def log_cmd_end(*_a, **_k):
            pass
    agentteams_log = _NoLog

DEFAULT_TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..",
    "template", "opencode-worker-agent", "AGENTS.md")

PLACEHOLDER_COORDINATION = "{{COORDINATION}}"
PLACEHOLDER_ENVIRONMENT = "{{ENVIRONMENT}}"

ENV_SECTION_TEMPLATE = """## Environment

- Worker name: {worker_name}
- Matrix ID: {matrix_id}
- Team: {team}
- Storage prefix: {storage_prefix}
- Today (UTC): {date}"""

# Seam rendered before the persona files so the model attributes them to
# "who I am" rather than "reference material" — copaw appends them bare,
# which works, but the explicit attribution plus the "never override your
# Worker role" precedence note is free insurance. Only rendered when at
# least one persona file is present.
PERSONA_SECTION = """## Persona

The sections below define who you are. They apply on top of the workspace
rules above and never override your Worker role."""


class GenerateError(Exception):
    """Input/protocol error -> exit 1."""


def parse_runtime_config(text):
    """MemberRuntimeConfig text -> the identity and team facts the prompt
    needs.

    Structured parse: the document is YAML marshalled by the controller
    from a typed struct (internal/service/runtime_config.go
    memberRuntimeConfigDocument), so it is loaded with yaml.safe_load and
    read as plain dicts/lists — no line matching. Fields consumed:
    member.name/runtimeName/matrixUserId/role, team.name,
    team.admin.matrixUserId, team.members[].matrixUserId/role,
    storage.sharedPrefix; everything else (credentials, desired, ...) is
    ignored by simply not being read.

    Returns:
      {
        'worker_name', 'matrix_id', 'domain', 'role',   # member.*
        'team_name', 'leader_id', 'admin_id',            # team.*
        'coordinators': [mxid, ...],                     # team.members[role=coordinator]
        'team': bool,                                    # team section present
        'storage_prefix',                                # storage.sharedPrefix
      }
    Worker identity is required (GenerateError when absent). role defaults
    from team presence (team -> worker, no team -> standalone); an explicit
    role=this worker is a team leader is rejected — the leader template is
    future work, not silent degradation."""
    if yaml is None:
        raise GenerateError(
            "PyYAML is required to parse the MemberRuntimeConfig document "
            "(pip install pyyaml; the opencode image ships it)")
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise GenerateError(f"runtime config is not valid YAML: {exc}")
    if not isinstance(doc, dict):
        raise GenerateError(
            f"runtime config top level is {type(doc).__name__}, expected "
            "a mapping")

    member = doc.get("member") or {}
    team = doc.get("team") or {}
    storage = doc.get("storage") or {}
    if not isinstance(member, dict) or not isinstance(team, dict) \
            or not isinstance(storage, dict):
        raise GenerateError(
            "runtime config member/team/storage sections are not mappings")

    worker_name = member.get("name") or member.get("runtimeName")
    matrix_id = member.get("matrixUserId")
    if not worker_name or not matrix_id:
        missing = [name for name, value in
                   (("member.name", worker_name),
                    ("member.matrixUserId", matrix_id)) if not value]
        raise GenerateError(
            "runtime config missing member identity field(s): "
            + ", ".join(missing))
    if ":" not in matrix_id:
        raise GenerateError(
            f"member.matrixUserId is not a full Matrix ID: {matrix_id}")
    domain = matrix_id.rsplit(":", 1)[1]

    has_team = bool(team.get("name"))
    role = member.get("role") or ("worker" if has_team else "standalone")
    if role == "team_leader":
        raise GenerateError(
            "member.role=team_leader: the leader agent.md template is not "
            "built yet (future leader migration); refusing to render a "
            "worker-shaped prompt for a leader")

    admin = team.get("admin") or {}
    admin_id = admin.get("matrixUserId") or ""

    members = team.get("members") or []
    if not isinstance(members, list):
        raise GenerateError("team.members is not a list")
    leader_id = None
    coordinators = []
    seen = set()
    for item in members:
        if not isinstance(item, dict):
            continue
        mxid = item.get("matrixUserId") or ""
        item_role = item.get("role") or ""
        if item_role == "team_leader" and mxid:
            leader_id = mxid
        elif item_role == "coordinator" and mxid \
                and mxid != admin_id and mxid not in seen:
            seen.add(mxid)
            coordinators.append(mxid)

    if role == "worker" and (not has_team or not leader_id):
        raise GenerateError(
            "worker runtime config has no team_leader member in "
            "team.members — cannot render the Coordination block")

    return {
        "worker_name": worker_name,
        "matrix_id": matrix_id,
        "domain": domain,
        "role": role,
        "team_name": team.get("name") or "",
        "leader_id": leader_id,
        "admin_id": admin_id,
        "coordinators": coordinators,
        "team": has_team,
        "storage_prefix": storage.get("sharedPrefix") or "",
    }


def load_runtime_config(path):
    with open(path, encoding="utf-8") as fh:
        return parse_runtime_config(fh.read())


def render_coordination(cfg):
    """The {{COORDINATION}} payload: a Coordination block equivalent to the
    controller's InjectCoordinationContext output for copaw AGENTS.md
    (coordination.go), rendered from the runtime.yaml team facts."""
    lines = ["## Coordination", ""]
    if cfg["role"] == "standalone":
        lines.append(f"- **Coordinator**: @manager:{cfg['domain']} (Manager)")
        lines.append("- Report task completion, blockers, and questions "
                     "to your coordinator")
        lines.append("- Only respond to @mentions from your coordinator "
                     "and Admin")
        return "\n".join(lines)

    lines.append(f"- **Coordinator**: {cfg['leader_id']} "
                 f"(Team Leader of {cfg['team_name']})")
    if cfg["admin_id"]:
        lines.append(f"- **Team Admin**: {cfg['admin_id']} "
                     "(has admin authority within this team)")
    if cfg["coordinators"]:
        lines.append("- **Coordinator Members**:")
        for mxid in cfg["coordinators"]:
            lines.append(f"  - {mxid} — can assign tasks and make "
                         "decisions within the team")
    lines.append("- Report task completion, blockers, and questions "
                 "to your coordinator")
    audiences = ["your coordinator"]
    if cfg["admin_id"]:
        audiences.append("Team Admin")
    if cfg["coordinators"]:
        audiences.append("coordinator members")
    # coordination.go wording: one audience joins with "and" (no comma),
    # two or more join with a serial comma before the final "and"
    audiences.append("global Admin")
    if len(audiences) == 2:
        respond = "{} and {}".format(*audiences)
    else:
        respond = ", ".join(audiences[:-1]) + ", and " + audiences[-1]
    lines.append("- Respond to @mentions from " + respond)
    lines.append("- Do NOT @mention Manager directly — all communication "
                 "goes through your Team Leader")
    return "\n".join(lines)


def render_environment(cfg, date):
    return ENV_SECTION_TEMPLATE.format(
        worker_name=cfg["worker_name"],
        matrix_id=cfg["matrix_id"],
        team=cfg["team_name"] or "standalone",
        storage_prefix=cfg["storage_prefix"] or "agentteams",
        date=date)


def _normalize_extra(text):
    """Normalize a persona file (SOUL.md/PROFILE.md) for verbatim merge."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def generate(cfg, template, date=None, soul_text="", profile_text=""):
    """Parsed runtime config + source template -> the finished agent.md.

    The template must carry exactly one {{COORDINATION}} and one
    {{ENVIRONMENT}} placeholder; after substitution the output must be
    free of any residual braces. SOUL.md/PROFILE.md are appended verbatim
    after the rendered content."""
    for placeholder, name in ((PLACEHOLDER_COORDINATION, "COORDINATION"),
                              (PLACEHOLDER_ENVIRONMENT, "ENVIRONMENT")):
        count = template.count(placeholder)
        if count != 1:
            raise GenerateError(
                f"template must contain exactly one {name} placeholder, "
                f"found {count}")
    for token in ("{{", "}}"):
        residual = template.count(token) - 2  # the two known placeholders
        if residual > 0:
            raise GenerateError(
                f"template carries unknown placeholder braces ({token} "
                f"x{residual} beyond the two known placeholders)")

    if date is None:
        date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    out = template.replace(PLACEHOLDER_COORDINATION,
                           render_coordination(cfg))
    out = out.replace(PLACEHOLDER_ENVIRONMENT,
                      render_environment(cfg, date))
    if "{{" in out or "}}" in out:
        raise GenerateError(
            "residual {{...}} braces in rendered output (placeholder "
            "payload itself is malformed?)")

    extras = [(_normalize_extra(extra) if extra else "").strip()
              for extra in (soul_text, profile_text)]
    if any(extras):
        out = out.rstrip("\n") + "\n\n" + PERSONA_SECTION
        for extra in extras:
            if extra:
                out += "\n\n" + extra
        out += "\n"
    return out


def main(argv=None):
    # The output contract is UTF-8/LF; don't let the host locale (e.g. a
    # GBK Windows console) re-encode the pipe.
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8", newline="\n")
    ap = argparse.ArgumentParser(
        description="Generate the opencode agent.md from the source "
                    "template + MemberRuntimeConfig (contract §6).")
    ap.add_argument("--runtime-config", required=True,
                    help="runtime.yaml path (MinIO agents/<name>/runtime/"
                         "runtime.yaml) — identity and team facts")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE,
                    help=f"source template path (default: {DEFAULT_TEMPLATE})")
    ap.add_argument("--soul-file", default=None,
                    help="optional SOUL.md path; content appended verbatim "
                         "after the rendered agent.md (copaw prompt order)")
    ap.add_argument("--profile-file", default=None,
                    help="optional PROFILE.md path; appended after SOUL.md")
    ap.add_argument("--date", default=None,
                    help="override Today (UTC), YYYY-MM-DD (tests)")
    ap.add_argument("--output", default="-",
                    help="output path, or - for stdout (default)")
    args = ap.parse_args(argv)

    log = agentteams_log.setup("generate_agent_md", sys.argv[1:])
    started = time.monotonic()

    def _read_optional(path):
        if not path:
            return ""
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    try:
        try:
            with open(args.template, encoding="utf-8") as fh:
                template = fh.read()
        except OSError as exc:
            raise GenerateError(f"cannot read template: {exc}")
        if not template.strip():
            raise GenerateError(f"empty template: {args.template}")
        runtime = load_runtime_config(args.runtime_config)
        soul_text = _read_optional(args.soul_file)
        profile_text = _read_optional(args.profile_file)
        out = generate(runtime, template, date=args.date,
                       soul_text=soul_text, profile_text=profile_text)
        agentteams_log.log_event(
            log, "generated",
            output_bytes=len(out.encode("utf-8")),
            worker_name=runtime["worker_name"],
            role=runtime["role"],
            team=runtime["team_name"] or "standalone",
            storage_prefix=runtime["storage_prefix"] or "-",
            coordinators=len(runtime["coordinators"]),
            admin=bool(runtime["admin_id"]),
            soul_merged=bool(soul_text.strip()),
            profile_merged=bool(profile_text.strip()))
    except GenerateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        agentteams_log.log_event(log, "error", level=logging.ERROR,
                                 error=str(exc))
        agentteams_log.log_cmd_end(log, 1, started)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        agentteams_log.log_event(log, "error", level=logging.ERROR,
                                 error=str(exc))
        agentteams_log.log_cmd_end(log, 1, started)
        return 1

    if args.output == "-":
        sys.stdout.write(out)
    else:
        with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(out)
    agentteams_log.log_cmd_end(log, 0, started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
