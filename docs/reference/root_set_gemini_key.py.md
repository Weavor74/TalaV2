# File: `set_gemini_key.py`

## Functions

### `async def main()`
*No docstring provided.*

## Source Code

```python
import sys
import asyncio
import asyncpg
import json

async def main():
    if len(sys.argv) < 2:
        print("Usage: python set_gemini_key.py <YOUR_API_KEY>")
        sys.exit(1)
        
    api_key = sys.argv[1]
    
    # Use the local DB connection
    conn = await asyncpg.connect("postgresql://tala:tala@localhost:5432/tala")
    
    try:
        # Find the Gemini provider
        row = await conn.fetchrow("SELECT id, config FROM inference_providers WHERE name = 'Gemini'")
        if not row:
            print("Could not find a provider named 'Gemini' in the database.")
            return
            
        provider_id = row['id']
        config_str = row['config']
        config = json.loads(config_str) if isinstance(config_str, str) else config_str
        
        # Update the config
        config['service'] = 'gemini'
        config['model'] = 'gemini-1.5-pro'
        config['apiKey'] = api_key
        
        # Save back
        await conn.execute(
            "UPDATE inference_providers SET config = $1 WHERE id = $2",
            json.dumps(config),
            provider_id
        )
        print("Successfully updated Gemini API key in the database!")
        print("Please restart Tala for the changes to take effect.")
        
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())

```
