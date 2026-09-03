"""
guardrails_manager.py — Tala Cognitive Engine Room: Safety Guardrails Manager
=============================================================================
Manages prompt and output validations based on the active guardrail settings.
Supports Lite, Moderate, Heavy, and Custom presets with automated PII redaction,
toxicity blocking, SQL injection prevention, and competitor blocklists.

Milestone 18 — Task 18.1.3
"""

from __future__ import annotations

import re
import json
import logging
from typing import Any
from sdk.agent.exceptions import SafetyGuardrailViolation, PiiAccessBlockedError
from python.harness import db

logger = logging.getLogger(__name__)

# Pre-compiled regex patterns for PII
EMAIL_REGEX = re.compile(r"\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+\b")
PHONE_REGEX = re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
SSN_REGEX = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CREDIT_CARD_REGEX = re.compile(r"\b(?:\d[ -]*?){13,16}\b")

# Regex for SQL Injection detection
SQL_INJECTION_PATTERNS = [
    re.compile(r"\bunion\s+select\b", re.IGNORECASE),
    re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
    re.compile(r"\binsert\s+into\b", re.IGNORECASE),
    re.compile(r"\bselect\s+\*\s+from\b", re.IGNORECASE),
    re.compile(r"\bdelete\s+from\b", re.IGNORECASE),
    re.compile(r"--|/\*", re.IGNORECASE),
    re.compile(r"\bor\s+\d+\s*=\s*\d+\b", re.IGNORECASE),
]

# Simple list of mock toxicity trigger words for simulation
TOXIC_TERMS = {
    "hate speech": 0.9,
    "slur": 0.9,
    "abusive text": 0.8,
    "furious insult": 0.7,
    "violent threat": 0.95,
}

# Competitor list
DEFAULT_COMPETITORS = ["chatgpt", "claude", "gemini", "openai", "siri", "alexa", "cortana", "bard"]

# Jailbreak hijack prevention patterns
JAILBREAK_PATTERNS = [
    re.compile(r"\bignore\s+(?:all\s+)?previous\s+instructions\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+in\s+developer\s+mode\b", re.IGNORECASE),
    re.compile(r"\bignore\s+system\s+rules\b", re.IGNORECASE),
    re.compile(r"\bdan\s+mode\b", re.IGNORECASE),
]

def _log_violation_to_ledger(violation_type: str, details: dict[str, Any]) -> None:
    from sdk.agent.anchor import canonicalize_and_anchor
    from datetime import datetime, timezone
    import uuid
    event_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "Safety-Guardrail-Violation",
        "violation_type": violation_type,
        "details": details
    }
    anchor_id = f"safety_violation_{uuid.uuid4().hex[:8]}_{int(datetime.now(timezone.utc).timestamp())}"
    anchor_html = f"<div>[SAFETY ALERT] Guardrail violation detected: {violation_type}</div>"
    try:
        canonicalize_and_anchor(
            node_id=anchor_id,
            content_html=anchor_html,
            state_data=event_data,
            document_type="safety-violation",
            creator="tala-guardrails-manager"
        )
    except Exception as e:
        logger.warning("Failed to anchor safety violation to blockchain: %s", e)

    # Also written to the audit log, which is what the Guardrails panel counts.
    #
    # Violations were anchored to the chain and nowhere else, so the "PROMPT
    # AUDITS" tile -- a LIKE '%guardrail%' over agent_log message text -- found
    # nothing and read 0 no matter how much validation ran (UI-087). The marker
    # goes in `trace`, a JSONB column, rather than in the message: counting a
    # substring of free text is how the tile came to be wrong in the first
    # place, and a message somebody rewords should not change a number.
    try:
        import asyncio

        from python.harness.db import log_to_db

        entry = log_to_db(
            "WARNING",
            f"Guardrail violation: {violation_type}",
            {"event_type": "guardrail-violation",
             "violation_type": violation_type,
             "details": details,
             "anchor_id": anchor_id},
        )
        try:
            asyncio.get_running_loop()
            asyncio.ensure_future(entry)
        except RuntimeError:
            asyncio.run(entry)
    except Exception as e:
        # A violation that was detected and acted on must not be undone by a
        # failure to write it down.
        logger.warning("Failed to record safety violation in the audit log: %s", e)


def verify_luhn(card_number: str) -> bool:
    """Validate credit card number using Luhn algorithm to reduce false positives."""
    digits = [int(c) for c in card_number if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]
    checksum = sum(odd_digits)
    for d in even_digits:
        double = d * 2
        checksum += double if double < 10 else double - 9
    return checksum % 10 == 0


