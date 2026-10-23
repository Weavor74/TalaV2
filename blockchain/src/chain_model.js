/**
 * The two things a chain is made of. A Block hashes the JSON of its
 * transactions, so every Transaction field (including signature and
 * public_key) is part of the block's identity.
 */
'use strict';

const crypto = require('crypto');

class Transaction {
  constructor(id, type, hash, creator, counterparty, timestamp = Date.now(), document = null, metadata = null, signature = null, public_key = null) {
    this.id = id; // Contract UUID
    this.type = type; // "contract", "payment", etc.
    this.hash = hash; // SHA-256 payload hash
    this.creator = creator;
    this.counterparty = counterparty;
    this.timestamp = timestamp;
    this.document = document;
    this.metadata = metadata;
    this.signature = signature;
    this.public_key = public_key;
  }

  calculateHash() {
    const documentData = this.document === null || this.document === undefined
      ? ''
      : JSON.stringify(this.document);
    const metadataData = this.metadata === null || this.metadata === undefined
      ? ''
      : JSON.stringify(this.metadata);
    return crypto
      .createHash('sha256')
      .update(this.id + this.type + this.hash + this.creator + this.counterparty + this.timestamp + documentData + metadataData)
      .digest('hex');
  }
}

class Block {
  constructor(index, previousHash, timestamp, transactions, nonce = 0, signature = null, public_key = null) {
    this.index = index;
    this.previousHash = previousHash;
    this.timestamp = timestamp;
    this.transactions = transactions;
    this.nonce = nonce;
    this.hash = this.calculateHash();
    this.signature = signature;
    this.public_key = public_key;
  }

  calculateHash() {
    const txData = JSON.stringify(this.transactions);
    return crypto
      .createHash('sha256')
      .update(this.index + this.previousHash + this.timestamp + txData + this.nonce)
      .digest('hex');
  }

  mineBlock(difficulty = 1) {
    const target = Array(difficulty + 1).join('0');
    while (this.hash.substring(0, difficulty) !== target) {
      this.nonce++;
      this.hash = this.calculateHash();
    }
  }

  signBlock(privateKeyPem, publicKeyPem) {
    this.public_key = publicKeyPem;
    this.signature = crypto.sign(null, Buffer.from(this.hash), privateKeyPem).toString('hex');
  }
}

module.exports = { Transaction, Block };
