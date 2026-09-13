"""NAME
       qa_desktop_rpc - check desktop RPC integration in the running harness
DESCRIPTION
       Uses the existing local authentication token without printing it.
"""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from websockets.legacy.client import connect
from python.harness.ws_auth import _token_path

async def main():
    async with connect('ws://127.0.0.1:8765', extra_headers={'Authorization':'Bearer '+_token_path().read_text().strip()}) as ws:
        await ws.send(json.dumps({'jsonrpc':'2.0','id':'desktop-qa','method':'desktop.stop','params':{}}))
        while True:
            response = json.loads(await asyncio.wait_for(ws.recv(),30))
            if response.get('id') == 'desktop-qa':
                print(json.dumps(response))
                break

asyncio.run(main())