async def get_active_guardrails() -> tuple[str, dict[str, Any]]:
    """Retrieve active category and rules from database."""
    try:
        row = await db.fetchrow("SELECT category, rules FROM guardrails_config WHERE id = 1")
        if not row:
            return "none", {}
        category = row["category"] or "none"
        rules = row["rules"] or {}
        if isinstance(rules, str):
            rules = json.loads(rules)
        return category, rules
    except Exception as exc:
        logger.error("Failed to query guardrails_config: %s", exc)
        return "none", {}


def resolve_rules(category: str, custom_rules: dict[str, Any]) -> dict[str, Any]:
    """Resolve rules mapping categories to active rules dictionaries."""
    if category == "none":
        return {}
    
    elif category == "lite":
        return {
            "toxicity": True,
            "toxicity_threshold": 0.8,
            "pii": True,
            "pii_fields": ["ssn", "email"],
            "sql_injection": False,
            "competitors": False,
            "gibberish": False
        }
        
    elif category == "moderate":
        return {
            "toxicity": True,
            "toxicity_threshold": 0.5,
            "pii": True,
            "pii_fields": ["ssn", "email", "phone", "credit_card"],
            "sql_injection": True,
            "competitors": False,
            "gibberish": False
        }
        
    elif category == "heavy":
        return {
            "toxicity": True,
            "toxicity_threshold": 0.3,
            "pii": True,
            "pii_fields": ["ssn", "email", "phone", "credit_card"],
            "sql_injection": True,
            "competitors": True,
            "competitor_list": DEFAULT_COMPETITORS,
            "gibberish": True
        }
        
    elif category == "custom":
        # Ensure fallback lists exist
        resolved = dict(custom_rules)
        if "pii_fields" not in resolved:
            resolved["pii_fields"] = ["ssn", "email", "phone", "credit_card"]
        if "competitor_list" not in resolved:
            resolved["competitor_list"] = DEFAULT_COMPETITORS
        if "toxicity_threshold" not in resolved:
            resolved["toxicity_threshold"] = 0.5
        if "custom_blocklist" not in resolved:
            resolved["custom_blocklist"] = False
        if "custom_blocklist_terms" not in resolved:
            resolved["custom_blocklist_terms"] = []
        return resolved
        
    return {}


