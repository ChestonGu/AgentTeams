"""Tests for the AgentTeams CoPaw worker Matrix channel."""

import asyncio
from types import SimpleNamespace

from copaw_worker.matrix_channel import MatrixChannel


class _FakeClient:
    def __init__(self):
        self.rooms = {}
        self.sent = []

    async def room_send(self, room_id, message_type, content, **kwargs):
        self.sent.append((room_id, message_type, content, kwargs))
        return SimpleNamespace(event_id=f"$sent{len(self.sent)}")


async def _noop_typing(_room_id, _typing):
    return None


def _make_channel(user_id: str = "@dag-team-1-lead:hs.local") -> MatrixChannel:
    ch = MatrixChannel.__new__(MatrixChannel)
    ch._user_id = user_id
    ch._client = _FakeClient()
    ch._send_typing = _noop_typing
    return ch


def _write_team_leader_runtime(tmp_path):
    working_dir = tmp_path / "leader" / ".copaw"
    runtime_dir = tmp_path / "leader" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "runtime.yaml").write_text(
        "kind: MemberRuntimeConfig\n"
        "member:\n"
        "  role: team_leader\n"
        "team:\n"
        "  name: dag-team-1\n"
        "  teamRoomId: \"!team-room:hs.local\"\n"
        "  leaderDmRoomId: \"!leader-dm:hs.local\"\n",
        encoding="utf-8",
    )
    return working_dir


