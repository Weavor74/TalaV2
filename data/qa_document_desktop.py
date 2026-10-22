"""NAME
       qa_document_desktop - document the local desktop controller
DESCRIPTION
       Writes HTML-NN descriptions with source accountability.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdk.agent.documentation_nodes import create_documentation_node

description = '''NAME
desktop_run - native desktop tasks with local Ollama vision
SYNOPSIS
desktop_run(task, model="gemma3:4b", endpoint="http://localhost:11434", max_steps=20)
DESCRIPTION
Each iteration sends a fresh screenshot directly to Ollama /api/chat with a JSON action schema. The controller owns observation tokens, validates one action, checks foreground focus, dispatches input, and observes again. The model does not need native tool-call support. No cloud fallback; the endpoint must be loopback and the installed model must advertise vision capability.
Supports click, double click, right click, move, drag from current pointer, scroll, Windows Unicode typing, hotkeys, window focus, and waiting. Primary monitor only; elevated and secure desktops are unsupported. Input shares the user's mouse and keyboard and requires an uninterrupted desktop session.
Completion is labeled model_visual_assessment with visible evidence, not independent proof of success. Errors and limits report failure, including potentially partial input. Default 20 actions, maximum 40, bounded to 20 minutes. desktop_stop cancels pending inference and input. Emergency halt and mouse-corner fail-safe remain active.
VALIDATION
Protocol, native dispatch, cancellation, focus-change and failure tests: tests/test_desktop_agent.py. Live autonomous task completion remains unverified; the user paused live input testing.
SEE ALSO
desktop_action, desktop_observe, desktop_stop
'''
for name, prose in [('desktop_run', description), ('desktop_stop', 'NAME\ndesktop_stop - cancel the active local desktop loop\nSYNOPSIS\ndesktop_stop()\nDESCRIPTION\nCancels pending inference and further input independently of the input lock. The controller joins native input before releasing ownership. The persistent emergency halt remains the global stop mechanism.\nSEE ALSO\ndesktop_run')]:
    result = create_documentation_node(node_id='tool-' + name.replace('_','-'), title='Tool: ' + name, doc_target=name, semantic_memory=prose, node_type='function-doc', source_files=['sdk/agent/desktop_agent.py','sdk/agent/desktop_control.py'], purpose='description', accountability='descriptive', tags=['desktop','ollama','computer-use'])
    print(name, result.get('success', True))
