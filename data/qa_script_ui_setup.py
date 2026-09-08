import json
from pathlib import Path
flow={'schema':'tala-flow/1.1','flow_id':'10fafd7f-b453-421e-96ea-fae19967c02a','name':'QA Custom Scripts 2026-09-08','nodes':[
{'id':'start','type':'trigger','label':'Start','config':{},'connections':{'onSuccess':'calculate'},'position':{'x':40,'y':80}},
{'id':'calculate','type':'custom_script','label':'Calculate','config':{'code':"numbers = variables.get('numbers', [2, 3, 5])\nprint('QA input:', numbers)\nresult = {'sum': sum(numbers), 'count': len(numbers)}",'timeout':3},'connections':{'onSuccess':'double','onFailure':'recovery'},'position':{'x':270,'y':80}},
{'id':'double','type':'custom_script','label':'Double and check','config':{'code':"prior = variables['step_calculate']['result']['script_result']\nresult = {'total': prior['sum'] * 2, 'count': prior['count'], 'checked': prior['sum'] == sum(variables.get('numbers', [2, 3, 5]))}",'timeout':3},'connections':{'onSuccess':'result'},'position':{'x':510,'y':80}},
{'id':'result','type':'output','label':'Result','config':{'value':'{{step_double.result.script_result}}','verify':'{{step_double.result.script_result.checked}}'},'connections':{},'position':{'x':750,'y':80}},
{'id':'recovery','type':'custom_script','label':'Failure handler','config':{'code':"result = {'handled': True, 'error': variables['step_calculate']['message']}",'timeout':3},'connections':{},'position':{'x':270,'y':280}}
]}
Path('data/qa-custom-script-flow.json').write_text(json.dumps(flow,indent=2))
import pyperclip,pyautogui,time
pyperclip.copy(json.dumps(flow,indent=2))
pyautogui.click(770,610); pyautogui.hotkey('ctrl','a'); pyautogui.hotkey('ctrl','v'); time.sleep(.4)
pyautogui.click(1230,380); time.sleep(.4)
pyautogui.screenshot().save('data/script-ui-loaded.png')
