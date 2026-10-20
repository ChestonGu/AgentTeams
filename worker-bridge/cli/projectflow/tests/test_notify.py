"""delegate-commit 回复送达：paste-ready 指派文本、预检、--no-notify 旧路径。

Two layers:
  * build_assignment_text unit — the assignment text contract (head line
    + 500-char spec preview) the leader pastes verbatim into the session
    reply;
  * cmd_delegate_commit integration — the same fake store/sync the existing
    suite uses (FileSystemTaskStore on a tempdir + NoSyncBackend). The
    command never sends anything itself: it commits, then prints the
    assignment text between paste markers (contract §5.4).

projectflow deploys as a flat sibling trio (projectflow.py + core + sync in
one scripts/ dir), so notify is a flat top-level module here — not a
`projectflow.notify` submodule (projectflow.py is not a package).
"""
import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))

import notify  # noqa: E402
import projectflow  # noqa: E402
import projectflow_core as core  # noqa: E402


class BuildAssignmentTextTest(unittest.TestCase):
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

    def extract_assignment_block(self, out):
        """Assignment text between the paste markers: find the two
        marker lines (they carry the [notify] prefix), join everything
        strictly between them — that is exactly what print(assignment_text)
        emitted, so it must equal build_assignment_text byte for byte."""
        lines = out.split("\n")
        begin = next(i for i, line in enumerate(lines)
                     if "assignment text begin" in line)
        end = next(i for i, line in enumerate(lines)
                   if "assignment text end" in line)
        return "\n".join(lines[begin + 1:end])

    def test_commit_prints_paste_ready_assignment(self):
        """The command never sends anything (there is no notification
        endpoint in any deployment): commit records the assignment without
        an event id, and stdout prints the full assignment text between
        paste markers for the leader to deliver in the session reply."""
        self.delegate_prepared()
        code, out, err = self.commit(self.args())
        self.assertEqual(code, 0, err)
        meta = self.store.read_task_meta("t-1")
        self.assertEqual(meta.status, "assigned")
        self.assertIsNone(meta.event_id)
        self.assertIn("[commit] t-1: assigned (event_id=-)", out)
        self.assertIn("[notify] no notification endpoint configured", out)
        self.assertIn("nothing was sent", out)
        self.assertIn("---- assignment text begin ----", out)
        self.assertIn("---- assignment text end ----", out)
        # the paste block is the assignment text, byte for byte
        self.assertEqual(
            self.extract_assignment_block(out),
            notify.build_assignment_text("alice:example.org", "t-1",
                                         "Build UI", "spec body"))
        self.assertEqual(
            self.extract_assignment_block(out),
            "@alice:example.org You are assigned task **t-1**: Build UI"
            "\n\nspec body")

    def test_retry_reprints_assignment_block(self):
        """Reply-delivery retry semantics: an assigned task without an
        event_id re-runs harmlessly (commit is idempotent) and re-prints
        the paste block — the leader can re-paste after a lost reply."""
        self.delegate_prepared()
        code, first_out, err = self.commit(self.args())
        self.assertEqual(code, 0, err)
        code, out, err = self.commit(self.args())
        self.assertEqual(code, 0, err)
        self.assertIn("---- assignment text begin ----", out)
        self.assertEqual(self.extract_assignment_block(out),
                         self.extract_assignment_block(first_out))
        meta = self.store.read_task_meta("t-1")
        self.assertEqual(meta.status, "assigned")

    def test_already_assigned_with_event_id_nothing_to_do(self):
        self.delegate_prepared()
        core.commit_task_assignment(self.store, project_id="p-1",
                                    task_id="t-1", event_id="$first")
        code, out, err = self.commit(self.args())
        self.assertEqual(code, 0, err)
        self.assertIn("nothing to do", out)
        self.assertNotIn("assignment text begin", out)
        meta = self.store.read_task_meta("t-1")
        self.assertEqual(meta.event_id, "$first")  # first event id kept

    def test_paste_block_truncates_long_spec(self):
        # the paste block carries the 500-char spec preview, ellipsis included
        long_spec = "x" * 600
        core.delegate_task(self.store, project_id="p-1", task_id="t-1",
                           spec=long_spec, room_id="!team:x")
        code, out, err = self.commit(self.args())
        self.assertEqual(code, 0, err)
        block = self.extract_assignment_block(out)
        self.assertIn("…", block)
        self.assertLessEqual(len(block.split("\n\n", 1)[1]),
                             notify.SPEC_PREVIEW_CHARS + 1)
        self.assertEqual(
            block,
            notify.build_assignment_text("alice:example.org", "t-1",
                                         "Build UI", long_spec))

    def test_default_path_still_requires_assignee_mxid(self):
        # unlike --no-notify, the default path keeps the prechecks
        self.delegate_prepared()
        code, out, err = self.commit(self.args(assignee_mxid=""))
        self.assertEqual(code, 1)
        self.assertIn("--assignee-mxid is required", err)
        meta = self.store.read_task_meta("t-1")
        self.assertEqual(meta.status, "prepared")

    def test_no_notify_manual_event_id_is_legacy_path(self):
        self.delegate_prepared()
        code, out, err = self.commit(
            self.args(no_notify=True, event_id="$manual"))
        self.assertEqual(code, 0, err)
        meta = self.store.read_task_meta("t-1")
        self.assertEqual(meta.status, "assigned")
        self.assertEqual(meta.event_id, "$manual")
        self.assertNotIn("assignment text begin", out)

    def test_missing_room_id_rejected(self):
        self.delegate_prepared(room_id=None)
        code, out, err = self.commit(self.args())
        self.assertEqual(code, 1)
        self.assertIn("re-delegate with --room-id", err)

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
        self.assertIn("paste it into your session reply", out.getvalue())


if __name__ == "__main__":
    unittest.main()
