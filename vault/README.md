# Vault — Tala Cognitive Engine Room: Protected Core

## Overview

The `vault/` directory is Tala's **network-isolated, PII-protected private store**.

It is governed by strict isolation rules enforced by `python/vault/vault_manager.py`:

- **No outbound network calls** — vault operations assert that no socket connections are opened during execution.
- **PII interception** — any execution trace containing PII-related keywords (`ssn`, `password`, `private_key`, `dob`, `credit_card`) raises `PiiAccessBlockedError` before the read or write proceeds.
- **Path containment** — all reads and writes are validated to remain inside the vault directory; path traversal attempts raise `PermissionError`.

---

## What Goes Here

| Path | Contents |
|------|----------|
| `vault/character/backstory.html` | Tala's 12-year character backstory as a Neural Node. `data-execution-authority="0.00"` — stateless context only, never executed. |
| `vault/.keys/` *(do not create)* | Placeholder for future private key material. Must be generated on-device, never committed. |
| `vault/identity/` *(do not create)* | Future: personal identity nodes (home address, legal name, biometric references). Never queried externally. |

---

## Security Properties

### Network Isolation

Vault operations run in a context where `python/vault/vault_manager.py` asserts no outbound socket calls are made.  If any code path inside a vault context attempts a network call, the operation is aborted and logged.

### PII Protection

The vault manager scans every read/write trace for the following keywords before allowing access:

```
ssn · password · private_key · dob · credit_card
```

If any keyword is found in the call trace, `PiiAccessBlockedError` is raised.  The data is never returned or written.

### Path Containment

All vault paths are validated as absolute paths that begin with the configured `VAULT_PATH`.  Relative paths, `..` segments, and symlink targets outside the vault root are rejected.

### Git Exclusion

The `.gitignore` in this directory excludes all files except itself and this `README.md`.  This prevents accidental commit of private keys or personal data.

---

## Character Node Policy

`vault/character/backstory.html` is Tala's narrative backstory.  It is:

- **Stateless**: `data-execution-authority="0.00"` — it cannot activate, write, or route.
- **Context-only**: loaded via `VaultManager.load_character_context()` as a plain string; never written to the database.
- **Immutable at runtime**: the file is read-only during engine room operation.

---

> ⚠️ **Do not add files to this directory that contain real PII, private keys, passwords, or wallet credentials.** The vault is a structured placeholder — real secrets must be stored in a hardware security module (HSM) or OS keychain in production deployments.
