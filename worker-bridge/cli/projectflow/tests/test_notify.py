"""delegate-commit 自动通知:txn 幂等文本、失败保持 prepared、--no-notify 旧路径。

Two layers:
  * notify client unit — the exact POST /api/v1/bridge/send contract body
    (Task 2's outbound endpoint) against a faked urlopen;
  * cmd_delegate_commit integration — the same fake store/sync the existing
    suite uses (FileSystemTaskStore on a tempdir + NoSyncBackend), invoked
    in-process with notify.send_assignment patched on the module the CLI's
    delayed import resolves to.

projectflow deploys as a flat sibling trio (projectflow.py + core + sync in
one scripts/ dir), so notify is a flat top-level module here — not a
`projectflow.notify` submodule (projectflow.py is not a package).
"""
import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))

import notify  # noqa: E402
import projectflow  # noqa: E402
import projectflow_core as core  # noqa: E402


class NotifyClientTest(unittest.TestCase):
    def test_send_assignment_posts_contract_body(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data.decode("utf-8"))
            resp = mock.MagicMock()
            resp.read.return_value = json.dumps(
                {"ok": True, "event_id": "$evt-9"}).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        with mock.patch.object(notify.urllib.request, "urlopen", fake_urlopen):
            event_id = notify.send_assignment(
                "!team:x", "@w:dom", "t-1", "Build UI",
                "spec " * 200, url="http://127.0.0.1:8081")
        self.assertEqual(event_id, "$evt-9")
        self.assertEqual(captured["url"], "http://127.0.0.1:8081/api/v1/bridge/send")
        body = captured["data"]
        self.assertEqual(body["room_id"], "!team:x")
        self.assertEqual(body["txn_id"], "delegate-t-1")
        self.assertIn("@w:dom You are assigned task **t-1**: Build UI",
                      body["text"])
        # spec 预览截断到 500 字
        self.assertLessEqual(len(body["text"].split("\n\n", 1)[1]), 501)

    def test_send_failure_raises(self):
        with mock.patch.object(
                notify.urllib.request, "urlopen",
                side_effect=OSError("conn refused")):
            with self.assertRaises(notify.NotifyError):
                notify.send_assignment("!team:x", "@w:dom", "t-1", "T", "s",
                                       url="http://127.0.0.1:8081")

    def test_ok_false_response_raises(self):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(
            {"ok": False, "error": "matrix gateway not ready"}).encode("utf-8")
        resp.__enter__.return_value = resp
        with mock.patch.object(notify.urllib.request, "urlopen",
                               return_value=resp):
            with self.assertRaisesRegex(notify.NotifyError, "not ready"):
                notify.send_assignment("!team:x", "@w:dom", "t-1", "T", "s",
                                       url="http://127.0.0.1:8081")

    def test_bridge_send_url_required_when_not_passed(self):
        env = {"BRIDGE_SEND_URL": ""}
        with mock.patch.dict("os.environ", env, clear=False):
            with self.assertRaisesRegex(notify.NotifyError, "BRIDGE_SEND_URL"):
                notify.send_assignment("!team:x", "@w:dom", "t-1", "T", "s")

    def test_assignment_text_truncates_with_ellipsis(self):
        text = notify.build_assignment_text("w:dom", "t-1", "Build UI", "x" * 600)
        head, preview = text.split("\n\n", 1)
        self.assertEqual(head, "@w:dom You are assigned task **t-1**: Build UI")
        self.assertTrue(preview.endswith("…"))
        self.assertEqual(len(preview), 501)

    def test_assignment_text_without_spec_is_head_only(self):
        text = notify.build_assignment_text("w:dom", "t-1", "Build UI", "   ")
        self.assertEqual(text, "@w:dom You are assigned task **t-1**: Build UI")