def test_worker_channel_suppresses_team_leader_dm_internal_preamble(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(
        "COPAW_WORKING_DIR",
        str(_write_team_leader_runtime(tmp_path)),
    )
    ch = _make_channel()

    for text in (
        "Now I need to notify the dev worker in the team room.",
        "I'll coordinate the team. Let me first check the team organization.",
        "Good, I have a thorough understanding of all the skills. "
        "Now let me check the team organization and available workers.",
        "I have 2 workers available: a dev worker and a QA worker. "
        "Now let me design the DAG plan and create the project.",
        "Let me read the relevant skill documentation to understand the "
        "coordination process.",
        "Now I have a solid understanding of the workflow. Let me first "
        "look up the available workers.",
        "I have two workers available:\n- dev\n- qa\n\n## Team Coordination Plan",
        "Good. I have the team roster:\n"
        "- **Dev worker**: `dag-team-1-dev` (`@dag-team-1-dev:hs.local`)\n"
        "- **QA worker**: `dag-team-1-qa` (`@dag-team-1-qa:hs.local`)\n\n"
        "Let me plan the project using DAG strategy.",
    ):
        asyncio.run(ch.send("!leader-dm:hs.local", text))

    assert ch._client.sent == []


def test_worker_channel_suppresses_leader_preamble_without_runtime_config(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("COPAW_WORKING_DIR", str(tmp_path / "missing" / ".copaw"))
    ch = _make_channel("@dag-team-1-lead:hs.local")

    asyncio.run(
        ch.send(
            "!leader-dm:hs.local",
            "I'll coordinate a team to build this REST API. "
            "Let me first check my team's organization and then plan the work properly.",
        ),
    )

    assert ch._client.sent == []


def test_worker_channel_suppression_reads_default_workspace_runtime(
    tmp_path, monkeypatch,
):
    copaw_root = _write_team_leader_runtime(tmp_path)
    monkeypatch.setenv(
        "COPAW_WORKING_DIR",
        str(copaw_root / "workspaces" / "default"),
    )
    ch = _make_channel()

    asyncio.run(
        ch.send(
            "!leader-dm:hs.local",
            "I'll coordinate the team. Let me first check the team organization.",
        ),
    )

    assert ch._client.sent == []


def test_worker_channel_keeps_team_assignment_reroute(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "COPAW_WORKING_DIR",
        str(_write_team_leader_runtime(tmp_path)),
    )
    ch = _make_channel()

    asyncio.run(
        ch.send(
            "!leader-dm:hs.local",
            "@dag-team-1-dev:hs.local Task assigned: implement the API.",
        ),
    )

    assert ch._client.sent[0][0] == "!team-room:hs.local"
    assert ch._client.sent[0][2]["m.mentions"] == {
        "user_ids": ["@dag-team-1-dev:hs.local"],
    }


def test_worker_channel_reroutes_team_assignment_with_localpart_mention(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "COPAW_WORKING_DIR",
        str(_write_team_leader_runtime(tmp_path)),
    )
    ch = _make_channel()

    asyncio.run(
        ch.send(
            "!leader-dm:hs.local",
            "@dag-team-1-dev Task assigned: implement the API.",
        ),
    )

    assert ch._client.sent[0][0] == "!team-room:hs.local"


def test_worker_channel_reroutes_taskflow_assignment_with_worker_alias(
    tmp_path,
    monkeypatch,
):
    working_dir = _write_team_leader_runtime(tmp_path)
    (working_dir.parent / "AGENTS.md").write_text(
        "- **Team Workers**:\n"
        "  - @dag-team-1-dev:hs.local — Room: !dev:hs.local\n"
        "  - @dag-team-1-qa:hs.local — Room: !qa:hs.local\n"
        "- Team coordination rules follow.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COPAW_WORKING_DIR", str(working_dir))
    ch = _make_channel()

    asyncio.run(
        ch.send(
            "!leader-dm:hs.local",
            "@dev:hs.local New task [api-design]: "
            "Read shared/tasks/api-design/spec.md and start the task.",
        ),
    )

    assert ch._client.sent[0][0] == "!team-room:hs.local"
    assert ch._client.sent[0][2]["m.mentions"] == {
        "user_ids": ["@dag-team-1-dev:hs.local"],
    }
    assert ch._client.sent[0][2]["body"].startswith(
        "@dag-team-1-dev:hs.local New task [api-design]",
    )


def test_worker_channel_suppresses_no_reply(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "COPAW_WORKING_DIR",
        str(_write_team_leader_runtime(tmp_path)),
    )
    ch = _make_channel()

    asyncio.run(ch.send("!leader-dm:hs.local", "NO_REPLY"))

    assert ch._client.sent == []


def _write_worker_collaboration_runtime(tmp_path, collaboration):
    working_dir = tmp_path / "worker" / ".copaw"
    runtime_dir = tmp_path / "worker" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "runtime.yaml").write_text(
        "kind: MemberRuntimeConfig\n"
        "member:\n"
        "  role: worker\n"
        "team:\n"
        f"  collaboration: {collaboration}\n",
        encoding="utf-8",
    )
    return working_dir


def _capture_agent_content_parts(ch):
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    ch.build_agent_request_from_user_content = capture
    return captured


def test_direct_request_exposes_current_sender_matrix_id(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "COPAW_WORKING_DIR",
        str(_write_worker_collaboration_runtime(tmp_path, "direct")),
    )
    ch = _make_channel("@dag-team-1-dev:hs.local")
    captured = _capture_agent_content_parts(ch)

    ch.build_agent_request_from_native({
        "sender_id": "@alice:hs.local",
        "meta": {"room_id": "!room:hs.local"},
        "content_parts": [{"type": "text", "text": "@dev 1+1?"}],
    })

    texts = [part.text for part in captured["content_parts"]]
    assert texts == [
        "@dev 1+1?",
        "[Current message sender: @alice:hs.local]",
    ]


def test_taskflow_request_exposes_current_sender_for_direct_answers(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "COPAW_WORKING_DIR",
        str(_write_worker_collaboration_runtime(tmp_path, "taskflow")),
    )
    ch = _make_channel("@dag-team-1-dev:hs.local")
    captured = _capture_agent_content_parts(ch)

    ch.build_agent_request_from_native({
        "sender_id": "@alice:hs.local",
        "meta": {"room_id": "!room:hs.local"},
        "content_parts": [{"type": "text", "text": "@dev 1+1?"}],
    })

    texts = [part.text for part in captured["content_parts"]]
    assert texts == [
        "@dev 1+1?",
        "[Current message sender: @alice:hs.local]",
    ]


def test_direct_answer_send_falls_back_to_current_sender():
    ch = _make_channel("@dag-team-1-dev:hs.local")

    asyncio.run(ch.send(
        "!room:hs.local",
        "2",
        {"sender_id": "@alice:hs.local", "direct_answer_requested": True},
    ))

    content = ch._client.sent[0][2]
    assert content["m.mentions"] == {"user_ids": ["@alice:hs.local"]}
    assert "@alice:hs.local" in content["body"]
    assert "matrix.to" in content["formatted_body"]


def test_work_progress_does_not_auto_mention_current_sender():
    ch = _make_channel("@dag-team-1-dev:hs.local")

    asyncio.run(ch.send(
        "!room:hs.local",
        "Progress: wrote the file; next run tests.",
        {"sender_id": "@alice:hs.local", "direct_answer_requested": False},
    ))

    content = ch._client.sent[0][2]
    assert "m.mentions" not in content
