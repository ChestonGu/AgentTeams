#!/usr/bin/env python3
"""T11 local orchestrator simulator (design doc §6.4/T11, contract §3/§4/§6).

Stands in for the three orchestrator duties and the copaw Leader so the
whole delegate -> trigger -> ack -> work -> submit -> leader-reads-result
chain can run without a real opencode sandbox or controller:

  * sandbox init        (contract §2: preinstalled skills, shared/ only)
  * agent.md generation (contract §6: the real bridge tool, run as the
                         bridge would run it — source template + a
                         runtime.yaml in, system prompt out; the
                         Coordination block is rendered from the
                         runtime.yaml team facts)
  * trigger message     (contract §4: copaw double-marker format)
  * reply forwarding    (contract §4: forward as-is; NO_REPLY swallowed)
  * fake leader         (the real projectflow CLI: plan/delegate/check —
                         the future opencode-leader path)

The "worker" is scripted (no LLM): it follows the exact behavior sequence
AGENTS.md §5 prescribes, driving the real taskflow CLI via the sandbox's
deployed script path.

Usage:
  python simulator.py [--mode none|mc] [--keep]
    none = local shared/ authoritative (default; no MinIO needed)
    mc   = MinIO authoritative (needs the §1 env trio + AGENTTEAMS_TEAM
           + mc on PATH; the team name resolves the teams/{team}/shared/
           scope — without it the CLI falls back to the agt lookup the
           sandbox does not have)
"""
import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "template", "opencode-worker-agent")
GENERATOR = os.path.join(HERE, "..", "bridge", "generate_agent_md.py")

# Deployment copies (contract §2: skills preinstalled in the image; the
# bridge-side logger copy feeds generate_agent_md.py). Each must stay
# byte-identical to its cli/ source — a stale copy means the sandbox image
# (or the bridge generator) ships old behavior.
REPO = os.path.join(HERE, "..")
W_TMPL = os.path.join(REPO, "template", "opencode-worker-agent",
                      "skills", "task-management", "scripts")
W_SHARE = os.path.join(REPO, "template", "opencode-worker-agent",
                       "skills", "file-sharing", "scripts")
L_TMPL = os.path.join(REPO, "template", "opencode-leader-agent",
                      "skills", "task-management", "scripts")
BRIDGE = os.path.join(REPO, "bridge")
DEPLOY_COPIES = [
    (os.path.join(REPO, "cli", "taskflow", "taskflow.py"),
     os.path.join(W_TMPL, "taskflow.py")),
    (os.path.join(REPO, "cli", "taskflow", "taskflow_core.py"),
     os.path.join(W_TMPL, "taskflow_core.py")),
    (os.path.join(REPO, "cli", "taskflow", "mc_sync.py"),
     os.path.join(W_TMPL, "mc_sync.py")),
    (os.path.join(REPO, "cli", "taskflow", "agentteams_log.py"),
     os.path.join(W_TMPL, "agentteams_log.py")),
    (os.path.join(REPO, "cli", "taskflow", "mc_sync.py"),
     os.path.join(W_SHARE, "mc_sync.py")),
    (os.path.join(REPO, "cli", "taskflow", "agentteams_log.py"),
     os.path.join(W_SHARE, "agentteams_log.py")),
    (os.path.join(REPO, "cli", "sync", "agentteams_sync.py"),
     os.path.join(W_SHARE, "agentteams_sync.py")),
    (os.path.join(REPO, "cli", "projectflow", "projectflow.py"),
     os.path.join(L_TMPL, "projectflow.py")),
    (os.path.join(REPO, "cli", "projectflow", "projectflow_core.py"),
     os.path.join(L_TMPL, "projectflow_core.py")),
    (os.path.join(REPO, "cli", "projectflow", "mc_sync.py"),
     os.path.join(L_TMPL, "mc_sync.py")),
    (os.path.join(REPO, "cli", "projectflow", "agentteams_log.py"),
     os.path.join(L_TMPL, "agentteams_log.py")),
    (os.path.join(REPO, "cli", "taskflow", "agentteams_log.py"),
     os.path.join(BRIDGE, "agentteams_log.py")),
]

