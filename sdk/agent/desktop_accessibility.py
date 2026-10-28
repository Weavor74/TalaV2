"""NAME
       desktop_accessibility - bounded Windows UI Automation snapshots
DESCRIPTION
       Runs COM inspection in a disposable subprocess so an unresponsive app
       cannot hang the desktop controller. No input or UI mutations occur here.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)


# The first ask of a Chromium/Electron window for its accessibility tree makes the window build one, which was seen to take longer than
# eight seconds; the next ask is quick. So: a generous first deadline, and one more try, before giving up on the window.
_DEADLINES_S = (25, 15)


def _title(hwnd):
    """The window's title, for the log only; never raises."""
    try:
        import ctypes
        buffer = ctypes.create_unicode_buffer(120)
        ctypes.windll.user32.GetWindowTextW(int(hwnd), buffer, 120)
        return buffer.value
    except Exception:
        return '?'


def snapshot(hwnd):
    """NAME
           snapshot - read native controls, bounded by a deadline and one retry
    DESCRIPTION
           Returns an explicit error when accessibility is unavailable.
    """
    if sys.platform != 'win32' or not hwnd:
        return {'elements': [], 'error': 'Windows accessibility unavailable'}
    waited = 0
    for attempt, deadline in enumerate(_DEADLINES_S, 1):
        started = time.monotonic()
        try:
            result = subprocess.run([sys.executable, '-m', 'sdk.agent.desktop_accessibility', str(int(hwnd))],
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True, text=True, encoding='utf-8', timeout=deadline,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            log.info('desktop accessibility: window %s read in %.1f s (try %d)', hwnd, time.monotonic() - started, attempt)
            if result.returncode:
                return {'elements': [], 'error': 'Accessibility helper failed'}
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            log.warning('desktop accessibility: window %s (%s) gave no answer in %d s (try %d)', hwnd, _title(hwnd), deadline, attempt)
            waited += deadline
        except Exception as exc:
            return {'elements': [], 'error': type(exc).__name__}
    return {'elements': [], 'error': f'TimeoutExpired after {waited} s over {len(_DEADLINES_S)} tries'}


def _read(hwnd):
    """NAME
           _read - inspect control names, roles and measured rectangles
    DESCRIPTION
           Never returns password values or COM objects across process boundaries.
    """
    import comtypes.client
    module = comtypes.client.GetModule('UIAutomationCore.dll')
    uia = comtypes.client.CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
    root = uia.ElementFromHandle(hwnd)
    walker = uia.ControlViewWalker
    deadline = time.monotonic() + 1.5
    elements = []
    roles = {50000: 'button', 50002: 'checkbox', 50003: 'combobox', 50004: 'input', 50005: 'link', 50007: 'listitem', 50011: 'menuitem', 50013: 'radio', 50018: 'tab', 50020: 'text', 50030: 'document', 50033: 'pane'}

    def visit(element, depth=0):
        if depth > 14 or len(elements) >= 160 or time.monotonic() > deadline:
            return
        try:
            child = walker.GetFirstChildElement(element)
            kind = element.CurrentControlType
            rect = element.CurrentBoundingRectangle
            enabled = bool(element.CurrentIsEnabled)
            focusable = bool(element.CurrentIsKeyboardFocusable)
            password = bool(element.CurrentIsPassword)
            if depth and not element.CurrentIsOffscreen and rect.right > rect.left and rect.bottom > rect.top:
                runtime_id = '.'.join(str(n) for n in element.GetRuntimeId())
                entry = {'id': runtime_id, 'role': roles.get(kind, str(kind)), 'name': element.CurrentName or '',
                    'bounds': [rect.left,rect.top,rect.right,rect.bottom], 'focused': bool(element.CurrentHasKeyboardFocus),
                    'clickable': enabled and (kind in (50000,50002,50003,50004,50005,50007,50011,50013,50018) or (focusable and not child)), 'password': password}
                if not password:
                    try:
                        pattern = element.GetCurrentPattern(10002).QueryInterface(module.IUIAutomationValuePattern)
                        entry['value'] = str(pattern.CurrentValue)[:1000]
                    except Exception:
                        pass
                elements.append(entry)
            while child and time.monotonic() <= deadline and len(elements) < 160:
                visit(child, depth+1)
                child = walker.GetNextSiblingElement(child)
        except Exception:
            return
    visit(root)
    return {'elements': elements}


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        print(json.dumps(_read(int(sys.argv[1])), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'elements': [], 'error': f'{type(exc).__name__}: {exc}'}))
