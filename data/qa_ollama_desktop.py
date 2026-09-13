"""NAME
       qa_ollama_desktop - real local vision/action smoke test
DESCRIPTION
       Opens a disposable window and independently checks the resulting value.
"""
import asyncio
import json
import sys
import threading
import tkinter as tk
import logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdk.agent import desktop_agent
desktop_agent.log.setLevel(logging.INFO)
desktop_agent.log.addHandler(logging.StreamHandler(sys.stdout))
original_decide = desktop_agent._decide
async def traced_decide(*args, **kwargs):
    decision = await original_decide(*args, **kwargs)
    print('PROPOSAL ' + decision.model_dump_json(), flush=True)
    return decision
desktop_agent._decide = traced_decide

root = tk.Tk()
root.title('Tala Desktop QA')
root.geometry('800x500+250+160')
root.attributes('-topmost', True)
tk.Label(root, text='Tala Desktop QA', font=('Arial', 28)).pack(pady=25)
tk.Label(root, text='Enter TALA-482 in the field, then click Verify.', font=('Arial', 18)).pack(pady=15)
entry = tk.Entry(root, font=('Arial', 24), width=25)
entry.pack(pady=15)
status = tk.Label(root, text='Waiting', font=('Arial', 24))
def verify():
    status.config(text='PASS: TALA-482' if entry.get() == 'TALA-482' else 'FAIL', fg='green' if entry.get() == 'TALA-482' else 'red')
tk.Button(root, text='Verify', command=verify, font=('Arial', 22)).pack(pady=15)
status.pack(pady=15)
def finished(result):
    result['independent_check'] = {'entry': entry.get(), 'status': status.cget('text')}
    Path('data/qa-ollama-rpc-result.json' if '--rpc' in sys.argv else 'data/qa-ollama-desktop-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result), flush=True)
    root.destroy()
def work():
    params = dict(task='In the Tala Desktop QA window enter TALA-482 into the input field, click Verify, and confirm the visible PASS: TALA-482 result. Do not interact with other applications.', model=sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else 'qwen2.5vl:3b', max_steps=8, cpu_only=True, success_text='PASS: TALA-482')
    async def run():
        if '--rpc' not in sys.argv:
            return await desktop_agent.desktop_run(**params)
        from websockets.legacy.client import connect
        from python.harness.ws_auth import _token_path
        async with connect('ws://127.0.0.1:8765',extra_headers={'Authorization':'Bearer '+_token_path().read_text().strip()}) as ws:
            await ws.send(json.dumps({'jsonrpc':'2.0','id':'desktop-live','method':'desktop.run','params':params}))
            while True:
                reply = json.loads(await asyncio.wait_for(ws.recv(),1200))
                if reply.get('id') == 'desktop-live':
                    wrapper = reply.get('result', {})
                    return {**(wrapper.get('output') or {}), 'registry_success':wrapper.get('success'), 'rpc_error':reply.get('error') or wrapper.get('error'), 'via':'live_harness_rpc'}
    result = asyncio.run(run())
    root.after(0, finished, result)
def start():
    root.focus_force()
    threading.Thread(target=work, daemon=True).start()
root.after(1500, start)
root.mainloop()
