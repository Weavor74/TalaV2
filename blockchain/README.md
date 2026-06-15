# Custom Node.js Blockchain Node

Tala's local micro-payment and smart contract proof-of-existence ledger.
Built in-house using native Node.js APIs (zero external dependencies).

## Specifications

- **Consensus**: Proof of Authority (PoA) (authorized local validator)
- **RPC Port**: `8332`
- **Integrity**: SHA-256 block hashing, Merkle-root transaction mapping
- **Execution Mode**: Edge-native, offline-first, with optional VPS discovery binding

## API Endpoints

- `POST /chain/tx` - Broadcast a smart contract transaction. Body:
  ```json
  {
    "type": "contract",
    "id": "<uuid>",
    "hash": "<sha256-payload-hash>",
    "creator": "<agent-id>",
    "counterparty": "<agent-id>",
    "document": {
      "encrypted": true,
      "algorithm": "AES-256-GCM",
      "key_id": "local-vault:blockchain-document-key:v1",
      "nonce": "<base64>",
      "ciphertext": "<base64>",
      "tag": "<base64>",
      "aad": "<document-id>:<sha256-payload-hash>"
    },
    "metadata": {
      "storage_policy": "local-blockchain-encrypted-document",
      "content_hash_algorithm": "sha256",
      "document_type": "html-nn | profile | user-profile | node",
      "encryption": "AES-256-GCM"
    }
  }
  ```
  `document` and `metadata` are optional for older transaction types. When a document payload is supplied, the node rejects it unless `document.encrypted` is `true`. The AES-256-GCM key is generated locally under `vault/.keys/blockchain_document_key.bin` or the app user-data vault, and is never posted to the chain.
- `GET /chain/tx/<id>` - Query live on-chain transaction by contract UUID.
- `GET /chain/status` - Get blockchain height, pending transactions, validator status, and peer-directory counts.
- `POST /chain/peers/register` - Register a Tala peer in the VPS discovery directory.
- `POST /chain/peers/heartbeat` - Refresh an existing peer entry.
- `GET /chain/peers` - List discoverable peers.
- `GET /chain/peers/<identifier>` - Look up a peer by username or wallet address.

The chain binds to `127.0.0.1` by default. On the permanent VPS, set `TALA_CHAIN_BIND_HOST=0.0.0.0` so other Tala clients can discover peers through the public discovery node.

## How to Run

Run directly with Node.js:

```bash
node blockchain/src/app.js
```
