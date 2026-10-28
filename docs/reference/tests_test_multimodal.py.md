# File: `test_multimodal.py`

## Functions

### `async def test_multimodal_headless_fallback(mock_settings)`
Ensure that multimodal functions return an error in headless mode.

### `async def test_multimodal_missing_deps(mock_settings)`
Ensure that multimodal functions return an error if dependencies are missing.

## Source Code

```python
import pytest
import asyncio
from unittest.mock import patch

from python.harness.multimodal import get_screen_context

@pytest.mark.asyncio
@patch("python.harness.multimodal.settings")
async def test_multimodal_headless_fallback(mock_settings):
    """Ensure that multimodal functions return an error in headless mode."""
    mock_settings.IS_HEADLESS = True
    
    res_screen = await get_screen_context()
    assert "error" in res_screen
    assert "headless" in res_screen["error"].lower()

@pytest.mark.asyncio
@patch("python.harness.multimodal.settings")
@patch("python.harness.multimodal.pyautogui", None) # Mock missing dependencies
async def test_multimodal_missing_deps(mock_settings):
    """Ensure that multimodal functions return an error if dependencies are missing."""
    mock_settings.IS_HEADLESS = False
    
    res_screen = await get_screen_context()
    assert "error" in res_screen
    assert "not installed" in res_screen["error"].lower()


```
