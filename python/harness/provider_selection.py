"""
python/harness/provider_selection.py -- which providers a request may try.

One decision lives here, pulled out of server.py's generate loop because
that file is pinned by the size ratchet (tests/test_size_ratchet.py) and
because the decision is a predicate worth reading on its own.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def skip_provider_attempt(
    provider: dict[str, Any],
    forced_provider: dict[str, Any] | None,
    request_provider: dict[str, Any] | None,
) -> bool:
    """
    Whether this provider should be passed over without being tried.

    A provider the caller asked for by id is exempt from the offline skip,
    exactly as a forced one is.

    `provider_id` put the requested provider first in the chain and then let
    this skip drop it, so naming a provider whose cached status was "offline"
    silently produced an answer from a different model. Every desktop-control
    run aimed at Gemini was served by a local qwen instead, and the reply
    looked like an ordinary success -- I reported one of those to Steven as a
    Gemini result before reading the log and finding otherwise.

    `status` is a cached probe, not ground truth: it goes stale, and it is
    exactly what an explicit choice should be able to override. If the
    provider really is down the request fails against it and the chain carries
    on to the next one, which is the behaviour a caller naming a provider
    expects.
    """
    if provider.get("status") != "offline":
        return False

    # Checked ahead of the embedded-builtin exemption so that the "requested
    # by id" line is logged for exactly the providers it was logged for
    # before this moved out of server.py.
    if bool(request_provider) and provider["id"] == request_provider["id"]:
        logger.info(
            "Provider '%s' is marked offline but was requested by id; trying it.",
            provider["name"],
        )
        return False

    if provider["id"] == "embedded-builtin":
        return False
    if bool(forced_provider) and provider["id"] == forced_provider["id"]:
        return False

    logger.info("Skipping offline provider '%s'", provider["name"])
    return True
