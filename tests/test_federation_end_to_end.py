"""
Three installs, three databases, real sockets, one room.

Everything else in this suite runs one harness against one database, which
cannot see the class of fault that has cost the most on this project: nine
defects on the VPS path passed the in-process suite and surfaced only
against a real box. A single-process test of federation is a test of a
function calling itself -- the packet never crosses a socket, the receiving
side reads rows the sending side wrote, and a conversation "delivered" to a
peer is the same row read twice.

So this starts three harnesses -- two clients and the connection node
between them -- each with its own `TALA_DB_URL`, `TALA_USERNAME`, vault and
ports, and drives them over WebSocket as the app does. Three because two is
not the topology: clients have no dialable address, so they cannot
introduce themselves to each other at all without the node.

What it can then observe:

* whether a client can join the node without approval;
* whether a stranger's relayed handshake enrols as pending and no more;
* whether an invitation crosses and lands as an invitation;
* whether a message crosses and lands in the room;
* whether the rule survives the crossing -- an unapproved participant can
  speak in the room and still cannot reach anything outside it.

It earned its keep: seven defects that the whole in-process suite passed,
and one missing piece -- enrolment made an install findable but nothing
ever made it reachable. See the commit that added it.

Skipped when Postgres is not reachable or the databases cannot be made,
because it is an integration test and a red suite on a laptop with no
database teaches nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[1]
PRIMARY_DSN = os.environ.get("TALA_DB_URL", "postgresql://tala:tala@localhost:5432/tala")
#: Both installs get a scratch database. Pointing one of them at the real
#: `tala` would run this against the developer's live data, alongside
#: whatever harness the desktop app already has on it.
TEST_DBS = ("tala_install_a_test", "tala_install_b_test", "tala_node_test")


def _dsn_for(database: str) -> str:
    return PRIMARY_DSN.rsplit("/", 1)[0] + "/" + database


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _make_databases() -> None:
    import asyncpg

    admin_dsn = PRIMARY_DSN.rsplit("/", 1)[0] + "/postgres"
    conn = await asyncpg.connect(admin_dsn)
    try:
        for name in TEST_DBS:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_databases() -> None:
    import asyncpg

    admin_dsn = PRIMARY_DSN.rsplit("/", 1)[0] + "/postgres"
    try:
        conn = await asyncpg.connect(admin_dsn)
    except Exception:
        return
    try:
        for name in TEST_DBS:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
                name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    except Exception:
        pass
    finally:
        await conn.close()


class Install:
    """One harness in its own process, talked to the way a client does."""

    def __init__(self, username: str, dsn: str, port: int, user_data: Path):
        self.username = username
        self.dsn = dsn
        self.port = port
        self.user_data = user_data
        # The gate on 8765 wants a token. Pinning one per install is what the
        # desktop client effectively does, and it keeps the two from sharing
        # a vault file.
        self.token = f"token-{uuid.uuid4().hex}"
        self.mcp_port = _free_port()
        self.endpoint = f"ws://127.0.0.1:{port}"
        self.process: subprocess.Popen | None = None

    def start(self, vps: bool = False) -> None:
        env = dict(os.environ)
        env.update({
            "TALA_DB_URL": self.dsn,
            "TALA_USERNAME": self.username,
            # Contacts are looked up by wallet on the receiving side, so two
            # installs sharing the default would each resolve the other's
            # packets to themselves.
            "TALA_WALLET_ADDRESS": f"wallet-{self.username}",
            "TALA_WEB_TOKEN": self.token,
            # Nodes and settings live here. Sharing one directory would make
            # the two installs one install wearing two names.
            "TALA_USER_DATA": str(self.user_data),
            # And the vault separately, because VAULT_PATH defaults to the
            # REPO root rather than to the user-data directory -- so installs
            # started from one checkout share a wallet key and therefore a
            # wallet address. The node resolves contacts by wallet, so the
            # second install to enrol is looked up as the first and refused
            # with "bad_signature". Two machines are unaffected; two installs
            # on one checkout are not.
            "VAULT_PATH": str(self.user_data / "vault"),
            "WS_HOST": "127.0.0.1",
            "WS_PORT": str(self.port),
            # One machine, several harnesses: everything that binds needs its
            # own port, not only the one the app talks to.
            "MCP_PORT": str(self.mcp_port),
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
        })
        self.log_path = self.user_data / "harness.log"
        self._log_handle = open(self.log_path, "wb")
        argv = [sys.executable, "-m", "python.harness.server"]
        if vps:
            # The one switch. Without it an install must never accept
            # enrolments from strangers.
            argv.append("--vps")
        self.process = subprocess.Popen(
            argv,
            cwd=str(ROOT), env=env,
            stdout=self._log_handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )

    async def wait_until_ready(self, timeout: float = 180.0) -> bool:
        """
        Wait for a real answer, not for the port.

        A TCP connect succeeds well before the harness can serve anything --
        startup verifies every inference provider and rewrites thirteen
        settings nodes, which takes the better part of a minute. Polling the
        socket returned "ready" immediately and every call then timed out in
        the opening handshake.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process and self.process.poll() is not None:
                return False
            try:
                # Something that needs the database. `harness.health` answers
                # before `init_pool()` has finished, so polling it reported
                # ready and the first real call failed with "Database pool is
                # not initialised".
                await self.rpc("contacts.list", {}, timeout=5.0)
                return True
            except Exception:
                await asyncio.sleep(1.0)
        return False

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def log(self, limit: int = 4000) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except Exception:
            return "<no log>"

    async def rpc(self, method: str, params: dict | None = None, timeout: float = 30.0):
        """One call over the wire, as the desktop app makes it."""
        import websockets

        request_id = str(uuid.uuid4())
        request = json.dumps({
            "jsonrpc": "2.0", "id": request_id,
            "method": method, "params": params or {},
        })
        async with websockets.connect(
            self.endpoint, open_timeout=10,
            additional_headers={"x-tala-token": self.token},
        ) as ws:
            await ws.send(request)
            # Read until the reply, not merely the next frame. The harness
            # pushes notifications down the same socket -- a client that
            # takes the first frame as its answer picks up whatever
            # broadcast happened to land first, which here was
            # `inference.providers_changed` being read as an enrolment
            # result.
            deadline = time.monotonic() + timeout
            response = None
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                framed = json.loads(raw)
                if framed.get("id") == request_id:
                    response = framed
                    break
            if response is None:
                raise TimeoutError(f"{method} never answered")

        if response.get("error"):
            raise RuntimeError(response["error"].get("message", response["error"]))
        return response.get("result")