# contract §4 — byte-identical to copaw matrix_channel.py:154-158
HISTORY_MARKER = "[Chat messages since your last reply - for context]"
CURRENT_MARKER = "[Current message - respond to this]"

FAILS = []


def ok(msg):
    print(f"  ok: {msg}")


def fail(msg):
    print(f"  FAIL: {msg}")
    FAILS.append(msg)


def step(msg):
    print(f"\n== {msg} ==")


def check(cond, msg_ok, msg_fail):
    ok(msg_ok) if cond else fail(msg_fail)


# --------------------------------------------------------------------------
# contract §6 — agent.md generation, exactly as the bridge runs it
# --------------------------------------------------------------------------
def generate_agent_md(worker_name, matrix_id, team_name, bridge_log, workdir):
    """Run bridge/generate_agent_md.py as the bridge would run it.

    The bridge pulls the runtime.yaml (agents/<name>/runtime/runtime.yaml)
    — here written from the sim identity with the full team shape the
    controller maintains (leader/admin/coordinator members) — and the
    generator renders the source template + that document into the system
    prompt. No upstream AGENTS.md is consumed (v2.4). The generator's
    unified-log output goes to bridge_log (stderr silenced so the stdout
    contract stays byte-clean)."""
    runtime_yaml = os.path.join(workdir, "runtime.yaml")
    with open(runtime_yaml, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "apiVersion: agentteams.io/v1beta1\n"
            "kind: MemberRuntimeConfig\n"
            "member:\n"
            f"  name: {worker_name}\n"
            f"  matrixUserId: '{matrix_id}'\n"
            "  role: worker\n"
            "storage:\n"
            f"  sharedPrefix: teams/{team_name}/shared\n"
            "team:\n"
            f"  name: {team_name}\n"
            "  admin:\n"
            "    matrixUserId: '@sim-admin:example.org'\n"
            "  members:\n"
            f"  - matrixUserId: '@{team_name}-leader:example.org'\n"
            "    role: team_leader\n"
            f"  - matrixUserId: '{matrix_id}'\n"
            "    role: worker\n"
            "  - matrixUserId: '@sim-coordinator:example.org'\n"
            "    role: coordinator\n")
    env = dict(os.environ, AGENTTEAMS_LOG_FILE=bridge_log,
               AGENTTEAMS_LOG_STDERR="0")
    proc = subprocess.run(
        [sys.executable, GENERATOR,
         "--runtime-config", runtime_yaml],
        capture_output=True, text=True, encoding="utf-8", env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"generator failed: {proc.stderr.strip()}")
    if proc.stderr.strip():
        raise RuntimeError(f"generator noisy on valid input: {proc.stderr}")
    return proc.stdout


# --------------------------------------------------------------------------
# contract §4 — trigger message construction / reply forwarding
# --------------------------------------------------------------------------
def build_trigger(history, current):
    """history: list of (sender, body, message_id); oldest first, cap 50."""
    history = history[-50:]
    if not history:
        return f"{CURRENT_MARKER}\n{current}"
    lines = [f"{sender}: {body} [id:{mid}]"
             for sender, body, mid in history]
    return f"{HISTORY_MARKER}\n" + "\n".join(lines) + f"\n\n{CURRENT_MARKER}\n{current}"


