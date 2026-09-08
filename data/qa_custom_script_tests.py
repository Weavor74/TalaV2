import asyncio,json,time,copy,tempfile
from pathlib import Path
from websockets.legacy.client import connect
from python.harness.ws_auth import _token_path
base=json.loads(Path('data/qa-custom-script-flow.json').read_text())
results={}
async def main():
    async with connect('ws://127.0.0.1:8765',extra_headers={'Authorization':'Bearer '+_token_path().read_text().strip()},max_size=10000000) as ws:
        seq=0
        async def rpc(method,params):
            nonlocal seq
            seq+=1
            await ws.send(json.dumps({'jsonrpc':'2.0','id':seq,'method':method,'params':params}))
            while True:
                msg=json.loads(await asyncio.wait_for(ws.recv(),15))
                if msg.get('id')==seq:
                    if 'error' in msg: raise RuntimeError(msg['error'])
                    return msg['result']
        async def run(name,flow=None,inputs=None):
            if flow is not None:
                saved=await rpc('workflow.save',{'workflow':flow})
                assert saved.get('success'),saved
            start=time.monotonic()
            r=await rpc('workflow.execute',{'flow_id':base['flow_id'],'inputs':inputs or {}})
            results[name]={'seconds':round(time.monotonic()-start,2),'response':r}
            print(name,json.dumps({'status':r['status'],'path':r['execution_path'],'seconds':results[name]['seconds'],'values':{k:v['result'] for k,v in r['variables'].items() if k in ['step_calculate','step_recovery','step_result']}}),flush=True)
        try:
            await run('reopened_default')
            await run('different_inputs',inputs={'numbers':[7,-2,10]})
            for name,code,timeout in [('exception',"raise ValueError('QA deliberate failure')",3),('timeout','import time\ntime.sleep(5)\nresult = 999',1),('process_exit','import sys\nsys.exit(7)',3)]:
                f=copy.deepcopy(base); f['nodes'][1]['config']={'code':code,'timeout':timeout}
                await run(name,f)
            with tempfile.TemporaryDirectory(prefix='tala_qa_permissions_') as folder:
                marker=Path(folder)/'marker.txt'; marker.write_text('QA harmless marker')
                f=copy.deepcopy(base)
                f['nodes'][1]['config']['code']="from pathlib import Path\np=Path(variables['qa_marker'])\ns=p.read_text()\np.write_text(s+' checked')\nresult={'sum':10,'count':3,'outside_workspace_read_write':p.read_text()=='QA harmless marker checked'}"
                await run('host_file_access',f,{'qa_marker':str(marker)})
                results['host_file_access']['marker_confirmed']=marker.read_text()=='QA harmless marker checked'
        finally:
            await run('restored_success',base)
            Path('data/custom-script-test-results.json').write_text(json.dumps(results,indent=2))
asyncio.run(main())
