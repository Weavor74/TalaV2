# File: `test_ollama.py`

## Functions

### `async def main()`
*No docstring provided.*

## Source Code

```python
import asyncio
import httpx

async def main():
    async with httpx.AsyncClient() as client:
        payload = {
            "model": "gemma4:e4b",
            "messages": [{"role": "user", "content": "Hello!"}],
            "max_tokens": 10
        }
        resp = await client.post("http://127.0.0.1:11434/v1/chat/completions", json=payload)
        print(f"Status: {resp.status_code}")
        print(f"Body: {resp.text}")

        # Also test with llama3.2:3b to see if it fails
        payload["model"] = "llama3.2:3b"
        resp2 = await client.post("http://127.0.0.1:11434/v1/chat/completions", json=payload)
        print(f"Llama Status: {resp2.status_code}")
        print(f"Llama Body: {resp2.text}")

if __name__ == "__main__":
    asyncio.run(main())

```
