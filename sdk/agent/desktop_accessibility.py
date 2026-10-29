"""NAME
       desktop_accessibility - bounded Windows UI Automation snapshots
DESCRIPTION
       Runs COM inspection in a disposable subprocess so an unresponsive app
       cannot hang the desktop controller. No input or UI mutations occur here.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)


# The first ask of a Chromium/Electron window for its accessibility tree makes the window build one, which was seen to take longer than
# eight seconds; the next ask is quick. So: a generous first deadline, and one more try, before giving up on the window.
_DEADLINES_S = (25, 15)

# Pass this instead of a window handle to read the whole desktop: the taskbar, the Start menu and every open window's controls.
DESKTOP = 'desktop'


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
    argument = DESKTOP if hwnd == DESKTOP else str(int(hwnd))
    waited = 0
    for attempt, deadline in enumerate(_DEADLINES_S, 1):
        started = time.monotonic()
        try:
            result = subprocess.run([sys.executable, '-m', 'sdk.agent.desktop_accessibility', argument],
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True, text=True, encoding='utf-8', timeout=deadline,
                # Above normal: the helper is short and the user is waiting, while a local model and the voice stack are using every core
                # (seen: a helper that takes 2 s on an idle machine had not got past its own start-up after 25 s during a voice turn).
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'ABOVE_NORMAL_PRIORITY_CLASS', 0))
            log.info('desktop accessibility: window %s read in %.1f s (try %d)', hwnd, time.monotonic() - started, attempt)
            if result.returncode:
                return {'elements': [], 'error': 'Accessibility helper failed'}
            read = json.loads(result.stdout)
            if read.get('skipped'):
                log.warning('desktop accessibility: windows that did not answer and were skipped: %s', read['skipped'])
            return read
        except subprocess.TimeoutExpired as exc:
            seen = exc.stderr.decode('utf-8', 'replace') if isinstance(exc.stderr, bytes) else (exc.stderr or '')
            log.warning('desktop accessibility: window %s (%s) gave no answer in %d s (try %d); the helper had got as far as: %s',
                hwnd, _title(hwnd), deadline, attempt, ' | '.join(seen.strip().splitlines()[-4:]) or 'nothing (it never started)')
            waited += deadline
        except Exception as exc:
            return {'elements': [], 'error': type(exc).__name__}
    return {'elements': [], 'error': f'TimeoutExpired after {waited} s over {len(_DEADLINES_S)} tries'}


_ROLES = {50000: 'button', 50002: 'checkbox', 50003: 'combobox', 50004: 'input', 50005: 'link', 50007: 'listitem', 50011: 'menuitem', 50013: 'radio', 50018: 'tab', 50020: 'text', 50030: 'document', 50033: 'pane'}


def _trace(message):
    """One timestamped line on stderr; the controller logs the tail of it when the helper does not finish, so a hang names its step."""
    sys.stderr.write(f'{time.strftime("%H:%M:%S")}.{int(time.time() * 1000) % 1000:03d} {message}\n')
    sys.stderr.flush()


def _uia():
    _trace('importing comtypes')
    import comtypes.client
    module = comtypes.client.GetModule('UIAutomationCore.dll')
    uia = comtypes.client.CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
    _trace('automation client ready')
    return module, uia


def _collect(module, walker, root, elements, deadline, cap):
    """Append the measured controls under root (not root itself) until the deadline or the cap."""

    def visit(element, depth=0):
        if depth > 14 or len(elements) >= cap or time.monotonic() > deadline:
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
                entry = {'id': runtime_id, 'role': _ROLES.get(kind, str(kind)), 'name': element.CurrentName or '',
                    'bounds': [rect.left,rect.top,rect.right,rect.bottom], 'focused': bool(element.CurrentHasKeyboardFocus),
                    'clickable': enabled and (kind in (50000,50002,50003,50004,50005,50007,50011,50013,50018) or (focusable and not child)), 'password': password}
                if not password:
                    try:
                        pattern = element.GetCurrentPattern(10002).QueryInterface(module.IUIAutomationValuePattern)
                        entry['value'] = str(pattern.CurrentValue)[:1000]
                    except Exception:
                        pass
                elements.append(entry)
            while child and time.monotonic() <= deadline and len(elements) < cap:
                visit(child, depth+1)
                child = walker.GetNextSiblingElement(child)
        except Exception:
            return
    visit(root)


def _read(hwnd):
    """NAME
           _read - inspect control names, roles and measured rectangles
    DESCRIPTION
           Never returns password values or COM objects across process boundaries.
    """
    module, uia = _uia()
    elements = []
    _collect(module, uia.ControlViewWalker, uia.ElementFromHandle(hwnd), elements, time.monotonic() + 1.5, 160)
    return {'elements': elements}


def _own_pids():
    """Process ids of the Tala app this helper belongs to: its ancestors that are the Electron shell, never a terminal or launcher."""
    try:
        import psutil
        return {p.pid for p in psutil.Process().parents() if any(word in (p.name() or '').lower() for word in ('electron', 'tala'))}
    except Exception:
        return set()


# Per top-level window: enough to reach a taskbar button or a Start menu tile, little enough that a busy window cannot eat the whole budget.
_WINDOW_S = 0.5
_WINDOW_CAP = 70
_DESKTOP_S = 6.0
_DESKTOP_CAP = 160
# The taskbar and the Start menu come first: they are why a desktop read exists, and the cut in front of the model keeps the first ones.
_SHELL_CLASSES = ('Shell_TrayWnd', 'Shell_SecondaryTrayWnd', 'Windows.UI.Core.CoreWindow', 'XamlExplorerHostIslandWindow', 'TopLevelWindowForOverflowXamlIsland')


def _top_windows():
    """(hwnd, pid, title) of the visible, uncloaked top-level windows that have an area, shell windows first and then front to back."""
    import ctypes
    from ctypes import wintypes
    user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        cloaked = wintypes.DWORD(0)
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if cloaked.value or rect.right <= rect.left or rect.bottom <= rect.top:
            return True
        pid, name, title = wintypes.DWORD(), ctypes.create_unicode_buffer(80), ctypes.create_unicode_buffer(80)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        user32.GetClassNameW(hwnd, name, 80)
        user32.GetWindowTextW(hwnd, title, 80)
        found.append((name.value in _SHELL_CLASSES, hwnd, pid.value, title.value or name.value))
        return True

    user32.EnumWindows(each, 0)
    found.sort(key=lambda row: not row[0])
    return [row[1:] for row in found]


def _read_window(hwnd, elements):
    """Fill elements from one window on a thread of its own, and give up on it, not on the whole read, if it does not answer."""
    import threading

    def work():
        try:
            import comtypes
            comtypes.CoInitialize()
            module, uia = _uia()
            _collect(module, uia.ControlViewWalker, uia.ElementFromHandle(hwnd), elements, time.monotonic() + _WINDOW_S, _WINDOW_CAP)
        except Exception:
            pass

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(_WINDOW_S + 1.0)
    return not worker.is_alive()


def _read_desktop():
    """NAME
           _read_desktop - inspect the controls of every top-level window on the desktop
    DESCRIPTION
           The taskbar, the Start menu and the open applications. A window that does not answer (a full-screen game, live: 2026-10-28)
           is skipped and named, not waited for; Tala's own window is left out, since it is not something she is asked to operate.
    """
    _trace('finding the apps own processes')
    own = _own_pids()
    _trace('listing windows')
    windows = _top_windows()
    _trace(f'{len(windows)} windows')
    elements, skipped = [], []
    deadline = time.monotonic() + _DESKTOP_S
    for hwnd, pid, title in windows:
        if time.monotonic() > deadline or len(elements) >= _DESKTOP_CAP:
            break
        if pid in own:
            continue
        mine = []
        _trace(f'reading {title[:30]!r}')
        if not _read_window(hwnd, mine):
            skipped.append(title)
            _trace('  no answer')
        elements.extend(mine[:max(0, _DESKTOP_CAP - len(elements))])
    return {'elements': elements, 'skipped': skipped}


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    _trace('helper started')
    try:
        print(json.dumps(_read_desktop() if sys.argv[1] == DESKTOP else _read(int(sys.argv[1])), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'elements': [], 'error': f'{type(exc).__name__}: {exc}'}))
    # A window that never answered leaves a thread stuck inside COM; do not let interpreter shutdown wait for it.
    sys.stdout.flush()
    _trace('done')
    os._exit(0)
