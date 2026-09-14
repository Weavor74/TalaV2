"""
The CSP has to permit what the renderer actually loads.

Text-to-speech worked end to end -- SAPI synthesised, voice.speak returned
base64, the Audio element was constructed -- and nothing played, because the
policy has no ``media-src`` and so fell back to ``default-src 'self'``. A
``data:`` URI is not 'self'. The only symptom was a DOMException in the
console, which reads as a broken voice backend rather than a policy decision.

Two copies of the policy exist and must agree: a <meta> tag in index.html and
a response header set in main.js for file:// loads. Whichever is stricter wins
at runtime, so a directive added to one and forgotten in the other is the same
outage with a harder trail.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "renderer" / "index.html"
MAIN = ROOT / "electron" / "main.js"

#: Directives whose absence silently breaks a feature the UI ships.
#: value -> what stops working without it.
REQUIRED = {
    "img-src": "inline images and generated screenshots",
    "media-src": "text-to-speech playback (voice.speak returns a data: URI)",
}


def _meta_policy() -> str:
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(
        r'http-equiv="Content-Security-Policy"\s*content="(.*?)"',
        html, re.S | re.I,
    )
    assert m, "no CSP <meta> found in index.html"
    return " ".join(m.group(1).split())


def _header_policy() -> str:
    js = MAIN.read_text(encoding="utf-8")
    m = re.search(r"responseHeaders\['Content-Security-Policy'\]\s*=\s*\[(.*?)\];", js, re.S)
    assert m, "no CSP response header found in main.js"
    # Concatenated string literals, with // comments interleaved.
    parts = re.findall(r'"([^"]*)"', m.group(1))
    return " ".join("".join(parts).split())


def _directive(policy: str, name: str) -> str | None:
    for chunk in policy.split(";"):
        chunk = chunk.strip()
        if chunk and chunk.split(" ")[0] == name:
            return chunk
    return None


def test_both_policies_allow_data_uris_where_the_ui_uses_them():
    for label, policy in (("index.html <meta>", _meta_policy()),
                          ("main.js header", _header_policy())):
        for name, breaks in REQUIRED.items():
            directive = _directive(policy, name)
            assert directive is not None, (
                f"{label} has no {name}; it falls back to default-src and "
                f"breaks {breaks}"
            )
            assert "data:" in directive, (
                f"{label}: {name} does not allow data:, which breaks {breaks}"
            )


def test_the_two_policies_carry_the_same_directive_names():
    """They need not be character-identical -- the header has a file: frame
    source the meta tag does not -- but a directive present in only one is
    drift, and the stricter copy silently wins."""
    meta = {c.strip().split(" ")[0] for c in _meta_policy().split(";") if c.strip()}
    header = {c.strip().split(" ")[0] for c in _header_policy().split(";") if c.strip()}
    assert meta == header, (
        "CSP directives differ between index.html and main.js:\n"
        f"  only in index.html: {sorted(meta - header) or 'none'}\n"
        f"  only in main.js:    {sorted(header - meta) or 'none'}"
    )
