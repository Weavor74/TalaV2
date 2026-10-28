"""
"Open the Start menu on my computer" failed live (2026-10-28) because desktop_run looked only at the front window -- which was
Tala's own chat. A window-only picture never contains the taskbar, and reading the accessibility tree of her own Electron window
hung for the full 40 s. desktop_run now looks at the whole screen and reads the controls of the whole desktop.
"""
from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from sdk.agent import desktop_accessibility as da
from sdk.agent import desktop_agent as agent


@pytest.mark.asyncio
async def test_desktop_run_looks_at_the_whole_screen_and_reads_the_whole_desktop(monkeypatch):
    seen = {}

    def capture(**kwargs):
        seen['capture'] = kwargs
        return {'success': True, 'observation_id': 't', 'data': 'pixels', 'width': 100, 'height': 100, 'window_handle': 777}

    def snapshot(target):
        seen['snapshot'] = target
        return {'elements': []}

    monkeypatch.setattr(agent.desktop, '_lock', asyncio.Lock())
    monkeypatch.setattr(agent.desktop, '_check_halt', lambda: None)
    monkeypatch.setattr(agent.desktop, '_capture', capture)
    monkeypatch.setattr(agent.accessibility, 'snapshot', snapshot)
    monkeypatch.setattr(agent, '_running', False)
    monkeypatch.setattr(agent, '_native_supported', lambda: True)
    client = AsyncMock()
    client.post.return_value = MagicMock(json=lambda: {'capabilities': ['vision']})
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=client)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(agent.httpx, 'AsyncClient', factory)
    monkeypatch.setattr(agent, '_decide', AsyncMock(return_value=agent.DesktopDecision(action='blocked', evidence='stop here')))

    await agent.desktop_run('open the start menu', max_steps=1)

    assert seen['capture'] == {'foreground_only': False}, "a window-only capture cannot contain the taskbar"
    assert seen['snapshot'] == da.DESKTOP, "the front window is usually Tala's own; the desktop is what the task is about"


def _element(name, top, ident):
    return {'id': ident, 'role': 'button', 'name': name, 'bounds': [10, top, 50, top + 20], 'focused': False, 'clickable': True, 'password': False}


def test_the_taskbar_survives_the_label_cut_on_a_desktop_read():
    """A desktop read arrives front to back; sorting it top to bottom put the taskbar (bottom of the screen) past the 60-label cut."""
    import base64
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new('RGB', (200, 1200), 'white').save(buffer, format='JPEG')
    data = base64.b64encode(buffer.getvalue()).decode()
    elements = [_element('Start', 1100, 'taskbar.start')] + [_element(f'window button {n}', 40 + n * 3, f'w.{n}') for n in range(100)]
    shot = {'data': data, 'width': 200, 'height': 1200, 'elements': elements, 'window_handle': da.DESKTOP}
    labelled = agent._prepare_observation(shot)
    assert [e['name'] for e in labelled['elements'] if e.get('native_id') == 'taskbar.start'] == ['Start']


def test_a_single_window_read_is_still_sorted_top_to_bottom():
    import base64
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new('RGB', (200, 400), 'white').save(buffer, format='JPEG')
    data = base64.b64encode(buffer.getvalue()).decode()
    shot = {'data': data, 'width': 200, 'height': 400, 'window_handle': 42,
            'elements': [_element('low', 300, 'b'), _element('high', 100, 'a')]}
    labelled = agent._prepare_observation(shot)
    assert [e['name'] for e in labelled['elements']] == ['high', 'low']


def test_the_desktop_is_read_by_the_same_helper_with_a_word_for_a_window(monkeypatch):
    monkeypatch.setattr(da.sys, 'platform', 'win32')
    seen = []

    def run(cmd, **kw):
        seen.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout='{"elements": []}', stderr='')

    monkeypatch.setattr(da.subprocess, 'run', run)
    assert da.snapshot(da.DESKTOP) == {'elements': []}
    assert da.snapshot(1234) == {'elements': []}
    assert seen == ['desktop', '1234']


def test_finding_the_apps_own_processes_never_raises(monkeypatch):
    assert isinstance(da._own_pids(), set)
    import builtins
    real = builtins.__import__

    def refuse(name, *a, **k):
        if name == 'psutil':
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', refuse)
    assert da._own_pids() == set()


# --- a window that does not answer is skipped, not waited for ---------------------------------------------------------------

def test_a_window_that_does_not_answer_is_skipped_and_named(monkeypatch):
    """Live: a full-screen game never answered the accessibility read, and the whole desktop read hung until the 40 s limit."""
    monkeypatch.setattr(da, '_own_pids', lambda: {99})
    monkeypatch.setattr(da, '_top_windows', lambda: [(1, 10, 'Taskbar'), (2, 13, 'Star Citizen '), (3, 99, 'Tala'), (4, 11, 'Notepad')])
    asked = []

    def read_window(hwnd, elements):
        asked.append(hwnd)
        if hwnd == 2:
            return False
        elements.append({'id': str(hwnd), 'role': 'button', 'name': f'w{hwnd}', 'bounds': [0, 0, 1, 1], 'focused': False, 'clickable': True})
        return True

    monkeypatch.setattr(da, '_read_window', read_window)
    result = da._read_desktop()
    assert asked == [1, 2, 4], "her own window (pid 99) is never read"
    assert result['skipped'] == ['Star Citizen ']
    assert [e['name'] for e in result['elements']] == ['w1', 'w4']


def test_the_skipped_windows_are_logged(monkeypatch, caplog):
    monkeypatch.setattr(da.sys, 'platform', 'win32')
    out = '{"elements": [], "skipped": ["Star Citizen "]}'
    monkeypatch.setattr(da.subprocess, 'run', lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=''))
    with caplog.at_level('WARNING', logger=da.__name__):
        da.snapshot(da.DESKTOP)
    assert 'Star Citizen' in caplog.text


def test_the_cap_holds_across_windows(monkeypatch):
    monkeypatch.setattr(da, '_own_pids', set)
    monkeypatch.setattr(da, '_top_windows', lambda: [(n, n, f'w{n}') for n in range(10)])

    def read_window(hwnd, elements):
        elements.extend({'id': f'{hwnd}.{i}', 'role': 'button', 'name': '', 'bounds': [0, 0, 1, 1], 'focused': False, 'clickable': True} for i in range(70))
        return True

    monkeypatch.setattr(da, '_read_window', read_window)
    assert len(da._read_desktop()['elements']) == da._DESKTOP_CAP
