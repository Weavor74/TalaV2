# File: `.env.example`

## Overview

A template for environment variables. Users copy this to '.env' to configure local secrets and port numbers.

## File Contents

```example
# ===========================================================================
# Tala Cognitive Engine Room — Example Environment Configuration
# ===========================================================================
# Copy this file to .env in the repository root and fill in your values.
#
#   cp .env.example .env
#
# IMPORTANT: Never commit .env to version control (it is in .gitignore).
# All values shown here are safe defaults for local development.
# ===========================================================================


# ---------------------------------------------------------------------------
# PostgreSQL — authoritative durable state store
# ---------------------------------------------------------------------------
# Full asyncpg connection string.
# Format: postgresql://USER:PASSWORD@HOST:PORT/DBNAME
#
# Local dev (Docker or native PG):
TALA_DB_URL=postgresql://tala:tala@localhost:5432/tala

# Production example (change before deployment):
# TALA_DB_URL=postgresql://tala_prod:STRONGPASSWORD@db.internal:5432/tala_prod


# ---------------------------------------------------------------------------
# Inference Configuration
# ---------------------------------------------------------------------------
# Supported modes: local, openai, gemini
INFERENCE_MODE=local

# ---------------------------------------------------------------------------
# vLLM Inference Engine
# ---------------------------------------------------------------------------
# Host and port of the vLLM server (or the embedded AsyncLLMEngine).
VLLM_HOST=127.0.0.1
VLLM_PORT=8000

# HuggingFace model ID or local path.
VLLM_MODEL=mistralai/Mistral-7B-Instruct-v0.3

# Number of GPUs for tensor parallelism (1 = single GPU).
VLLM_TENSOR_PARALLEL_SIZE=1


# ---------------------------------------------------------------------------
# WebSocket Harness Server
# ---------------------------------------------------------------------------
# Bind address for the JSON-RPC 2.0 WebSocket server.
# Electron renderer connects to this address.
WS_HOST=127.0.0.1
WS_PORT=8765

# For live Tala-to-Tala P2P, set WS_HOST=0.0.0.0 on the receiving client
# and share ws://<LAN-IP>:8765 with trusted contacts only.
# For a permanent VPS peer, use WS_HOST=0.0.0.0 and a stable
# TALA_WALLET_ADDRESS, then run ./scripts/start-tala-vps.sh.
# This wallet/address string must match the peer's contact record for you.
TALA_WALLET_ADDRESS=local-tala

# Blockchain / discovery node
# Local desktop installs keep the chain on localhost.
# The permanent VPS should bind publicly so other clients can discover peers.
TALA_CHAIN_BIND_HOST=127.0.0.1
TALA_CHAIN_BIND_PORT=8332

# VPS only: accept first-time Tala client handshakes and self-register them
# as contacts. Keep disabled on desktop Tala clients.
TALA_VPS_ACCEPT_ANY_TALA=0


# ---------------------------------------------------------------------------
# Vault (encrypted secret store)
# ---------------------------------------------------------------------------
# Absolute or relative path to the vault directory.
# Created automatically on first run if it does not exist.
VAULT_PATH=./vault


# ---------------------------------------------------------------------------
# Token Economy
# ---------------------------------------------------------------------------
# USD cost per token (one ten-millionth = 0.0000001).
# Adjust to match actual model pricing if using an external API.
TOKEN_COST_PER_TOKEN=0.0000001

# Starting AI token balance seeded to the wallet on first boot.
# Increase for longer development sessions without top-ups.
INITIAL_AI_TOKEN_BALANCE=1000.0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# One of: DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_LEVEL=INFO

```
