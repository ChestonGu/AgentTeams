import asyncio

from cimicode_bridge.matrix.gateway import MatrixGateway


class _Member:
    def __init__(self, user_id: str, display_name: str = "") -> None:
        self.user_id = user_id
        self.display_name = display_name


class _JoinedMembers:
    def __init__(self, members: list[_Member]) -> None:
        self.members = members


class _SendResponse:
    event_id = "$sent-1"


class FakeNioClient:
    def __init__(self, members: list[_Member] | None = None) -> None:
        self.members = members
        self.sent: list[tuple[str, dict]] = []

    async def joined_members(self, room_id: str):
        if self.members is None:
            raise RuntimeError("api down")
        return _JoinedMembers(self.members)

    async def room_send(self, room_id: str, etype: str, content: dict, **kwargs):
        self.sent.append((room_id, content))
        return _SendResponse()


ROOM = "!room-1:x"
LEAD = "@oct-lead:agentteams-oct-synapse.opencode-team-test.svc.cluster.local"
OCW = "@ocw-1:agentteams-oct-synapse.opencode-team-test.svc.cluster.local"
ADMIN = "@admin:agentteams-oct-synapse.opencode-team-test.svc.cluster.local"


def _gateway(client: FakeNioClient) -> MatrixGateway:
    gateway = MatrixGateway("http://hs", "token")
    gateway.client = client  # type: ignore[assignment]
    gateway.user_id = OCW
    return gateway


def test_short_localpart_resolved_via_room_members():
    client = FakeNioClient([_Member(LEAD, "oct-lead"), _Member(OCW), _Member(ADMIN, "admin")])
    gateway = _gateway(client)

    asyncio.run(gateway.send_text(ROOM, "TASK_COMPLETED [t-1] @oct-lead 交付完毕"))

    mentions = client.sent[0][1]["m.mentions"]["user_ids"]
    assert mentions == [LEAD]


def test_full_mxid_in_body_passes_through():
    client = FakeNioClient([_Member(LEAD), _Member(OCW)])
    gateway = _gateway(client)

    body = f"已提交，请 @{LEAD} 验收"
    asyncio.run(gateway.send_text(ROOM, body))

    assert client.sent[0][1]["m.mentions"]["user_ids"] == [LEAD]


def test_unknown_localpart_yields_no_mentions():
    client = FakeNioClient([_Member(LEAD), _Member(OCW)])
    gateway = _gateway(client)

    asyncio.run(gateway.send_text(ROOM, "path@host 里的 @stranger 不应成为 mention"))

    assert "m.mentions" not in client.sent[0][1]


def test_members_api_failure_degrades_to_no_mentions():
    client = FakeNioClient(None)
    gateway = _gateway(client)

    asyncio.run(gateway.send_text(ROOM, "done @oct-lead"))

    assert "m.mentions" not in client.sent[0][1]


def test_self_mention_excluded():
    client = FakeNioClient([_Member(LEAD), _Member(OCW, "ocw-1")])
    gateway = _gateway(client)

    asyncio.run(gateway.send_text(ROOM, "@ocw-1 完成 @oct-lead"))

    assert client.sent[0][1]["m.mentions"]["user_ids"] == [LEAD]


def test_display_name_match_resolves():
    client = FakeNioClient([_Member(LEAD, "组长"), _Member(OCW)])
    gateway = _gateway(client)

    asyncio.run(gateway.send_text(ROOM, "@组长 请查收"))

    assert client.sent[0][1]["m.mentions"]["user_ids"] == [LEAD]


def test_explicit_content_mentions_not_overwritten():
    client = FakeNioClient([_Member(LEAD), _Member(OCW)])
    gateway = _gateway(client)

    asyncio.run(
        gateway.send_text(ROOM, "hi @oct-lead", content={"m.mentions": {"user_ids": [ADMIN]}})
    )

    assert client.sent[0][1]["m.mentions"]["user_ids"] == [ADMIN]


def test_multiple_mentions_deduped_in_order():
    client = FakeNioClient([_Member(LEAD), _Member(OCW), _Member(ADMIN, "admin")])
    gateway = _gateway(client)

    asyncio.run(gateway.send_text(ROOM, "@admin @oct-lead @oct-lead 请复核"))

    assert client.sent[0][1]["m.mentions"]["user_ids"] == [ADMIN, LEAD]
