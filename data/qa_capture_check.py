import asyncio,json,base64
from pathlib import Path
from websockets.legacy.client import connect
from python.harness.ws_auth import _token_path
async def main():
 async with connect('ws://127.0.0.1:8765',extra_headers={'Authorization':'Bearer '+_token_path().read_text().strip()},max_size=10000000) as ws:
  await ws.send(json.dumps({'jsonrpc':'2.0','id':1,'method':'multimodal.capture_screen','params':{}}))
  while True:
   r=json.loads(await asyncio.wait_for(ws.recv(),20))
   if r.get('id')==1:
    result=r.get('result',{})
    data=result.pop('data',None)
    if data:Path('data/tala-screen-capture.jpg').write_bytes(base64.b64decode(data))
    print(json.dumps({'captured':bool(data),'response':result,'error':r.get('error')}));break
asyncio.run(main())
