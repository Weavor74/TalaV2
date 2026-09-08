import json
import pytest
from sdk.agent.tools import run_workflow_node

@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv('TALA_USER_DATA', str(tmp_path))
    nodes = tmp_path / 'nodes'
    nodes.mkdir()
    spec = {'schema':'tala-flow/1.1','flow_id':'qa-chat','name':'Chat arithmetic','nodes':[
        {'id':'start','type':'trigger','config':{},'connections':{'onSuccess':'sum'}},
        {'id':'sum','type':'custom_script','config':{'code':"result = sum(variables['numbers'])",'timeout':3},'connections':{'onSuccess':'double'}},
        {'id':'double','type':'custom_script','config':{'code':"result = variables['step_sum']['result']['script_result'] * 2",'timeout':3},'connections':{'onSuccess':'end'}},
        {'id':'end','type':'output','config':{'value':'{{step_double.result.script_result}}','verify':'{{step_double.success}}'},'connections':{}}
    ]}
    path = nodes / 'flow-qa-chat.html'
    def write():
        path.write_text('<article data-node-id="flow-qa-chat" data-node-type="workflow" data-execution-authority="1.00"><section id="semantic-memory">Test</section><script type="application/json" id="node-state">'+json.dumps(spec)+'</script></article>')
    write()
    return spec,write

@pytest.mark.asyncio
@pytest.mark.parametrize('reference',['qa-chat','flow-qa-chat','Chat arithmetic'])
async def test_chat_saved_workflow_returns_real_output(saved, reference):
    out=await run_workflow_node(action='execute',node_id=reference,inputs={'numbers':[11,4,-3]})
    assert out['success'] is True
    assert out['value']==24
    assert out['verified'] is True
    assert out['executed_from']=='html-nn'

@pytest.mark.asyncio
async def test_chat_failure_is_structured(saved):
    spec,write=saved
    spec['nodes'][1]['config']['code']="raise ValueError('deliberate')"
    write()
    out=await run_workflow_node(action='start',node_id='qa-chat',value={'numbers':[1]})
    assert out['success'] is False
    assert 'deliberate' in out['error']

@pytest.mark.asyncio
async def test_unknown_action_and_missing_workflow_fail(saved):
    assert (await run_workflow_node(action='bogus',node_id='qa-chat'))['success'] is False
    assert (await run_workflow_node(action='execute',node_id='missing'))['success'] is False

@pytest.mark.asyncio
async def test_verification_failure_reaches_chat(saved):
    spec,write=saved
    spec['nodes'][-1]['config']['verify']='false'
    write()
    out=await run_workflow_node(action='execute',node_id='qa-chat',inputs={'numbers':[1]})
    assert out['success'] is False
    assert out['verified'] is False

@pytest.mark.asyncio
@pytest.mark.parametrize('arguments',[
    {'value':'{"numbers":[11,4,-3]}'},
    {'inputs':'{"numbers":[11,4,-3]}'},
    {'numbers':[11,4,-3]},
])
async def test_chat_input_encodings_do_not_use_script_defaults(saved, arguments):
    spec,write=saved
    spec['nodes'][1]['config']['code']="result = sum(variables.get('numbers', [2,3,5]))"
    write()
    out=await run_workflow_node(action='execute',node_id='qa-chat',**arguments)
    assert out['success'] is True
    assert out['inputs']=={'numbers':[11,4,-3]}
    assert out['value']==24

@pytest.mark.asyncio
@pytest.mark.parametrize('value',['numbers = [11,4,-3]','[11,4,-3]'])
async def test_bad_input_is_not_silently_discarded(saved, value):
    out=await run_workflow_node(action='execute',node_id='qa-chat',value=value)
    assert out['success'] is False
    assert 'execution' not in out