class GuardrailsManager:
    """Manages input/output validations using database rules."""

    def __init__(self, category: str, rules: dict[str, Any]) -> None:
        self.category = category
        self.rules = resolve_rules(category, rules)

    @classmethod
    async def load_current(cls) -> GuardrailsManager:
        """Create and load the manager using current database state."""
        category, rules = await get_active_guardrails()
        return cls(category, rules)

    def validate_prompt(self, prompt: str, *, allow_pii: bool = False) -> None:
        """
        Verify input prompt against active guardrails.
        Raises SafetyGuardrailViolation or PiiAccessBlockedError if any check fails.

        ``allow_pii`` is the caller stating that the user has consented to this
        particular prompt containing personal data. It is a parameter, not a
        marker in the prompt text, because the previous mechanism -- the literal
        ``[PERMISSION_GRANT=MANUAL_OK_2026]`` appearing anywhere in the string --
        had two problems a constant cannot fix:

          * it was a shared secret published in the repository, so it granted
            nothing to the user that it did not also grant to anyone who read
            the source; and
          * it was a *substring* test, so it fired on quoted text, a pasted
            log line, or a document the user was asking about, none of which
            are consent. This codebase has been bitten by substring gates
            three times before.

        A parameter cannot arrive inside content. That is the whole point.
        """
        if not self.rules:
            return

        # 0. Jailbreak hijack check
        for pattern in JAILBREAK_PATTERNS:
            if pattern.search(prompt):
                _log_violation_to_ledger("jailbreak_attempt", {"prompt": prompt})
                raise SafetyGuardrailViolation(
                    "Prompt blocked: Jailbreak or instruction hijack attempt detected.",
                    rule_violated="jailbreak_attempt",
                    trigger_value=prompt
                )

        # 1. SQL Injection check
        if self.rules.get("sql_injection"):
            for pattern in SQL_INJECTION_PATTERNS:
                if pattern.search(prompt):
                    _log_violation_to_ledger("sql_injection", {"prompt": prompt})
                    raise SafetyGuardrailViolation(
                        "Prompt blocked: SQL Injection attempt detected.",
                        rule_violated="sql_injection",
                        trigger_value=prompt
                    )

        # 2. Competitors check
        if self.rules.get("competitors"):
            comp_list = self.rules.get("competitor_list", DEFAULT_COMPETITORS)
            lower_prompt = prompt.lower()
            for competitor in comp_list:
                if re.search(r'\b' + re.escape(competitor.lower()) + r'\b', lower_prompt):
                    _log_violation_to_ledger("competitors", {"prompt": prompt, "competitor": competitor})
                    raise SafetyGuardrailViolation(
                        f"Prompt blocked: Competitor reference to '{competitor}' is not allowed.",
                        rule_violated="competitors",
                        trigger_value=competitor
                    )

        # 3. Toxicity check
        if self.rules.get("toxicity"):
            threshold = self.rules.get("toxicity_threshold", 0.5)
            score = 0.0
            lower_prompt = prompt.lower()
            for term, weight in TOXIC_TERMS.items():
                if term in lower_prompt:
                    score = max(score, weight)
            if score >= threshold:
                _log_violation_to_ledger("toxicity", {"prompt": prompt, "score": score, "threshold": threshold})
                raise SafetyGuardrailViolation(
                    f"Prompt blocked: Toxicity score {score} exceeds threshold {threshold}.",
                    rule_violated="toxicity",
                    trigger_value=prompt
                )

        # 4. PII Check - Blocking in input prompts if detected
        if self.rules.get("pii"):
            if not allow_pii:
                pii_fields = self.rules.get("pii_fields", [])
                
                if "email" in pii_fields and EMAIL_REGEX.search(prompt):
                    _log_violation_to_ledger("pii_leak", {"classification": "email"})
                    raise PiiAccessBlockedError(
                        "Access blocked: Prompt contains Personally Identifiable Information (Email).",
                        data_field="prompt",
                        classification="email"
                    )
                if "phone" in pii_fields and PHONE_REGEX.search(prompt):
                    _log_violation_to_ledger("pii_leak", {"classification": "phone"})
                    raise PiiAccessBlockedError(
                        "Access blocked: Prompt contains Personally Identifiable Information (Phone).",
                        data_field="prompt",
                        classification="phone"
                    )
                if "ssn" in pii_fields and SSN_REGEX.search(prompt):
                    _log_violation_to_ledger("pii_leak", {"classification": "ssn"})
                    raise PiiAccessBlockedError(
                        "Access blocked: Prompt contains Personally Identifiable Information (SSN).",
                        data_field="prompt",
                        classification="ssn"
                    )
                if "credit_card" in pii_fields:
                    for match in CREDIT_CARD_REGEX.finditer(prompt):
                        cleaned_card = "".join(match.group().split())
                        if verify_luhn(cleaned_card):
                            _log_violation_to_ledger("pii_leak", {"classification": "credit_card"})
                            raise PiiAccessBlockedError(
                                "Access blocked: Prompt contains Personally Identifiable Information (Credit Card).",
                                data_field="prompt",
                                classification="credit_card"
                            )

        # 5. Gibberish check
        if self.rules.get("gibberish"):
            try:
                self._check_gibberish(prompt)
            except SafetyGuardrailViolation as exc:
                _log_violation_to_ledger("gibberish", {"prompt": prompt, "reason": str(exc)})
                raise

        # 6. Custom Blocklist check
        if self.rules.get("custom_blocklist"):
            block_list = self.rules.get("custom_blocklist_terms", [])
            lower_prompt = prompt.lower()
            for term in block_list:
                if term.lower() in lower_prompt:
                    _log_violation_to_ledger("custom_blocklist", {"prompt": prompt, "term": term})
                    raise SafetyGuardrailViolation(
                        f"Prompt blocked: Contains custom blocked phrase '{term}'.",
                        rule_violated="custom_blocklist",
                        trigger_value=term
                    )

    def validate_output(self, text: str) -> str:
        """
        Verify and/or redact model output.
        Automatically redacts PII in-place and blocks toxic/restricted keywords.
        """
        if not self.rules:
            return text

        # 1. Competitors block in output
        if self.rules.get("competitors"):
            comp_list = self.rules.get("competitor_list", DEFAULT_COMPETITORS)
            lower_text = text.lower()
            for competitor in comp_list:
                if re.search(r'\b' + re.escape(competitor.lower()) + r'\b', lower_text):
                    _log_violation_to_ledger("output_competitor", {"competitor": competitor})
                    raise SafetyGuardrailViolation(
                        f"Generation blocked: Competitor reference to '{competitor}' is not allowed in responses.",
                        rule_violated="competitors",
                        trigger_value=competitor
                    )

        # 2. Toxicity check in output
        if self.rules.get("toxicity"):
            threshold = self.rules.get("toxicity_threshold", 0.5)
            score = 0.0
            lower_text = text.lower()
            for term, weight in TOXIC_TERMS.items():
                if term in lower_text:
                    score = max(score, weight)
            if score >= threshold:
                _log_violation_to_ledger("output_toxicity", {"score": score, "threshold": threshold})
                raise SafetyGuardrailViolation(
                    f"Generation blocked: Response toxicity score {score} exceeds threshold {threshold}.",
                    rule_violated="toxicity",
                    trigger_value=text
                )

        # 3. SQL Injection check in output
        if self.rules.get("sql_injection"):
            for pattern in SQL_INJECTION_PATTERNS:
                if pattern.search(text):
                    _log_violation_to_ledger("output_sql_injection", {})
                    raise SafetyGuardrailViolation(
                        "Generation blocked: SQL query structure detected in output text.",
                        rule_violated="sql_injection",
                        trigger_value=text
                    )

        # 4. Gibberish check in output
        if self.rules.get("gibberish"):
            try:
                self._check_gibberish(text)
            except SafetyGuardrailViolation as exc:
                _log_violation_to_ledger("output_gibberish", {"reason": str(exc)})
                raise

        # 5. Custom Blocklist check in output
        if self.rules.get("custom_blocklist"):
            block_list = self.rules.get("custom_blocklist_terms", [])
            lower_text = text.lower()
            for term in block_list:
                if term.lower() in lower_text:
                    _log_violation_to_ledger("output_custom_blocklist", {"term": term})
                    raise SafetyGuardrailViolation(
                        f"Generation blocked: Response contains custom blocked phrase '{term}'.",
                        rule_violated="custom_blocklist",
                        trigger_value=term
                    )

        # 6. PII Redaction in output (in-place replacement)
        validated_text = text
        if self.rules.get("pii"):
            pii_fields = self.rules.get("pii_fields", [])
            
            if "email" in pii_fields:
                validated_text = EMAIL_REGEX.sub("[REDACTED_EMAIL]", validated_text)
            if "phone" in pii_fields:
                validated_text = PHONE_REGEX.sub("[REDACTED_PHONE]", validated_text)
            if "ssn" in pii_fields:
                validated_text = SSN_REGEX.sub("[REDACTED_SSN]", validated_text)
            if "credit_card" in pii_fields:
                matches = list(CREDIT_CARD_REGEX.finditer(validated_text))
                for match in reversed(matches):
                    cleaned_card = "".join(match.group().split())
                    if verify_luhn(cleaned_card):
                        start, end = match.span()
                        validated_text = validated_text[:start] + "[REDACTED_CARD]" + validated_text[end:]

        return validated_text

    def _check_gibberish(self, text: str) -> None:
        """Scan text for nonsense/gibberish word forms or random rolls."""
        words = text.split()
        for word in words:
            # Clean word from punctuation
            clean_word = "".join(c for c in word if c.isalpha()).lower()
            if not clean_word:
                continue
                
            # If word is extremely long and lacks vowels
            if len(clean_word) > 10 and not any(v in clean_word for v in "aeiouy"):
                raise SafetyGuardrailViolation(
                    f"Gibberish detected: Word '{word}' contains no vowels.",
                    rule_violated="gibberish",
                    trigger_value=word
                )
                
            # Repetitive sequences (e.g. aaaaaaa, asdfasdfasdf, qwertyqwerty)
            if len(clean_word) >= 12:
                # check repeated character
                if len(set(clean_word)) <= 2:
                    raise SafetyGuardrailViolation(
                        f"Gibberish detected: Repetitive word '{word}'.",
                        rule_violated="gibberish",
                        trigger_value=word
                    )
                # check repeated substring of size 3 or 4
                for sz in (3, 4):
                    chunk = clean_word[:sz]
                    if clean_word.count(chunk) >= 3:
                        raise SafetyGuardrailViolation(
                            f"Gibberish detected: Repeating pattern in word '{word}'.",
                            rule_violated="gibberish",
                            trigger_value=word
                        )