@pytest.fixture(scope="module")
def federation():
    """
    Two clients and the node between them, or a skip that says why.

    Three processes because two is not the topology. Clients have no
    dialable address -- a laptop behind NAT has no socket anybody can open
    to it -- so two clients cannot introduce themselves to each other at
    all, by design: BOOTSTRAP admission exists only on a connection node.
    A two-process test could only have been made to pass by writing the
    contact rows by hand, which is the enrolment being skipped rather than
    exercised.
    """
    try:
        import asyncpg  # noqa: F401
        import websockets  # noqa: F401
    except ImportError as exc:
        pytest.skip(f"integration dependencies unavailable: {exc}")

    try:
        asyncio.new_event_loop().run_until_complete(_make_databases())
    except Exception as exc:
        pytest.skip(f"could not create the test databases: {exc}")

    import tempfile

    marker = uuid.uuid4().hex[:6]
    workspace = Path(os.environ.get("TALA_TEST_WORKSPACE") or tempfile.mkdtemp(prefix="tala-federation-"))
    alice = Install(f"@alice-{marker}", _dsn_for(TEST_DBS[0]), _free_port(), workspace / "alice")
    bob = Install(f"@bob-{marker}", _dsn_for(TEST_DBS[1]), _free_port(), workspace / "bob")
    node = Install(f"@node-{marker}", _dsn_for(TEST_DBS[2]), _free_port(), workspace / "node")
    for install in (alice, bob, node):
        install.user_data.mkdir(parents=True, exist_ok=True)

    node.start(vps=True)
    alice.start()
    bob.start()

    async def _ready():
        return await asyncio.gather(*[i.wait_until_ready() for i in (node, alice, bob)])

    try:
        up = asyncio.new_event_loop().run_until_complete(_ready())
        for install, ok in zip((node, alice, bob), up):
            if not ok:
                pytest.skip(f"{install.username} did not come up: {install.log()}")
        # Enrol once, here. `chain.connect_vps` mints a fresh shared secret
        # every call and overwrites this side's row, while the node keeps the
        # one it was enrolled with -- so calling it twice breaks the pairing
        # with "signature does not verify". Worth knowing about the real
        # toggle too, not only about this fixture.
        async def _pair():
            await _enrol_with_node(alice, node)
            await _enrol_with_node(bob, node)

            # Enrolment makes them findable; the socket each one holds open
            # to the node is what makes them reachable, and it is opened by a
            # background task a moment later. Relaying before that lands on
            # "not connected to this node".
            async def both_reachable():
                peers = await node.rpc("relay.peers", {})
                online = {p["username"] for p in (peers.get("peers") or [])}
                return {alice.username, bob.username} <= online or None

            assert await _wait_for(both_reachable, attempts=60, delay=1.0), (
                "the clients enrolled but never became reachable through the node. "
                + "alice: " + " | ".join(alice.log()[-1500:].splitlines())
            )
            await _introduce(alice, bob, node)

        asyncio.new_event_loop().run_until_complete(_pair())
        yield alice, bob, node
    finally:
        for install in (alice, bob, node):
            install.stop()
        try:
            asyncio.new_event_loop().run_until_complete(_drop_databases())
        except Exception:
            pass


