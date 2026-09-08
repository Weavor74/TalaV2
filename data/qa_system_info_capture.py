import asyncio,json
from pathlib import Path
from websockets.legacy.client import connect
from python.harness.ws_auth import _token_path
async def main():
    token=_token_path().read_text().strip()
    async with connect('ws://127.0.0.1:8765',extra_headers={'Authorization':'Bearer '+token},max_size=10000000) as ws:
        await ws.send(json.dumps({'jsonrpc':'2.0','id':'qa-info','method':'workflow.execute','params':{'flow_id':'8cedcb9e-5fee-4ea0-9ff8-65c5c5882ea5'}}))
        while True:
            msg=json.loads(await asyncio.wait_for(ws.recv(),60))
            if msg.get('id')=='qa-info':
                Path('data/system-info-workflow-response.json').write_text(json.dumps(msg,indent=2))
                print(json.dumps(msg,indent=2))
                break
asyncio.run(main())
