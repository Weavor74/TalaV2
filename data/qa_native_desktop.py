"""NAME
       qa_native_desktop - independently verify native input in a disposable app
DESCRIPTION
       Uses Tala's real desktop dispatcher and reads the actual Tk field value.
"""
import asyncio
import json
import sys
import threading
import tkinter as tk
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdk.agent import desktop_control as desktop

root = tk.Tk()
root.title('Tala Native Input QA')
root.geometry('700x400+300+180')
root.attributes('-topmost', True)
tk.Label(root, text='Tala Native Input QA', font=('Arial',24)).pack(pady=20)
entry = tk.Entry(root, font=('Arial',24))
entry.pack(pady=20)
status = tk.Label(root, text='Waiting', font=('Arial',22))
button = tk.Button(root, text='Verify', font=('Arial',22), command=lambda: status.config(text='PASS' if entry.get() == 'TALA-482 ✓' else 'FAIL'))
button.pack(pady=20)
status.pack()
def finish(results):
    result = {'steps': results, 'actual_text': entry.get(), 'actual_status': status.cget('text'), 'success': entry.get() == 'TALA-482 ✓' and status.cget('text') == 'PASS'}
    Path('data/qa-native-desktop-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True), flush=True)
    root.destroy()
def start():
    root.focus_force()
    points = [(w.winfo_rootx()+w.winfo_width()//2, w.winfo_rooty()+w.winfo_height()//2) for w in (entry, button)]
    async def drive():
        results = []
        for action, extra in [('click',dict(x=points[0][0],y=points[0][1])), ('type',dict(text='replace me')), ('hotkey',dict(keys=['ctrl','a'])), ('type',dict(text='TALA-482 ✓')), ('click',dict(x=points[1][0],y=points[1][1]))]:
            shot = await asyncio.to_thread(desktop._capture)
            result = await desktop.desktop_action(shot['observation_id'], action, **extra)
            results.append(result)
            if not result.get('success'):
                break
            await asyncio.sleep(0.3)
        root.after(100, finish, results)
    threading.Thread(target=lambda: asyncio.run(drive()), daemon=True).start()
root.after(1000, start)
root.mainloop()
