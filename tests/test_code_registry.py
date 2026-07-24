import pytest
import asyncio
from unittest.mock import MagicMock
from sdk.agent.code_registry import CodeRegistry, DEFAULT_REGISTRY_KEYS
from sdk.agent.coder import CodeGenerator

@pytest.mark.asyncio
async def test_registry_default_seeding():
    # Seed registry
    await CodeRegistry.seed_registry()
    
    # Assert defaults are present in memory
    for key, expected_val in DEFAULT_REGISTRY_KEYS.items():
        val = await CodeRegistry.get(key)
        assert val == expected_val

@pytest.mark.asyncio
async def test_registry_get_set():
    # Set custom value
    custom_key = "tala.code.style.python.custom_key"
    await CodeRegistry.set(custom_key, "custom_value")
    
    # Get custom value
    val = await CodeRegistry.get(custom_key)
    assert val == "custom_value"

@pytest.mark.asyncio
async def test_registry_list_keys():
    # Seed
    await CodeRegistry.seed_registry()
    
    # List with prefix
    keys = await CodeRegistry.list_keys("tala.code.style.python")
    assert "tala.code.style.python.indent" in keys
    assert "tala.code.style.python.naming" in keys
    assert "tala.code.style.nodejs.indent" not in keys

@pytest.mark.asyncio
async def test_generate_template_from_registry():
    # Seed
    await CodeRegistry.seed_registry()
    
    # Override standard Python class template in registry
    test_template = "class {class_name}_Customized:\n    def __init__(self):\n        # {description}"
    await CodeRegistry.set("tala.code.template.python.class", test_template)
    
    # Generate python class
    code = await CodeGenerator.generate_template(
        "python", 
        "MyClass", 
        {"description": "My custom class description"}
    )
    
    assert "class MyClass_Customized:" in code
    assert "# My custom class description" in code