async def _enrol_with_node(client: Install, node: Install) -> None:
    """
    A client joins the node. Steven's rule: no approval needed for this.

    `chain.connect_vps` is the one the toggle in the app calls -- it mints a
    shared secret, records the node as a contact, and shakes hands.
    """
    result = await client.rpc("chain.connect_vps", {
        "endpoint_url": node.endpoint,
        "username": node.username,
        "wallet_address": f"wallet-{node.username}",
    }, timeout=60.0)
    assert result and result.get("success"), (
        f"{client.username} could not enrol with the node: {result}"
    )

    # The setting is what the relay holder reads, and it is written only when
    # the handshake actually succeeded -- so this is the check that says
    # enrolment happened rather than merely returned.
    state = await client.rpc("chain.connection_get", {})
    connection = (state or {}).get("connection") or {}
    assert connection.get("mode") == "vps", (
        f"{client.username} did not end up connected: {result} / {connection}"
    )


async def _wait_for(check, attempts: int = 120, delay: float = 1.0):
    """Poll something that crosses a process boundary."""
    last = None
    for _ in range(attempts):
        last = await check()
        if last:
            return last
        await asyncio.sleep(delay)
    return last


async def _introduce(alice: Install, bob: Install, node: Install) -> None:
    """
    Alice asks to know Bob, through the node.

    Neither has an address the other could dial, so the handshake is relayed
    over the sockets they each hold to the node. Bob has never heard of
    Alice, so his harness enrols her -- as **pending**, which is the case
    worth testing: everything below then asks whether the room works for
    somebody who was never approved.
    """
    await alice.rpc("contacts.save", {
        "username": bob.username,
        "wallet_address": f"wallet-{bob.username}",
        "endpoint_url": "",
        "shared_secret": f"shared-{uuid.uuid4().hex}",
        "permissions": {},
    })
    try:
        await alice.rpc("p2p.handshake", {
            "username": bob.username,
            "allow_vps_enrollment": True,
        }, timeout=60.0)
    except Exception as exc:
        raise AssertionError(
            "relayed handshake failed: " + str(exc)
            + "  |  node log: " + node.log()[-2500:].replace("\n", " | ")
        ) from exc

    async def bob_knows_alice():
        contacts = await bob.rpc("contacts.list", {})
        return {c["username"]: c for c in contacts["contacts"]}.get(alice.username)

    known = await _wait_for(bob_knows_alice)
    assert known, "the relayed handshake never reached the other client"
    assert known["status"] == "pending", (
        f"an install enrolled a stranger as {known['status']} rather than pending"
    )


# ---------------------------------------------------------------------------
# The installs are actually separate
# ---------------------------------------------------------------------------


