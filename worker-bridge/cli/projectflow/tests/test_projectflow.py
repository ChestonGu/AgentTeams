"""Tests for cli/projectflow (leader-side protocol, contract §5).

Two layers:
  * core — the vendored leader state machine (create/plan/ready/delegate);
  * CLI  — projectflow.py --sync none, including the cross-closure with the
    WORKER taskflow CLI: leader delegates, worker acks+submits, leader
    reads the result. That closure is the whole point of the vendoring:
    identical protocol bytes on both sides.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
REPO = PKG.parent.parent
sys.path.insert(0, str(PKG))

import projectflow_core as core  # noqa: E402

PROJECTFLOW = PKG / "projectflow.py"
TASKFLOW = PKG.parent / "taskflow" / "taskflow.py"

TASKS_JSON = json.dumps([
    {"taskId": "t-1", "title": "First", "assignedTo": "alice"},
    {"taskId": "t-2", "title": "Second", "assignedTo": "bob",
     "dependsOn": ["t-1"]},
])


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.store = core.FileSystemTaskStore(tempfile.mkdtemp(prefix="pf-core-"))

    def test_create_project_writes_meta_and_initial_plan(self):
        meta = core.create_project(self.store, project_id="p-1", title="Proj")
        self.assertEqual(meta.status, "active")
        plan = self.store.read_project_plan("p-1")
        self.assertIn("# Team Project: Proj", plan)
        self.assertIn("**ID**: p-1", plan)
        self.assertIn("**Plan Type**: dag", plan)

    def test_create_duplicate_rejected(self):
        core.create_project(self.store, project_id="p-1", title="Proj")
        with self.assertRaises(core.TaskflowError):
            core.create_project(self.store, project_id="p-1", title="Again")

    def test_add_tasks_then_ready_gating(self):
        core.create_project(self.store, project_id="p-1", title="Proj")
        tasks = core.add_tasks(self.store, project_id="p-1",
                               tasks=json.loads(TASKS_JSON))
        self.assertEqual([t.task_id for t in tasks], ["t-1", "t-2"])
        ready = core.ready_nodes(self.store, project_id="p-1")
        self.assertEqual([t.task_id for t in ready], ["t-1"])  # t-2 gated

    def test_add_tasks_rejects_modifying_non_pending(self):
        core.create_project(self.store, project_id="p-1", title="Proj")
        core.add_tasks(self.store, project_id="p-1", tasks=json.loads(TASKS_JSON))
        core.delegate_task(self.store, project_id="p-1", task_id="t-1", spec="s")
        with self.assertRaises(core.TaskflowError):
            core.add_tasks(self.store, project_id="p-1",
                           tasks=json.loads(TASKS_JSON))

    def test_plan_dag_replaces_and_keeps_status(self):
        core.create_project(self.store, project_id="p-1", title="Proj")
        core.add_tasks(self.store, project_id="p-1", tasks=json.loads(TASKS_JSON))
        core.delegate_task(self.store, project_id="p-1", task_id="t-1", spec="s")
        reshaped = [{"taskId": "t-9", "title": "New", "assignedTo": "carol"},
                    {"taskId": "t-1", "title": "First", "assignedTo": "alice"}]
        final = core.plan_dag(self.store, project_id="p-1", tasks=reshaped)
        by_id = {t.task_id: t for t in final}
        self.assertEqual(by_id["t-1"].status, "delegated")  # preserved
        self.assertEqual(by_id["t-9"].status, "pending")

    def test_validate_dag_rejects_cycle_unknown_dup(self):
        tasks = [
            {"taskId": "a", "title": "A", "assignedTo": "x", "dependsOn": ["b"]},
            {"taskId": "b", "title": "B", "assignedTo": "x", "dependsOn": ["a"]},
        ]
        with self.assertRaises(core.TaskflowError):
            core.validate_dag([core.DagTask(**{
                "task_id": t["taskId"], "title": t["title"],
                "assigned_to": t["assignedTo"], "depends_on": t.get("dependsOn", [])})
                for t in tasks])
        with self.assertRaises(core.TaskflowError):  # unknown dep
            core.validate_dag([core.DagTask(task_id="a", title="A",
                                            assigned_to="x", depends_on=["zz"])])

    def test_loop_plan_roundtrip_and_ready(self):
        core.create_project(self.store, project_id="p-1", title="Proj")
        loop = core.plan_loop(
            self.store, project_id="p-1", goal="Ship it",
            stop_condition="All tests green", iteration_template="Do {{i}}",
            max_iterations=5, current_iteration=2,
            tasks=json.loads(TASKS_JSON))
        self.assertEqual(loop.status, "running")
        parsed = core.parse_loop_plan(self.store.read_project_plan("p-1"))
        self.assertEqual(parsed.goal, "Ship it")
        self.assertEqual(parsed.max_iterations, 5)
        self.assertEqual(parsed.current_iteration, 2)
        self.assertEqual(parsed.iteration_template, "Do {{i}}")
        self.assertEqual([t.task_id for t in parsed.tasks], ["t-1", "t-2"])
        ready = core.ready_loop_nodes(self.store, project_id="p-1")
        self.assertEqual([t.task_id for t in ready], ["t-1"])

    def test_record_iteration_decisions(self):
        core.create_project(self.store, project_id="p-1", title="Proj")
        core.plan_loop(self.store, project_id="p-1", goal="g", stop_condition="s",
                       iteration_template="t", max_iterations=3)
        core.record_loop_iteration(self.store, project_id="p-1", iteration=1,
                                   decision="continue", summary="ok so far",
                                   next_action="retry")
        loop = core.parse_loop_plan(self.store.read_project_plan("p-1"))
        self.assertEqual(loop.current_iteration, 1)
        self.assertEqual(len(loop.history), 1)
        self.assertIn("Next: retry", loop.history[0])
        core.record_loop_iteration(self.store, project_id="p-1", iteration=2,
                                   decision="stop_success", summary="done")
        self.assertEqual(
            core.parse_loop_plan(self.store.read_project_plan("p-1")).status,
            "completed")
        self.assertEqual(core.ready_loop_nodes(self.store, project_id="p-1"), [])

    def test_delegate_prepare_commit_lifecycle(self):
        core.create_project(self.store, project_id="p-1", title="Proj")
        core.add_tasks(self.store, project_id="p-1", tasks=json.loads(TASKS_JSON))
        meta = core.delegate_task(self.store, project_id="p-1", task_id="t-1",
                                  spec="do it", room_id="!r:example.org")
        self.assertEqual(meta.status, "prepared")
        self.assertEqual(meta.assigned_to, "alice")  # canonicalized
        self.assertEqual(self.store.read_task_spec("t-1"), "do it")
        # a completed prepare marks the node delegated -> re-prepare rejected
        # (copaw semantics: the graph, not the meta file, gates the retry)
        with self.assertRaises(core.TaskflowError):
            core.delegate_task(self.store, project_id="p-1", task_id="t-1",
                               spec="do it", room_id="!r:example.org")
        # t-2 still blocked by delegated (not completed) t-1
        self.assertEqual(core.ready_nodes(self.store, project_id="p-1"), [])
        committed = core.commit_task_assignment(
            self.store, project_id="p-1", task_id="t-1", event_id="$e1")
        self.assertEqual(committed.status, "assigned")
        self.assertEqual(committed.event_id, "$e1")
        # re-commit with a new event id keeps the first one (no duplicate notify)
        recommitted = core.commit_task_assignment(
            self.store, project_id="p-1", task_id="t-1", event_id="$e2")
        self.assertEqual(recommitted.event_id, "$e1")
        self.assertEqual(recommitted.status, "assigned")

    def test_prepare_idempotency_after_partial_failure(self):
        """Meta written but plan write never happened -> re-prepare returns it."""
        core.create_project(self.store, project_id="p-2", title="P2")
        core.add_tasks(self.store, project_id="p-2",
                       tasks=[{"taskId": "t-9", "title": "Nine",
                               "assignedTo": "zoe"}])
        orphan = core.TaskMeta(
            task_id="t-9", project_id="p-2", task_title="Nine",
            assigned_to="zoe", room_id="!r:example.org", status="prepared",
            assigned_at="2026-09-01T00:00:00Z")
        self.store.write_task_meta(orphan)
        recovered = core.delegate_task(self.store, project_id="p-2",
                                       task_id="t-9", spec="s",
                                       room_id="!r:example.org")
        self.assertEqual(recovered.status, "prepared")
        self.assertEqual(recovered.assigned_at, "2026-09-01T00:00:00Z")

    def test_delegate_rejects_non_pending_and_blocked(self):
        core.create_project(self.store, project_id="p-1", title="Proj")
        core.add_tasks(self.store, project_id="p-1", tasks=json.loads(TASKS_JSON))
        with self.assertRaises(core.TaskflowError):  # blocked by t-1
            core.delegate_task(self.store, project_id="p-1", task_id="t-2", spec="s")
        core.delegate_task(self.store, project_id="p-1", task_id="t-1", spec="s")
        with self.assertRaises(core.TaskflowError):  # already delegated
            core.delegate_task(self.store, project_id="p-1", task_id="t-1", spec="s")

    def test_pause_resume(self):
        core.create_project(self.store, project_id="p-1", title="Proj")
        core.add_tasks(self.store, project_id="p-1", tasks=json.loads(TASKS_JSON))
        core.pause_project(self.store, project_id="p-1")
        self.assertEqual(core.ready_nodes(self.store, project_id="p-1"), [])
        with self.assertRaises(core.TaskflowError):  # paused blocks delegation
            core.delegate_task(self.store, project_id="p-1", task_id="t-1", spec="s")
        core.resume_project(self.store, project_id="p-1")
        self.assertEqual(len(core.ready_nodes(self.store, project_id="p-1")), 1)


class CliTest(unittest.TestCase):
    """projectflow.py --sync none, including the worker-side closure."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pf-cli-"))
        self.worker = "alice"
        self.mxid = "@alice:example.org"

    def run_cli(self, *argv, cli=PROJECTFLOW, env_extra=None, input_text=None):
        env = dict(os.environ,
                   AGENTTEAMS_FS_ROOT=str(self.root),
                   AGENTTEAMS_MATRIX_USER_ID=self.mxid)
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, str(cli), "--sync", "none"] + list(argv),
            capture_output=True, text=True, encoding="utf-8",
            env=env, input=input_text)

    def leader(self, *argv, **kw):
        return self.run_cli(*argv, **kw)

    def worker_cli(self, *argv):
        return self.run_cli(*argv, cli=TASKFLOW)

    def bootstrap_project(self):
        self.leader("create-project", "p-1", "--title", "Sim project")
        self.leader("add-tasks", "p-1", "--tasks-json", TASKS_JSON)

    def test_full_leader_worker_closure(self):
        """leader delegates -> worker acks+submits -> leader reads result."""
        self.bootstrap_project()
        out = self.leader("ready", "p-1")
        self.assertIn("- [ ] t-1 — First (assigned: alice)", out.stdout)

        out = self.leader("delegate", "p-1", "t-1", "--spec", "Write READY.",
                          "--room-id", "!r:example.org")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('"status": "prepared"', out.stdout)
        out = self.leader("delegate-commit", "p-1", "t-1", "--event-id", "$ev1")
        self.assertIn('"status": "assigned"', out.stdout)

        out = self.worker_cli("ack", "t-1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Write READY.", out.stdout)  # spec printed on ack
        out = self.worker_cli("submit", "t-1", "--status", "SUCCESS",
                              "--summary", "Done.",
                              "--deliverables", "workspace/out.md")
        self.assertEqual(out.returncode, 0, out.stderr)

        out = self.leader("check", "t-1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("status: submitted", out.stdout)
        self.assertIn("STATUS: SUCCESS", out.stdout)
        self.assertIn("effective_for_acceptance: yes", out.stdout)
        self.assertIn("- shared/tasks/t-1/workspace/out.md", out.stdout)

    def test_check_without_result(self):
        self.bootstrap_project()
        self.leader("delegate", "p-1", "t-1", "--spec", "s",
                    "--room-id", "!r:example.org")
        self.leader("delegate-commit", "p-1", "t-1")
        out = self.leader("check", "t-1")
        self.assertIn("result: <none>", out.stdout)

    def test_show_project_and_plan(self):
        self.bootstrap_project()
        out = self.leader("show-project", "p-1")
        self.assertIn('"title": "Sim project"', out.stdout)
        self.assertIn("--- plan.md ---", out.stdout)
        out = self.leader("show-plan", "p-1")
        self.assertIn("**Plan Type**: dag", out.stdout)

    def test_plan_loop_and_record(self):
        self.leader("create-project", "p-1", "--title", "Loop proj")
        out = self.leader("plan-loop", "p-1", "--goal", "g", "--stop-condition",
                          "s", "--iteration-template", "tmpl", "--max-iterations",
                          "3", "--tasks-json", TASKS_JSON)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("**Plan Type**: loop", out.stdout)
        out = self.leader("record-iteration", "p-1", "--iteration", "1",
                          "--decision", "continue", "--summary", "ok")
        self.assertIn("- Iteration 1: continue — ok", out.stdout)
        out = self.leader("ready", "p-1")
        self.assertIn("- [ ] t-1 — First (assigned: alice)", out.stdout)

    def test_payload_from_file_and_stdin(self):
        self.leader("create-project", "p-1", "--title", "P")
        spec_file = self.root / "spec.txt"
        spec_file.write_text("Spec via file.", encoding="utf-8")
        tasks_file = self.root / "tasks.json"
        tasks_file.write_text(TASKS_JSON, encoding="utf-8")
        out = self.leader("add-tasks", "p-1", "--tasks-json", f"@{tasks_file}")
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.leader("delegate", "p-1", "t-1", "--spec", f"@{spec_file}",
                          "--room-id", "!r:example.org")
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.leader("delegate", "p-1", "t-2", "--spec", "-",
                          input_text="Spec via stdin.")
        self.assertEqual(out.returncode, 1, out.stderr)  # t-2 blocked by t-1

    def test_pause_ready_and_lifecycle(self):
        self.bootstrap_project()
        self.leader("pause-project", "p-1")
        out = self.leader("ready", "p-1")
        self.assertIn("(no ready nodes)", out.stdout)
        self.leader("resume-project", "p-1")
        out = self.leader("ready", "p-1")
        self.assertIn("t-1", out.stdout)
        out = self.leader("complete-project", "p-1")
        self.assertIn('"status": "completed"', out.stdout)

    def test_error_paths(self):
        out = self.leader("delegate", "p-1", "t-x", "--spec", "s")
        self.assertEqual(out.returncode, 1, out.stderr)  # no such project/task
        self.leader("create-project", "p-1", "--title", "P")
        out = self.leader("add-tasks", "p-1", "--tasks-json", "not json")
        self.assertEqual(out.returncode, 1, out.stderr)
        out = self.leader("create-project", "p-1", "--title", "P")
        self.assertEqual(out.returncode, 1, out.stderr)  # duplicate
        out = self.leader("ready")  # missing arg -> usage error
        self.assertEqual(out.returncode, 2, out.stderr)

    def test_worker_cannot_delegate_role_guard(self):
        """The worker taskflow CLI has no leader verbs at all (role split)."""
        self.bootstrap_project()
        out = self.worker_cli("delegate", "p-1", "t-1", "--spec", "s")
        self.assertEqual(out.returncode, 2, out.stderr)  # argparse: no such cmd


if __name__ == "__main__":
    unittest.main()
