"""NAME
       qa_ollama_vision_only - exercise local vision without desktop input
DESCRIPTION
       Sends a saved screenshot to the actual controller and prints its proposal.
"""
import asyncio
import base64
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from PIL import Image
from sdk.agent.desktop_agent import _decide

async def main():
    path = Path('data/script-ui-final.png')
    size = Image.open(path).size
    shot = {'data': base64.b64encode(path.read_bytes()).decode(), 'width': size[0], 'height': size[1], 'windows': []}
    async with httpx.AsyncClient(timeout=180) as client:
        result = await _decide(client, 'http://localhost:11434', 'qwen2.5vl:3b', 'Read the visible application title and report it as evidence. This is a read-only inspection; choose done if readable or blocked if not. Do not click or type.', shot, [], cpu_only=True)
        print(result.model_dump_json(), flush=True)
        Path('data/qa-ollama-vision-result.json').write_text(result.model_dump_json(), encoding='utf-8')

asyncio.run(main())