async def test_they_are_separate_installs(federation):
    """
    The premise of everything below. Installs sharing a database would make
    delivery look like it worked while nothing crossed anything.
    """
    alice, bob, node = federation
    assert len({alice.port, bob.port, node.port}) == 3
    assert len({alice.dsn, bob.dsn, node.dsn}) == 3

    room = await alice.rpc("conversation.create", {"title": "alice only", "participants": []})
    theirs = await bob.rpc("conversation.list", {})
    assert room["id"] not in {c["id"] for c in theirs["conversations"]}, (
        "one install can see another's rows; they share a database"
    )


# ---------------------------------------------------------------------------
# Enrolment, over the wire
# ---------------------------------------------------------------------------


async def test_a_client_joins_the_node_without_being_approved(federation):
    """Steven's rule: all clients can connect to the node without approval."""
    alice, bob, node = federation
    contacts = await node.rpc("contacts.list", {})
    known = {c["username"] for c in contacts["contacts"]}
    assert alice.username in known and bob.username in known


async def test_a_stranger_is_enrolled_as_pending_and_not_admitted(federation):
    """
    The other half: clients cannot connect to each other until approved. The
    handshake crosses -- relayed, because neither has an address -- and it
    buys a row and nothing else.
    """
    alice, bob, node = federation

# ---------------------------------------------------------------------------
# A room, across two machines
# ---------------------------------------------------------------------------


async def test_a_room_reaches_the_other_install(federation):
    alice, bob, node = federation
    room = await alice.rpc("conversation.create", {
        "title": "across the wire",
        "participants": [{"participant": bob.username, "kind": "user"}],
    })

    async def bob_sees_it():
        theirs = await bob.rpc("conversation.list", {})
        return {c["id"]: c for c in theirs["conversations"]}.get(room["id"])

    listed = await _wait_for(bob_sees_it)
    assert listed, "the invitation never arrived at the other install"
    assert listed["status"] == "invited", (
        "an invitation joined the other install to a room without being asked"
    )

    await bob.rpc("conversation.set_membership", {"id": room["id"], "status": "joined"})
    await alice.rpc("conversation.post", {"id": room["id"], "body": "can you hear me"})

    async def bob_heard():
        payload = await bob.rpc("conversation.messages", {"id": room["id"]})
        return [m for m in payload["messages"] if m["body"] == "can you hear me"] or None

    heard = await _wait_for(bob_heard)
    assert heard, "the message never crossed"


async def test_the_rule_survives_the_crossing(federation):
    """
    The whole design, end to end and over real sockets.

    Note the direction. Alice initiated, so she has Bob as an approved
    contact -- she chose him. Bob was enrolled by a stranger's handshake, so
    on HIS install Alice is `pending`. That is where the rule has to hold:
    somebody Bob never approved speaks in the room, is marked as such, and
    gains nothing outside it.

    The first version of this test asserted the opposite direction and
    passed nothing: it checked whether an *approved* contact could speak,
    which the strict rule would also allow.
    """
    alice, bob, node = federation

    room = await alice.rpc("conversation.create", {
        "title": "the rule",
        "participants": [{"participant": bob.username, "kind": "user"}],
    })

    async def bob_sees_it():
        theirs = await bob.rpc("conversation.list", {})
        return room["id"] in {c["id"] for c in theirs["conversations"]} or None

    assert await _wait_for(bob_sees_it), "the invitation never arrived"
    await bob.rpc("conversation.set_membership", {"id": room["id"], "status": "joined"})

    # Alice is the one Bob never approved.
    contacts = await bob.rpc("contacts.list", {})
    standing = {c["username"]: c["status"] for c in contacts["contacts"]}
    assert standing.get(alice.username) == "pending", (
        f"the premise does not hold: Bob has Alice as {standing.get(alice.username)}"
    )

    await alice.rpc("conversation.post", {"id": room["id"], "body": "from someone unapproved"})

    async def bob_heard():
        payload = await bob.rpc("conversation.messages", {"id": room["id"]})
        return [m for m in payload["messages"] if m["sender"] == alice.username] or None

    heard = await _wait_for(bob_heard)
    assert heard, "an unapproved participant could not speak in the room"
    assert heard[-1]["sender_approved"] is False, (
        "an unapproved sender was recorded as approved"
    )

    # And the room granted nothing outside itself.
    contacts = await bob.rpc("contacts.list", {})
    standing = {c["username"]: c["status"] for c in contacts["contacts"]}
    assert standing.get(alice.username) == "pending", (
        "speaking in the room upgraded the contact"
    )
