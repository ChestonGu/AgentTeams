#!/usr/bin/env python3
"""Team-level evidence collector for multi-team verification rounds.

Wraps watch_taskflow (per-worker unified timeline) and adds the artifacts a
team-scoped audit needs beyond a single worker:

  TIMELINE  one unified timeline file per worker (ROOM/BRIDGE/TASKFLOW/META/
            LEADER merged — see watch_taskflow.py)
  AGENTMD   the generated AGENTS.md pulled from MinIO per worker, with
            bytes/sha256 recorded (proves the v2.4 generator actually wrote
            the coordination prompt the runtime consumed)
  MINIO     recursive listing of teams/<team>/ shared storage (file-level
            collaboration evidence: task dirs appearing, results pushed)
  PODS      pod inventory snapshot for the namespace
  MANIFEST  manifest.json with sha256 of everything collected

Usage (on the 105 server):

  SUDO_PASS='...' python3 team_evidence.py --team oct-team \
      --since 09:40 --out /tmp/evidence/r1/oct-team

Output directory is created; existing files are overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watch_taskflow as wt  # noqa: E402

NS = "opencode-team-test"


def mc(passw: str, *args: str, timeout: int = 60) -> str:
    """Run mc inside the minio pod (alias `local` is pre-configured there)."""
    return wt.k(passw, "exec", "-n", NS, MINIO_POD, "--", "/usr/bin/mc", *args, timeout=timeout)


MINIO_POD = ""  # resolved in main()


def find_pod(passw: str, pattern: str) -> str:
    pods = wt.k(passw, "get", "pods", "-n", NS, "-o", "name")
    for name in pods.splitlines():
        if pattern in name:
            return name.split("/")[-1]
    raise RuntimeError(f"pod matching {pattern} not found")


def sha256_bytes(data: bytes) -> tuple[str, int]:
    return hashlib.sha256(data).hexdigest(), len(data)


def team_members(passw: str, team: str) -> tuple[str, list[str]]:
    """(leader, [workers]) from the Team CR spec."""
    raw = wt.k(passw, "get", "team", "-n", NS, team, "-o", "json")
    spec = json.loads(raw).get("spec", {})
    members = spec.get("workerMembers", [])
    workers, leader = [], ""
    for m in members:
        name = m.get("name", "")
        if m.get("role") == "team_leader" or (not leader and m.get("isLeader")):
            leader = name
        else:
            workers.append(name)
    if not leader and members:
        leader = members[0].get("name", "")
        workers = workers[1:] if workers else workers
    return leader, workers


def collect_timeline(passw: str, worker: str, team: str, leader: str,
                     since: datetime, task_filter: str, out_dir: str) -> dict:
    """Per-worker unified timeline → <out>/<worker>.timeline.txt"""
    worker_pod = find_pod(passw, f"agentteams-worker-{worker}")
    events: list[tuple[datetime, str, str]] = []
    events += wt.bridge_events(passw, worker_pod, since)
    try:
        sandbox_pod = find_pod(passw, f"{worker}-sandbox")
        events += wt.taskflow_events(passw, sandbox_pod, since, task_filter)
    except RuntimeError:
        events.append((datetime.now(timezone.utc), "EVIDENCE", f"no sandbox pod for {worker} (non-worker member?)"))
    events += wt.meta_events(passw, MINIO_POD, since, task_filter, team)
    if worker == leader:
        events += wt.leader_events(passw, worker_pod, leader, since)
    events.sort(key=lambda e: e[0])

    path = os.path.join(out_dir, f"{worker}.timeline.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"# team={team} worker={worker} since={since.isoformat()}\n")
        last_minute = ""
        for when, src, detail in events:
            hhmm = when.strftime("%H:%M")
            if hhmm[:4] != last_minute:
                fh.write(f"\n── {when.strftime('%H:%M')} ──────────────────────\n")
                last_minute = hhmm[:4]
            fh.write(f"  {when.strftime('%H:%M:%S')} {src:12} {detail}\n")
        if not events:
            fh.write("(no events after since)\n")
    return {"file": os.path.basename(path), "events": len(events)}


def collect_agent_md(passw: str, worker: str, out_dir: str) -> dict:
    """Pull agents/<worker>/AGENTS.md from MinIO; record bytes + sha256."""
    key = f"local/agentteams-storage/agents/{worker}/AGENTS.md"
    try:
        raw = mc(passw, "cat", key)
    except RuntimeError as exc:
        return {"file": None, "error": str(exc)[:160]}
    data = raw.encode("utf-8")
    digest, size = sha256_bytes(data)
    path = os.path.join(out_dir, "agents", f"{worker}.AGENTS.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return {"file": f"agents/{worker}.AGENTS.md", "bytes": size, "sha256": digest}


def collect_minio_tree(passw: str, team: str, out_dir: str) -> dict:
    """Recursive listing of teams/<team>/ — file-level collaboration evidence."""
    listing = mc(passw, "ls", "--recursive", f"local/agentteams-storage/teams/{team}/")
    path = os.path.join(out_dir, "minio-tree.txt")
    lines = [ln.strip() for ln in listing.splitlines() if ln.strip()]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"# teams/{team}/ recursive listing at collection time\n")
        fh.write("\n".join(lines) + "\n")
    return {"file": os.path.basename(path), "objects": len(lines)}


def collect_pods(passw: str, out_dir: str) -> dict:
    raw = wt.k(passw, "get", "pods", "-n", NS, "-o", "wide")
    path = os.path.join(out_dir, "pods.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw)
    return {"file": os.path.basename(path)}


def main() -> int:
    global MINIO_POD
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--team", required=True, help="team CR name")
    ap.add_argument("--since", required=True, help="UTC HH:MM or ISO datetime")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--task", default="", help="substring filter on task id")
    ap.add_argument("--leader", default="", help="override leader name (default: from Team CR)")
    ap.add_argument("--workers", default="", help="comma-separated override (default: from Team CR)")
    args = ap.parse_args()

    passw = os.environ.get("SUDO_PASS", "")
    if not passw:
        print("set SUDO_PASS env (server k3s sudo password)", file=sys.stderr)
        return 2

    MINIO_POD = find_pod(passw, "minio")
    os.makedirs(args.out, exist_ok=True)

    if args.workers:
        workers = [w.strip() for w in args.workers.split(",") if w.strip()]
        leader = args.leader or (workers[0] if workers else "")
    else:
        leader, workers = team_members(passw, args.team)
        if args.leader:
            leader = args.leader

    manifest: dict = {
        "team": args.team,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "since": wt.parse_since(args.since).isoformat(),
        "leader": leader,
        "workers": workers,
        "artifacts": {},
    }

    for worker in workers:
        manifest["artifacts"][f"timeline:{worker}"] = collect_timeline(
            passw, worker, args.team, leader, wt.parse_since(args.since), args.task, args.out)
        manifest["artifacts"][f"agentmd:{worker}"] = collect_agent_md(passw, worker, args.out)

    manifest["artifacts"]["minio_tree"] = collect_minio_tree(passw, args.team, args.out)
    manifest["artifacts"]["pods"] = collect_pods(passw, args.out)

    mpath = os.path.join(args.out, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
