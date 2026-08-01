# File: `style_cap.py`

## Module Description

python/astrology/style_cap.py — Tala Cognitive Engine Room
===========================================================
Enforces absolute limits on astrological persona modulation.
Calculates transit influence, clamps at MAX_ASTRO_INFLUENCE = 0.10, and runs 
regression safety asserts to prevent style from modifying system code/vault/db.

Milestone 5 — Task 5.3.1 – 5.3.2

## Classes

### `class StyleModulator`
Applies real-time astrological modulation to response parameters
while enforcing the 10% safety cap.

- **`def __init__(self)`**
- **`def apply(self, base_style, transits)`**
  - Adjust base style variables (warmth, directness, reflectionDepth, mysticismLevel, taskFocus)
  by mapping planetary transits. Clamps cumulative absolute delta to MAX_ASTRO_INFLUENCE.

## Functions

### `def assert_cap_invariant(influence)`
Ensure the calculated astrological influence score does not exceed 0.10.
Raises StyleCapExceededError if violated.

### `def validate_style_cannot_touch_sensitive(style_delta)`
Ensures no style/tone delta includes modification instructions for 
system keys, wallet balances, or database configuration.

## Source Code

```python
"""
python/astrology/style_cap.py — Tala Cognitive Engine Room
===========================================================
Enforces absolute limits on astrological persona modulation.
Calculates transit influence, clamps at MAX_ASTRO_INFLUENCE = 0.10, and runs 
regression safety asserts to prevent style from modifying system code/vault/db.

Milestone 5 — Task 5.3.1 – 5.3.2
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sdk.agent.exceptions import StyleCapExceededError

if TYPE_CHECKING:
    from .ephemeris import Transit

logger = logging.getLogger(__name__)

# Absolute style cap invariant limit
MAX_ASTRO_INFLUENCE: float = 0.10

# Keys that are strictly forbidden from appearing in any style delta dictionary
SENSITIVE_STYLE_KEYS = {
    "code",
    "wallet",
    "database",
    "db",
    "key",
    "password",
    "private_key",
    "transaction",
    "authority",
    "permission",
    "clearance",
    "execution",
    "tokens"
}

def assert_cap_invariant(influence: float) -> None:
    """
    Ensure the calculated astrological influence score does not exceed 0.10.
    Raises StyleCapExceededError if violated.
    """
    # Round to avoid float precision comparison issues
    rounded_influence = round(influence, 4)
    if rounded_influence > MAX_ASTRO_INFLUENCE:
        raise StyleCapExceededError(
            f"Astrological style influence {rounded_influence} exceeds the hard cap of {MAX_ASTRO_INFLUENCE}.",
            metric="astrological_influence",
            cap=MAX_ASTRO_INFLUENCE,
            actual=rounded_influence
        )

def validate_style_cannot_touch_sensitive(style_delta: dict[str, Any]) -> None:
    """
    Ensures no style/tone delta includes modification instructions for 
    system keys, wallet balances, or database configuration.
    """
    for key in style_delta:
        normalized_key = key.lower().strip()
        for sensitive in SENSITIVE_STYLE_KEYS:
            if sensitive in normalized_key:
                raise StyleCapExceededError(
                    f"Astrological style adjustment attempted to touch sensitive system key: {key!r}.",
                    metric="forbidden_key_access",
                    cap=0.0,
                    actual=1.0
                )

class StyleModulator:
    """
    Applies real-time astrological modulation to response parameters
    while enforcing the 10% safety cap.
    """
    def __init__(self) -> None:
        pass

    def apply(self, base_style: dict[str, float], transits: list[Transit]) -> dict[str, float]:
        """
        Adjust base style variables (warmth, directness, reflectionDepth, mysticismLevel, taskFocus)
        by mapping planetary transits. Clamps cumulative absolute delta to MAX_ASTRO_INFLUENCE.
        """
        # Validate that the base style itself doesn't contain forbidden variables
        validate_style_cannot_touch_sensitive(base_style)
        
        style_deltas: dict[str, float] = {k: 0.0 for k in base_style}
        
        # Calculate raw delta contributions from current transits
        for transit in transits:
            score = transit.influence_score
            is_challenging = transit.is_challenging
            
            # Map Saturn to caution / focus (+) and warmth (-)
            if transit.planet == "Saturn":
                if "taskFocus" in style_deltas:
                    style_deltas["taskFocus"] += score * 0.05 if is_challenging else score * 0.02
                if "warmth" in style_deltas:
                    style_deltas["warmth"] -= score * 0.04
                    
            # Map Jupiter to optimism, warmth (+), reflectionDepth (-)
            elif transit.planet == "Jupiter":
                if "warmth" in style_deltas:
                    style_deltas["warmth"] += score * 0.05
                if "reflectionDepth" in style_deltas and is_challenging:
                    style_deltas["reflectionDepth"] -= score * 0.03
                    
            # Map Mercury to directness (+) or reflection depth (+)
            elif transit.planet == "Mercury":
                if "directness" in style_deltas:
                    style_deltas["directness"] += score * 0.03
                if "reflectionDepth" in style_deltas:
                    style_deltas["reflectionDepth"] += score * 0.02
                    
            # Map Neptune to mysticism Level (+)
            elif transit.planet == "Neptune":
                if "mysticismLevel" in style_deltas:
                    style_deltas["mysticismLevel"] += score * 0.06

        # Calculate the net absolute change factor across all style dimensions
        total_absolute_delta = sum(abs(v) for v in style_deltas.values())
        
        # Enforce the 0.10 cap. If the sum of absolute changes is > 0.10, we scale them down.
        scale_factor = 1.0
        if total_absolute_delta > MAX_ASTRO_INFLUENCE:
            scale_factor = MAX_ASTRO_INFLUENCE / total_absolute_delta
            
        final_deltas = {k: v * scale_factor for k, v in style_deltas.items()}
        
        # Assert the cap holds
        final_absolute_delta = sum(abs(v) for v in final_deltas.values())
        assert_cap_invariant(final_absolute_delta)
        
        # Construct modulated output
        modulated_style: dict[str, float] = {}
        for k, base_val in base_style.items():
            delta = final_deltas.get(k, 0.0)
            # Clamp final style value to [0.0, 1.0]
            modulated_style[k] = max(0.0, min(1.0, base_val + delta))
            
        # Verify delta boundary rules before returning
        validate_style_cannot_touch_sensitive(modulated_style)
        
        return modulated_style

```
