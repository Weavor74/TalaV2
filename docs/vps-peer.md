# Permanent Tala VPS Peer

A VPS Tala is an always-on peer and discovery node that other Tala clients can reach when home machines are offline, behind NAT, or moving between networks.

In this mode, run the Python harness and the public Tala blockchain discovery node. Do not run the Electron desktop shell on the VPS.

## Recommended Topology

- Desktop Tala clients keep the full UI, local profile, wallet contact list, and encrypted local blockchain.
- The VPS Tala keeps its own wallet address, contact records, encrypted blockchain, peer directory, and WebSocket JSON-RPC server.
- The VPS blockchain should bind to `0.0.0.0:8332` behind a firewall or reverse proxy so it can act as the central discovery node.
- Other Tala clients add the VPS as a contact, connect to `ws://<vps-ip-or-domain>:8765`, and register themselves with the VPS blockchain directory.
- Use a VPN or reverse proxy with TLS before exposing the VPS on the public internet.

## VPS `.env`

Copy `.env.example` to `.env` on the VPS and change at least:

```env
TALA_DB_URL=postgresql://tala:STRONGPASSWORD@localhost:5432/tala
WS_HOST=0.0.0.0
WS_PORT=8765
TALA_WALLET_ADDRESS=tala-vps-main
TALA_VPS_ACCEPT_ANY_TALA=1
TALA_CHAIN_BIND_HOST=0.0.0.0
TALA_CHAIN_BIND_PORT=8332
VAULT_PATH=/opt/tala/vault
LOG_LEVEL=INFO
```

For a relay-only peer without local model inference, keep inference conservative:

```env
INFERENCE_MODE=local
VLLM_HOST=127.0.0.1
VLLM_PORT=8000
```

## Install

On a fresh Ubuntu/Debian VPS:

```bash
sudo adduser --system --group --home /opt/tala tala
sudo mkdir -p /opt/tala
sudo chown -R tala:tala /opt/tala
```

Copy this repository to `/opt/tala`, then as the `tala` user:

```bash
cd /opt/tala
./install-linux.sh
cp .env.example .env
nano .env
```

Start manually once:

```bash
./scripts/start-tala-vps.sh
```

This starts:

- VPS blockchain service: `http://127.0.0.1:8332` on the host itself, exposed externally as `http://<vps-ip>:8332`
- Public/VPN P2P harness: `ws://<vps-ip>:8765`
- Public/VPN discovery chain: `http://<vps-ip>:8332`

With `TALA_VPS_ACCEPT_ANY_TALA=1`, the VPS accepts first-time Tala client handshakes and self-registers them as contacts. This mode is intended only for the permanent VPS.
The blockchain node also stores the peer directory, so new clients can discover existing peers through `/chain/peers`.

Manual pre-registration is still available if you want a closed allow-list:

```bash
uv run python scripts/add-vps-contact.py \
  --username @steve-desktop \
  --wallet steve-desktop-wallet \
  --endpoint ws://100.x.x.x:8765
```

The script prompts for the shared secret. Use the same secret when adding the VPS contact on the desktop client.

## Systemd Service

Install the service template:

```bash
sudo cp /opt/tala/deploy/tala-vps.service /etc/systemd/system/tala-vps.service
sudo systemctl daemon-reload
sudo systemctl enable --now tala-vps
sudo systemctl status tala-vps
```

Watch logs:

```bash
journalctl -u tala-vps -f
```

## Firewall

For trusted VPN-only access, expose `8765` only on the VPN interface.

For basic testing:

```bash
sudo ufw allow 8765/tcp
```

Public `ws://` traffic is not encrypted by TLS. Prefer one of:

- Tailscale/ZeroTier/WireGuard and `ws://<vpn-ip>:8765`
- Nginx/Caddy TLS reverse proxy and `wss://tala.example.com`

## Connect Clients

On each desktop Tala, the intended production flow is:

1. Start Tala.
2. Fill out and save the `master-user` profile.
3. Choose `Connect to Tala VPS` when prompted, or choose local-only.
4. Enter the Tala VPS endpoint, wallet address, and shared secret.

After this first pairing, the client stores the VPS as `@tala-vps` and reconnects automatically on startup.

To change or stop the permanent pairing later:

- Use `Update Chain` in the User Profile header to replace the VPS endpoint, wallet, or shared secret.
- Use `Disconnect Chain` to stop automatic reconnect and return the client to local-only mode.
- When disconnecting, choose whether to remove the `@tala-vps` contact or keep it for later manual use.

Manual setup is still possible:

1. Open Contacts.
2. Add the VPS contact with username like `@tala-vps`.
3. Set wallet address to the VPS `TALA_WALLET_ADDRESS`.
4. Set endpoint to `ws://<vps-ip>:8765` or `wss://<domain>`.
5. Set the shared secret to the same value stored for that contact on the VPS.
6. Enable only the profile permissions the VPS is allowed to know.
7. Click `Connect`.
8. Send a message with `/peer @tala-vps hello`.

The automatic chain connection path is specifically Tala client ↔ Tala VPS. Ordinary desktop clients should not be used as the permanent chain endpoint.

When the VPS runs with `TALA_VPS_ACCEPT_ANY_TALA=1`, new clients do not need to be pre-added on the VPS. They must still speak Tala's `tala-p2p/1.0` handshake and include a signed enrollment secret over the VPS connection.
In addition, they should announce themselves to the discovery node via `POST /chain/peers/register` so other Tala clients can find them later.

## Peer Node References

Once a peer is in your contact list and the contact firewall allows the node name, Tala can resolve peer HTML-NN references in the form `@user323/financial` or `@user323/Research Collection`.

- The contact must have the `nodes` share permission enabled.
- The firewall entry for that peer must allow the node name or collection name in `allowed_categories`.
- Tala resolves the peer node directly from the peer's endpoint and injects the returned HTML-NN into context.

## Security Notes

- The shared secret is required for signed handshakes and messages.
- `TALA_VPS_ACCEPT_ANY_TALA=1` accepts any valid Tala client handshake, not arbitrary raw socket traffic.
- Contact permissions decide which profile categories are shared during handshake.
- Node-reference sharing is gated by both contact permissions and the contact firewall allowlist.
- P2P messages are stored locally and anchored to the encrypted local blockchain.
- Back up `/opt/tala/.env`, `/opt/tala/vault`, and blockchain data if this VPS identity matters.
