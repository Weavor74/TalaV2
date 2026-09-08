import asyncio,json
from pathlib import Path
from websockets.legacy.client import connect
from python.harness.ws_auth import _token_path
async def main():
 async with connect('ws://127.0.0.1:8765',extra_headers={'Authorization':'Bearer '+_token_path().read_text().strip()},max_size=10000000) as ws:
  await ws.send(json.dumps({'jsonrpc':'2.0','id':'qa-chat','method':'task.list','params':{}}))
  while True:
   r=json.loads(await asyncio.wait_for(ws.recv(),20))
   if r.get('id')=='qa-chat':
    matched=[t for t in r.get('result',{}).get('tasks',[]) if 'QA Custom Scripts' in t.get('description','')]
    Path('data/chat-workflow-tasks.json').write_text(json.dumps(matched,indent=2)); print(json.dumps(matched,indent=2));break
asyncio.run(main())
