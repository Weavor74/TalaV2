"""NAME
       test_desktop_agent - exercise local desktop protocol and failure boundaries
DESCRIPTION
       Mock native input; never move the user's mouse during the unit suite.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from sdk.agent import desktop_agent as agent


def test_desktop_schema_resolves_deferred_annotations():
    from sdk.agent.tools import ToolRegistry, _coerce_declared_numbers
    import inspect
    schema = ToolRegistry._generate_schema(agent.desktop_run)['properties']
    assert schema['max_steps']['type'] == 'integer'
    assert schema['cpu_only']['type'] == 'boolean'
    action = ToolRegistry._generate_schema(agent.desktop.desktop_action)['properties']
    assert action['x']['type'] == 'integer' and action['keys']['type'] == 'array'
    assert _coerce_declared_numbers(inspect.signature(agent.desktop_run), {'cpu_only': 'false'})['cpu_only'] is False


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(agent.desktop, '_lock', asyncio.Lock())
    monkeypatch.setattr(agent.desktop, '_check_halt', lambda: None)
    monkeypatch.setattr(agent.desktop, '_capture', lambda **kwargs: {'success': True, 'observation_id': 'owned-token', 'data': 'pixels', 'width': 100, 'height': 100})
    act = MagicMock(return_value={'success': True, 'verified': False})
    monkeypatch.setattr(agent.desktop, '_act', act)
    client = AsyncMock()
    client.post.return_value = MagicMock(json=lambda: {'capabilities': ['vision']})
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=client)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(agent.httpx, 'AsyncClient', factory)
    monkeypatch.setattr(agent, '_running', False)
    monkeypatch.setattr(agent.accessibility, 'snapshot', lambda hwnd: {'elements':[]})
    monkeypatch.setattr(agent, '_verify', AsyncMock(return_value=agent.DesktopVerification(complete=False, evidence='Not complete')))
    return act, client


@pytest.mark.asyncio
async def test_controller_owns_token_and_observes_after_action(runtime, monkeypatch):
    monkeypatch.setattr(agent, '_verify', AsyncMock(return_value=agent.DesktopVerification(complete=True, evidence='PASS is visible')))
    decide = AsyncMock(side_effect=[agent.DesktopDecision(action='click', x=20, y=30, evidence='field'), agent.DesktopDecision(action='done', evidence='PASS is visible')])
    monkeypatch.setattr(agent, '_decide', decide)
    result = await agent.desktop_run('test', max_steps=1, success_text='PASS')
    assert result['success'] and result['verification'] == 'required_text_in_screen_transcription'
    assert runtime[0].call_args.args[:4] == ('owned-token', 'click', 2, 3)
    assert decide.await_count == 1


@pytest.mark.asyncio
async def test_no_silent_action_failure(runtime, monkeypatch):
    runtime[0].side_effect = RuntimeError('Foreground window changed')
    monkeypatch.setattr(agent, '_decide', AsyncMock(return_value=agent.DesktopDecision(action='type', text='hello', evidence='field')))
    result = await agent.desktop_run('test')
    assert not result['success'] and 'Foreground' in result['error']


@pytest.mark.asyncio
async def test_stop_cancels_inflight_model(runtime, monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def waiting(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(agent, '_decide', waiting)
    task = asyncio.create_task(agent.desktop_run('test'))
    await entered.wait()
    await agent.desktop_stop()
    result = await asyncio.wait_for(task, 2)
    assert result['status'] == 'stopped' and cancelled.is_set()
    runtime[0].assert_not_called()


@pytest.mark.asyncio
async def test_no_cloud_fallback(runtime):
    result = await agent.desktop_run('test', endpoint='https://example.com')
    assert not result['success']
    runtime[1].post.assert_not_called()


@pytest.mark.asyncio
async def test_nonvision_model_rejected(runtime):
    runtime[1].post.return_value.json = lambda: {'capabilities': ['completion']}
    result = await agent.desktop_run('test')
    assert not result['success'] and 'vision' in result['error']


@pytest.mark.asyncio
async def test_unverified_completion_is_failure(runtime, monkeypatch):
    monkeypatch.setattr(agent, '_decide', AsyncMock(return_value=agent.DesktopDecision(action='done', evidence='I think done')))
    result = await agent.desktop_run('test')
    assert not result['success'] and 'Completion claim rejected' in result['error']
    runtime[0].assert_not_called()


@pytest.mark.asyncio
async def test_visible_button_is_not_final_success_text():
    client = AsyncMock()
    client.post.return_value = MagicMock(json=lambda: {'message':{'content':'{"visible_text":"TALA-482\\nVerify\\nWaiting"}'}})
    result = await agent._verify(client,'http://localhost:11434','vision','enter text and verify',{'data':'annotated','raw_data':'original'},success_text='PASS: TALA-482')
    assert not result.complete
    sent = client.post.call_args.kwargs['json']['messages']
    assert sent[0]['images'] == ['original']
    assert 'PASS: TALA-482' not in sent[0]['content']
    client.post.return_value.json = lambda: {'message':{'content':'{"visible_text":"TALA-482\\nVerify\\nPASS: TALA-482"}'}}
    assert (await agent._verify(client,'http://localhost:11434','vision','task',{'data':'original'},success_text='PASS: TALA-482')).complete


@pytest.mark.asyncio
async def test_ollama_receives_pixels_and_schema():
    client = AsyncMock()
    client.post.return_value = MagicMock(json=lambda: {'message': {'content': '{"action":"blocked","evidence":"No target"}'}})
    result = await agent._decide(client, 'http://localhost:11434', 'vision', 'task', {'data': 'pixels', 'observation_id': 'secret-token'}, [])
    payload = client.post.call_args.kwargs['json']
    assert payload['messages'][1]['images'] == ['pixels']
    assert 'secret-token' not in payload['messages'][1]['content']
    assert payload['format']['anyOf'][0]['required'] == ['action','evidence','x','y']
    assert result.action == 'blocked'


@pytest.mark.asyncio
async def test_action_budget_does_not_report_success(runtime, monkeypatch):
    monkeypatch.setattr(agent, '_decide', AsyncMock(return_value=agent.DesktopDecision(action='click', x=20, y=30, evidence='button')))
    result = await agent.desktop_run('test', max_steps=1)
    assert not result['success'] and 'limit' in result['error']
    assert runtime[0].call_count == 1


@pytest.mark.asyncio
async def test_cancellation_joins_native_worker(runtime, monkeypatch):
    import threading
    import time
    entered = threading.Event()
    finished = threading.Event()
    def work(*args):
        entered.set()
        while not agent.desktop._input_stop.is_set():
            time.sleep(0.01)
        finished.set()
    monkeypatch.setattr(agent.desktop, '_act', work)
    task = asyncio.create_task(agent._dispatch({'observation_id': 'token'}, agent.DesktopDecision(action='type', text='test', evidence='field')))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    agent.desktop._input_stop.clear()


@pytest.mark.asyncio
async def test_crop_coordinates_are_translated_once(runtime):
    await agent._dispatch({'observation_id': 't', 'image_origin': (250,160), 'width': 800, 'height': 500}, agent.DesktopDecision(action='click', x=100, y=120, evidence='field'))
    assert runtime[0].call_args.args[2:4] == (350, 280)
    with pytest.raises(ValueError, match='outside'):
        await agent._dispatch({'observation_id': 't', 'width': 800, 'height': 500}, agent.DesktopDecision(action='click', x=900, y=20, evidence='outside'))


@pytest.mark.asyncio
async def test_accessible_target_is_remeasured_before_click(runtime, monkeypatch):
    old = {'id':'control', 'role':'button', 'name':'Verify', 'clickable':True, 'bounds':[10,20,110,60]}
    fresh = {**old, 'bounds':[200,300,300,340]}
    monkeypatch.setattr(agent.accessibility, 'snapshot', lambda hwnd: {'elements':[fresh]})
    shot = {'observation_id':'t', 'window_handle':42, 'elements':[old]}
    decision = agent.DesktopDecision(action='click', element_id='control', evidence='Verify')
    await agent._dispatch(shot, decision)
    assert runtime[0].call_args.args[2:4] == (250,320)
    monkeypatch.setattr(agent.accessibility, 'snapshot', lambda hwnd: {'elements':[]})
    with pytest.raises(ValueError, match='disappeared'):
        await agent._dispatch(shot, decision)


def test_action_specific_schema_requires_measured_target():
    schema = agent._decision_schema(False, ['field','button'])
    click = next(v for v in schema['anyOf'] if 'click' in v['properties']['action']['enum'])
    assert click['required'] == ['action','evidence','element_id']
    assert click['properties']['element_id']['enum'] == ['field','button']
    assert all('type' not in v['properties']['action']['enum'] for v in schema['anyOf'])


def test_control_labels_preserve_original_verification_image():
    import base64
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (200,200), 'white').save(buffer,format='PNG')
    original = base64.b64encode(buffer.getvalue()).decode()
    shot = {'data':original, 'width':200, 'height':200, 'elements':[
        {'id':'native-button','bounds':[20,100,90,140],'role':'button','name':'OK','clickable':True,'focused':False},
        {'id':'native-field','bounds':[20,40,180,70],'role':'input','name':'Value','clickable':True,'focused':False}]}
    result = agent._prepare_observation(shot)
    assert result['raw_data'] == original and result['data'] != original
    assert [(e['id'],e['native_id']) for e in result['elements']] == [('E1','native-field'),('E2','native-button')]


@pytest.mark.asyncio
async def test_custom_widget_focus_does_not_trap_typing():
    client = AsyncMock()
    client.post.return_value = MagicMock(json=lambda: {'message':{'content':'{"action":"type","text":"hello","evidence":"Clicked field"}'}})
    element = {'id':'E1','role':'pane','name':'','focused':False,'clickable':True,'bounds':[0,30,100,60]}
    shot = {'data':'pixels','width':100,'height':100,'elements':[element]}
    history = [{'proposal':{'action':'click','element_id':'E1'},'result':{'success':True,'control':{'role':'pane'}}}]
    result = await agent._decide(client,'http://localhost:11434','vision','type hello',shot,history)
    assert result.action == 'type'


@pytest.mark.asyncio
async def test_rpc_stop_cancels_run_even_before_input_loop_starts(monkeypatch):
    import json
    from python.harness import server
    from sdk.agent.tools import ToolRegistry
    entered = asyncio.Event()
    async def waiting(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(ToolRegistry, 'run_tool', waiting)
    task = asyncio.create_task(server.handle_desktop_run({'task':'test'}, 'run'))
    await entered.wait()
    stop = json.loads(await server.handle_desktop_stop({}, 'stop'))
    result = json.loads(await task)
    assert stop['result']['stop_requested']
    assert result['result']['output']['status'] == 'stopped'
    assert not server._desktop_rpc_tasks


@pytest.mark.parametrize('action,method', [('click','click'), ('double_click','doubleClick'), ('right_click','click'), ('move','moveTo'), ('drag','dragTo'), ('scroll','scroll'), ('hotkey','hotkey')])
def test_native_dispatch_and_single_use_token(monkeypatch, action, method):
    import time
    from types import SimpleNamespace
    pg = MagicMock()
    pg.getActiveWindow.return_value = SimpleNamespace(_hWnd=42, title='Test', isMinimized=False)
    pg.KEYBOARD_KEYS = ['ctrl', 'a']
    monkeypatch.setattr(agent.desktop, '_backend', lambda: pg)
    monkeypatch.setattr(agent.desktop, '_check_halt', lambda: None)
    # Window ids are strings (_Window wraps the HWND with str()), and an
    # observation carries image size, the logical size it covers, and its
    # origin -- the mapping _act uses to turn observed pixels into input
    # coordinates.
    monkeypatch.setattr(agent.desktop, '_observations',
                        {'t': (time.monotonic(), '42', (800, 600), (800, 600), (0, 0))})
    result = agent.desktop._act('t', action, 100, 120, '', ['ctrl','a'], 2, '')
    assert result['success'] and not result['verified']
    getattr(pg, method).assert_called_once()
    with pytest.raises(ValueError, match='consumed'):
        agent.desktop._act('t', action, 100, 120, '', ['ctrl','a'], 2, '')


def test_native_input_refuses_changed_focus(monkeypatch):
    import time
    pg = MagicMock()
    pg.getActiveWindow.return_value._hWnd = 99
    monkeypatch.setattr(agent.desktop, '_backend', lambda: pg)
    monkeypatch.setattr(agent.desktop, '_check_halt', lambda: None)
    # Window ids are strings (_Window wraps the HWND with str()), and an
    # observation carries image size, the logical size it covers, and its
    # origin -- the mapping _act uses to turn observed pixels into input
    # coordinates.
    monkeypatch.setattr(agent.desktop, '_observations',
                        {'t': (time.monotonic(), '42', (800, 600), (800, 600), (0, 0))})
    with pytest.raises(ValueError, match='Foreground'):
        agent.desktop._act('t', 'click', 100, 120, '', [], 0, '')
    pg.click.assert_not_called()