def forward_reply(room_log, reply):
    """Orchestrator forwards the worker's final turn text to the source room.

    NO_REPLY means "nothing to send" — swallowed, nothing written."""
    if reply.strip() == "NO_REPLY":
        return False
    with open(room_log, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(reply.strip("\n") + "\n")
    return True


# --------------------------------------------------------------------------
# contract §2/§3 (v2) — sandbox layout: preinstalled image + shared/ only
# --------------------------------------------------------------------------
def sandbox_init(root, worker, mode):
    """Simulate the preinstalled image: skills (with scripts) at the image
    layout. Runtime state is shared/ only — no agents/<name>/ tree, no
    SOUL.md, no memory (contract v2 §0.2/§0.3)."""
    skills_dst = os.path.join(root, "skills")
    shutil.copytree(os.path.join(TEMPLATE, "skills"), skills_dst,
                    dirs_exist_ok=True)
    os.makedirs(os.path.join(root, "shared"), exist_ok=True)
    if mode == "mc":  # the image would carry the exec bit; replay it here
        for base, _, files in os.walk(skills_dst):
            for name in files:
                if name.endswith(".sh"):
                    os.chmod(os.path.join(base, name), 0o755)
    return skills_dst


# --------------------------------------------------------------------------
# fake Leader — drives the real projectflow CLI (the future opencode-leader
# path; today's copaw leader is protocol-equivalent on the same files)
# --------------------------------------------------------------------------
class FakeLeader:
    SCRIPT = os.path.join(HERE, "..", "cli", "projectflow", "projectflow.py")

    def __init__(self, root, mode, worker_mxid):
        self.root = root
        self.mode = mode
        self.worker_mxid = worker_mxid

    def _projectflow(self, *argv):
        env = dict(os.environ, AGENTTEAMS_FS_ROOT=self.root,
                   AGENTTEAMS_WORKER_NAME="sim-leader")
        proc = subprocess.run(
            [sys.executable, self.SCRIPT, "--sync", self.mode] + list(argv),
            capture_output=True, text=True, env=env, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(f"projectflow {argv[0]} failed: {proc.stderr.strip()}")
        return proc.stdout

    def setup(self, project_id, task_id, title):
        self._projectflow("create-project", project_id, "--title", title)
        self._projectflow("add-tasks", project_id, "--tasks-json",
                          json.dumps([{"taskId": task_id, "title": title,
                                       "assignedTo": self.worker_mxid}]))

    def delegate(self, project_id, task_id, room_id, spec):
        self._projectflow("delegate", project_id, task_id,
                          "--spec", spec, "--room-id", room_id)
        self._projectflow("delegate-commit", project_id, task_id)

    def read_back(self, task_id):
        """What the leader does after submit: check (pull + meta + result)."""
        return self._projectflow("check", task_id)


# --------------------------------------------------------------------------
# scripted opencode worker (behavior sequence per AGENTS.md §5)
# --------------------------------------------------------------------------
class FakeWorker:
    def __init__(self, root, worker, matrix_id, mode):
        self.root = root
        self.worker = worker
        self.matrix_id = matrix_id
        self.mode = mode
        self.script = os.path.join(root, "skills", "task-management",
                                   "scripts", "taskflow.py")

    def _taskflow(self, *argv):
        env = dict(os.environ,
                   AGENTTEAMS_WORKER_NAME=self.worker,
                   AGENTTEAMS_MATRIX_USER_ID=self.matrix_id,
                   AGENTTEAMS_FS_ROOT=self.root)
        proc = subprocess.run(
            [sys.executable, self.script] + (["--sync", self.mode]
                                             if self.mode != "none" else
                                             ["--sync", "none"]) + list(argv),
            capture_output=True, text=True, env=env, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(f"taskflow {argv[0]} failed: {proc.stderr.strip()}")
        return proc.stdout

    def run_task(self, trigger, task_id, spec_keyword, deliverable_text):
        """The prescribed sequence: acknowledge -> ack (spec in output) ->
        do the work -> submit -> reply with the completion notice."""
        current = trigger.split(CURRENT_MARKER)[-1].strip()
        assert spec_keyword in current, "trigger must carry the task assignment"
        self._taskflow("check", task_id)            # reconciles state
        ack_out = self._taskflow("ack", task_id)    # prints the spec
        if spec_keyword not in ack_out:
            raise RuntimeError("ack output missing the spec content")
        task_ws = os.path.join(self.root, "shared", "tasks", task_id, "workspace")
        os.makedirs(task_ws, exist_ok=True)
        with open(os.path.join(task_ws, "deliverable.md"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(deliverable_text + "\n")
        self._taskflow("submit", task_id, "--status", "SUCCESS",
                       "--summary", "Sim deliverable produced.",
                       "--deliverables", "workspace/deliverable.md")
        return f"@coordinator TASK_COMPLETED: {task_id} Sim deliverable produced."

    def run_idle(self, trigger):
        """Message unrelated to any of this worker's tasks -> stays silent."""
        return "NO_REPLY"


# --------------------------------------------------------------------------
# main scenario
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["none", "mc"], default="none")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    worker = "sim-worker"
    team = "sim-team"
    matrix_id = f"@{worker}:example.org"

    work = tempfile.mkdtemp(prefix="sim-")
    if not args.keep:
        import atexit
        atexit.register(shutil.rmtree, work, ignore_errors=True)
    root = os.path.join(work, "fs")

    # -- deployment copies match their cli sources (§2) --------------------
    step("deployment copies (§2): template scripts byte-identical to cli/")
    stale = [dst for src, dst in DEPLOY_COPIES
             if not (os.path.exists(dst)
                     and filecmp.cmp(src, dst, shallow=False))]
    check(not stale,
          f"all {len(DEPLOY_COPIES)} template script copies current",
          f"stale deployment copies: {[os.path.basename(p) for p in stale]}")

    # -- sandbox layout (§2/§3, v2) ----------------------------------------
    step("sandbox layout (v2 §2): preinstalled skills, no per-agent tree")
    skills_dst = sandbox_init(root, worker, args.mode)
    ok(f"preinstalled skills at {skills_dst} (image simulation)")

    # -- agent.md generation (§6: real bridge tool, template + runtime.yaml) --
    step("agent.md generation (§6): bridge tool, source template + runtime.yaml in")
    logs_dir = os.path.join(work, "logs")
    bridge_log = os.path.join(logs_dir, "bridge-generate.log")
    prompt = generate_agent_md(worker, matrix_id, team, bridge_log, work)
    check(prompt.startswith("<!-- agentteams-builtin-start -->"),
          "controller fence skeleton in template (copaw-style, file starts with it)",
          "builtin-start fence lost")
    check(prompt.index("## 8.") < prompt.index("\n<!-- agentteams-builtin-end -->\n")
          < prompt.index("## Coordination"),
          "builtin-end fence closes the builtin sections, before Coordination",
          "builtin fence misplaced")
    check("## Coordination" in prompt
          and f"**Coordinator**: @{team}-leader:example.org" in prompt
          and "**Team Admin**: @sim-admin:example.org" in prompt
          and "**Coordinator Members**" in prompt
          and "@sim-coordinator:example.org" in prompt,
          "Coordination block rendered from runtime.yaml team facts",
          "coordination block")
    check("## 9." not in prompt and "| Worker | Matrix ID |" not in prompt,
          "no rendered team table (Coordination block is the team source)",
          "stale team table present")
    check("# opencode Worker Agent Workspace" in prompt,
          "opencode preamble", "preamble")
    check("## 2. Response Language" in prompt,
          "neutral section present in the source template", "template lost")
    check(f"Worker name: {worker}" in prompt and f"Team: {team}" in prompt
          and f"Storage prefix: teams/{team}/shared" in prompt,
          "environment section rendered from runtime.yaml", "environment")
    check("QwenPaw" not in prompt and "find-skills" not in prompt,
          "template is opencode-native (no find-skills/QwenPaw)", "runtime residue")
    check("## Persona" not in prompt,
          "no Persona seam without SOUL/PROFILE files", "persona seam leaked")
    check("{{" not in prompt, "no unresolved {{...}} left", "placeholder residue")

    # -- unified logging (§5.5): generator trail in its own JSONL file ----
    step("unified log (§5.5): generator left a JSONL trail")
    with open(bridge_log, encoding="utf-8") as fh:
        entries = [json.loads(line) for line in fh if line.strip()]
    conv = [e for e in entries if e.get("event") == "generated"]
    check(bool(conv) and conv[0].get("tool") == "generate_agent_md"
          and conv[0].get("worker_name") == worker,
          "generator events stamped tool/run_id/worker_name", "generator log")

    # -- trigger message formats (§4) -------------------------------------
    step("trigger message (§4): copaw double-marker, byte-identical")
    history = [("alice", "morning all", 101), ("bob", "specs look good", 102)]
    trig_full = build_trigger(history, f"@{worker} please take t-sim-1 (READY-report).")
    expected = ("[Chat messages since your last reply - for context]\n"
                "alice: morning all [id:101]\n"
                "bob: specs look good [id:102]\n\n"
                "[Current message - respond to this]\n"
                f"@{worker} please take t-sim-1 (READY-report).")
    check(trig_full == expected, "history+current format byte-identical", "format")
    trig_empty = build_trigger([], "solo ping")
    check(trig_empty == "[Current message - respond to this]\nsolo ping",
          "empty history -> current-only, no empty marker", "empty history")

    # -- full chain: projectflow delegate -> worker turn -> projectflow check --
    step("full chain: projectflow delegate -> worker turn -> projectflow check")
    leader = FakeLeader(root, args.mode, matrix_id)
    leader.setup("p-sim", "t-sim-1", "Simulated task")
    leader.delegate("p-sim", "t-sim-1", "!sim-room:example.org",
                    "# Spec\n\nWrite workspace/deliverable.md containing READY-report.")
    worker_agent = FakeWorker(root, worker, matrix_id, args.mode)
    room_log = os.path.join(work, "room.log")

    reply = worker_agent.run_task(trig_full, "t-sim-1", "READY-report", "READY-report")
    check(forward_reply(room_log, reply), f"reply forwarded: {reply[:52]}...",
          "reply not forwarded")
    check_out = leader.read_back("t-sim-1")
    check("status: submitted" in check_out, "leader reads status=submitted", "status")
    check("assigned_to: sim-worker" in check_out,
          "assigned_to canonicalized from the roster MXID", "assigned_to")
    check("STATUS: SUCCESS" in check_out
          and "- shared/tasks/t-sim-1/workspace/deliverable.md" in check_out
          and "effective_for_acceptance: yes" in check_out,
          "result.md protocol fields + acceptance flag", "result.md")

    # -- NO_REPLY swallowed (§4) ------------------------------------------
    step("NO_REPLY: forwarded nothing")
    idle = worker_agent.run_idle("how is the weather")
    room_before = os.path.getsize(room_log)
    forward_reply(room_log, idle)
    check(os.path.getsize(room_log) == room_before, "NO_REPLY swallowed", "NO_REPLY leaked")

    # -- unified logging (§5.5): leader + worker in ONE JSONL file --------
    step("unified log (§5.5): leader + worker trails in one file")
    sandbox_log = os.path.join(root, "logs", "agentteams.log")
    with open(sandbox_log, encoding="utf-8") as fh:
        entries = [json.loads(line) for line in fh if line.strip()]
    tools = {e.get("tool") for e in entries}
    changes = [e for e in entries if e.get("event") == "status_change"
               and e.get("task") == "t-sim-1"]
    check({"projectflow", "taskflow"} <= tools,
          f"one file carries both tools: {sorted(tools)}", "tools split across files")
    check(bool(changes),
          "status_change records for t-sim-1 present", "no status_change trail")

    print()
    if FAILS:
        print(f"SIM FAILED: {len(FAILS)} check(s)")
        return 1
    print("SIM PASSED" + (f" (workdir={work}, kept)" if args.keep else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