class DelegateCommitNotifyTest(unittest.TestCase):
    """cmd_delegate_commit main paths on the existing fake store/sync."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pf-notify-")
        self.store = core.FileSystemTaskStore(self.tmp)
        self.sync = projectflow.NoSyncBackend()
        core.create_project(self.store, project_id="p-1", title="Proj")
        core.add_tasks(self.store, project_id="p-1", tasks=[{
            "taskId": "t-1", "title": "Build UI", "assignedTo": "alice"}])

    def delegate_prepared(self, room_id="!team:x"):
        return core.delegate_task(self.store, project_id="p-1",
                                  task_id="t-1", spec="spec body",
                                  room_id=room_id)

    def args(self, **kw):
        fields = dict(project_id="p-1", task_id="t-1", event_id=None,
                      assignee_mxid="@alice:example.org", no_notify=False)
        fields.update(kw)
        return argparse.Namespace(**fields)

    def commit(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = projectflow.cmd_delegate_commit(self.store, args, self.sync)
        return code, out.getvalue(), err.getvalue()

    def test_auto_notify_sends_then_records_event_id(self):
        self.delegate_prepared()
        with mock.patch.object(notify, "send_assignment",
                               return_value="$evt-9") as send:
            code, out, err = self.commit(self.args())
        self.assertEqual(code, 0, err)
        # room, mxid (leading @ stripped), task id, title, spec — in order
        send.assert_called_once_with("!team:x", "alice:example.org", "t-1",
                                     "Build UI", "spec body")
        meta = self.store.read_task_meta("t-1")
        self.assertEqual(meta.status, "assigned")
        self.assertEqual(meta.event_id, "$evt-9")
        self.assertIn("[commit] t-1: assigned (event_id=$evt-9)", out)
        self.assertIn("[notify]", out)  # no-second-message warning printed

    def test_already_assigned_skips_notification(self):
        self.delegate_prepared()
        core.commit_task_assignment(self.store, project_id="p-1",
                                    task_id="t-1", event_id="$first")
        with mock.patch.object(notify, "send_assignment") as send:
            code, out, err = self.commit(self.args())
        self.assertEqual(code, 0, err)
        send.assert_not_called()
        self.assertIn("notification skipped", out)
        meta = self.store.read_task_meta("t-1")
        self.assertEqual(meta.event_id, "$first")  # first event id kept

    def test_send_failure_keeps_prepared_exit_1(self):
        self.delegate_prepared()
        boom = notify.NotifyError(
            "assignment notification failed: conn refused")
        with mock.patch.object(notify, "send_assignment", side_effect=boom):
            code, out, err = self.commit(self.args())
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        meta = self.store.read_task_meta("t-1")
        self.assertEqual(meta.status, "prepared")  # retry-safe: txn idempotent
        self.assertIsNone(meta.event_id)

    def test_no_notify_manual_event_id_is_legacy_path(self):
        self.delegate_prepared()
        with mock.patch.object(notify, "send_assignment") as send:
            code, out, err = self.commit(
                self.args(no_notify=True, event_id="$manual"))
        self.assertEqual(code, 0, err)
        send.assert_not_called()
        meta = self.store.read_task_meta("t-1")
        self.assertEqual(meta.status, "assigned")
        self.assertEqual(meta.event_id, "$manual")

    def test_missing_room_id_rejected_before_send(self):
        self.delegate_prepared(room_id=None)
        with mock.patch.object(notify, "send_assignment") as send:
            code, out, err = self.commit(self.args())
        self.assertEqual(code, 1)
        send.assert_not_called()
        self.assertIn("re-delegate with --room-id", err)

    def test_missing_assignee_mxid_rejected_before_send(self):
        self.delegate_prepared()
        with mock.patch.object(notify, "send_assignment") as send:
            code, out, err = self.commit(self.args(assignee_mxid=""))
        self.assertEqual(code, 1)
        send.assert_not_called()
        self.assertIn("--assignee-mxid is required", err)

    def test_delegate_next_hint_names_assignee_mxid(self):
        args = argparse.Namespace(project_id="p-1", task_id="t-1",
                                  spec="spec body", room_id="!team:x")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = projectflow.cmd_delegate(self.store, args, self.sync)
        self.assertEqual(code, 0)
        self.assertIn(
            "delegate-commit p-1 t-1 --assignee-mxid <full-mxid>",
            out.getvalue())
        self.assertIn("do NOT send the assignment yourself", out.getvalue())


if __name__ == "__main__":
    unittest.main()
