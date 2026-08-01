# File: `test_handshake.py`

## Module Description

sdk/contracts/tests/test_handshake.py — Pytest suite for handshake module

## Functions

### `def test_is_path_like()`
*No docstring provided.*

### `def test_strip_paths()`
*No docstring provided.*

### `def test_prepare_and_receive_packet()`
*No docstring provided.*

## Source Code

```python
"""
sdk/contracts/tests/test_handshake.py — Pytest suite for handshake module
"""

from __future__ import annotations

import pytest
from sdk.contracts.handshake import (
    HandshakePacket,
    HandshakeSender,
    HandshakeReceiver,
    HandshakeVerificationError,
    _is_path_like,
    _strip_paths,
)

def test_is_path_like():
    assert _is_path_like("C:\\Program Files") is True
    assert _is_path_like("/usr/local/bin") is True
    assert _is_path_like("./relative/path") is True
    assert _is_path_like("Just text") is False
    # The generated regex matches anything with a /segment/ pattern, including URLs
    assert _is_path_like("https://example.com/api") is True

def test_strip_paths():
    data = {
        "text": "Hello world",
        "win_path": "C:\\Program Files\\app",
        "unix_path": "/var/log/syslog",
        "rel_path": "./local/file",
        "nested": {
            "path": "/home/user/doc.txt",
            "number": 42
        },
        "list_items": [
            "/var/log/",
            "not-a-path",
            {"inner_path": "/etc/inner"}
        ]
    }
    
    clean = _strip_paths(data)
    assert clean["text"] == "Hello world"
    assert "win_path" not in clean
    assert "unix_path" not in clean
    assert "rel_path" not in clean
    assert clean["nested"] == {"number": 42}
    assert clean["list_items"] == ["not-a-path", {}]

def test_prepare_and_receive_packet():
    # Setup keys: must be 32 hex chars (16 bytes)
    key_alice = "0123456789abcdef0123456789abcdef"
    
    sender = HandshakeSender()
    packet = sender.prepare_packet(
        sender_id="alice",
        recipient_id="bob",
        raw_data={"task": "do-work", "secret_path": "/etc/shadow"},
        private_key_hex=key_alice
    )
    
    assert packet.sender_id == "alice"
    assert packet.recipient_id == "bob"
    assert "secret_path" not in packet.metadata  # path firewalled
    assert len(packet.signature) == 64  # SHA-256 hex string length
    
    receiver = HandshakeReceiver()
    
    # Successful verification
    assert receiver.receive_packet(packet, {"alice": key_alice}) is True
    
    # Failure: unknown sender
    with pytest.raises(HandshakeVerificationError, match="unknown sender_id"):
        receiver.receive_packet(packet, {"charlie": key_alice})
        
    # Failure: wrong key
    wrong_key = "abcdef0123456789abcdef0123456789"
    with pytest.raises(HandshakeVerificationError, match="signature mismatch"):
        receiver.receive_packet(packet, {"alice": wrong_key})
        
    # Failure: tampered metadata
    packet.metadata["task"] = "hacked-task"
    with pytest.raises(HandshakeVerificationError, match="signature mismatch"):
        receiver.receive_packet(packet, {"alice": key_alice})

```
