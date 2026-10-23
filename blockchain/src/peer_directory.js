/**
 * The VPS peer directory: who has registered, and the anchor transaction each
 * registration writes to the ledger.
 */
'use strict';

const { readJsonFile, writeJsonFile } = require('./fs_util');
const { sha256Hex } = require('./identity');
const { Transaction } = require('./chain_model');

class PeerDirectory {
  constructor({ file, getLedger }) {
    this.file = file;
    this.getLedger = getLedger;
    const payload = readJsonFile(this.file, { version: 1, peers: [] });
    this.peers = Array.isArray(payload.peers) ? payload.peers : [];
  }

  save() {
    writeJsonFile(this.file, {
      version: 1,
      peers: this.peers,
    });
  }

  normalizePeer(peer, existing = null) {
    const now = new Date().toISOString();
    const username = String(peer.username || '').trim();
    const walletAddress = String(peer.wallet_address || peer.wallet || '').trim();
    const endpointUrl = String(peer.endpoint_url || peer.endpoint || '').trim();

    if (!username) {
      throw new Error('Peer username is required');
    }
    if (!walletAddress) {
      throw new Error('Peer wallet_address is required');
    }
    if (!endpointUrl) {
      throw new Error('Peer endpoint_url is required');
    }

    return {
      username,
      wallet_address: walletAddress,
      endpoint_url: endpointUrl,
      network: String(peer.network || peer.network_name || 'tala').trim() || 'tala',
      status: String(peer.status || existing?.status || 'online').trim() || 'online',
      discoverable: peer.discoverable === undefined ? true : Boolean(peer.discoverable),
      // The key this peer says it signs blocks with. Advertised, not trusted:
      // a directory entry is a claim by whoever wrote it, and admitting a
      // signer is a decision the receiving owner makes deliberately.
      signing_key: String(peer.signing_key || existing?.signing_key || '').trim(),
      capabilities: Array.isArray(peer.capabilities) ? peer.capabilities : [],
      metadata: peer.metadata && typeof peer.metadata === 'object' ? peer.metadata : {},
      last_seen: peer.last_seen || existing?.last_seen || now,
      created_at: existing?.created_at || now,
      updated_at: now,
    };
  }

  find(identifier) {
    const needle = String(identifier || '').trim();
    if (!needle) {
      return null;
    }
    return this.peers.find(peer => peer.username === needle || peer.wallet_address === needle) || null;
  }

  list(filters = {}) {
    let peers = [...this.peers];
    if (filters.discoverable !== undefined) {
      peers = peers.filter(peer => peer.discoverable === filters.discoverable);
    }
    if (filters.network) {
      peers = peers.filter(peer => peer.network === filters.network);
    }
    if (filters.username) {
      const username = String(filters.username).trim();
      peers = peers.filter(peer => peer.username === username);
    }
    if (filters.wallet_address) {
      const walletAddress = String(filters.wallet_address).trim();
      peers = peers.filter(peer => peer.wallet_address === walletAddress);
    }
    if (filters.onlineOnly) {
      peers = peers.filter(peer => peer.status === 'online');
    }
    return peers.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  }

  register(peer, { anchor = true } = {}) {
    const existing = this.find(peer.username) || this.find(peer.wallet_address);
    const normalized = this.normalizePeer(peer, existing);
    if (existing) {
      const index = this.peers.findIndex(item => item.username === existing.username || item.wallet_address === existing.wallet_address);
      if (index >= 0) {
        this.peers[index] = normalized;
      } else {
        this.peers.push(normalized);
      }
    } else {
      this.peers.push(normalized);
    }
    this.save();

    if (anchor) {
      try {
        const directoryHash = sha256Hex(JSON.stringify(normalized));
        const anchorTx = new Transaction(
          `peer-directory:${normalized.wallet_address}`,
          existing ? 'peer-directory-update' : 'peer-directory-register',
          directoryHash,
          normalized.username,
          'vps-discovery',
          Date.now(),
          null,
          {
            directory_entry: normalized,
          }
        );
        this.getLedger().addTransaction(anchorTx);
      } catch (err) {
        console.warn(`[Tala Chain] Failed to anchor peer directory update for ${normalized.username}: ${err.message}`);
      }
    }

    return normalized;
  }

  heartbeat(identifier, patch = {}) {
    const existing = this.find(identifier);
    if (!existing) {
      return null;
    }
    const updated = this.normalizePeer({
      ...existing,
      ...patch,
      username: existing.username,
      wallet_address: existing.wallet_address,
      endpoint_url: patch.endpoint_url || existing.endpoint_url,
      status: patch.status || 'online',
      last_seen: new Date().toISOString(),
    }, existing);
    const index = this.peers.findIndex(item => item.username === existing.username || item.wallet_address === existing.wallet_address);
    if (index >= 0) {
      this.peers[index] = updated;
      this.save();
    }
    return updated;
  }
}

module.exports = { PeerDirectory };
