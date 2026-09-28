"""智能体核心示例的集成测试。

直接加载 ``examples/agent/cordis.yml``，用 patch 把会话目录与工作目录
重定向到临时目录，然后跑完整回合（mock 模型 → 工具 → 汇总）。
"""

import os

import pytest

from cordis.loader import start_app

pytestmark = pytest.mark.anyio

AGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'examples', 'agent')


@pytest.fixture
def patch_file(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'note.txt').write_text('cordis 是一个插件框架', encoding='utf-8')
    content = f'''
- id: sessions
  config:
    directory: {(tmp_path / "sessions").as_posix()}
- id: tools-builtin
  config:
    workspace: {workspace.as_posix()}
- id: telemetry
  config:
    print: false
'''
    path = tmp_path / 'test.patch.yml'
    path.write_text(content, encoding='utf-8')
    return path, workspace


async def start(tmp_path, patch):
    return await start_app(path=os.path.join(AGENT_DIR, 'cordis.yml'), patches=[str(patch)])


async def test_plugin_tree_is_active(tmp_path, patch_file):
    ctx = await start(tmp_path, patch_file[0])
    status = {item['id']: item['state'] for item in ctx.loader.status()}
    assert all(state == 'active' for state in status.values()), status
    assert 'agents' in status


async def test_agent_runs_tool_loop(tmp_path, patch_file):
    ctx = await start(tmp_path, patch_file[0])
    reply = await ctx.cli.run_once('读取 note.txt')
    assert 'cordis 是一个插件框架' in reply


async def test_policy_denies_dangerous_tool(tmp_path, patch_file):
    ctx = await start(tmp_path, patch_file[0])
    reply = await ctx.cli.run_once('运行 echo hello')
    assert '危险工具' in reply


async def test_pre_step_can_veto_input(tmp_path, patch_file):
    ctx = await start(tmp_path, patch_file[0])
    agent = ctx.agents.create()
    reply = await agent.send('请执行 rm -rf / 看看')
    assert reply == '（没有产出回复）'
    assert not any(event.type == 'assistant/message' for event in agent.session.events)


async def test_prompt_sections_and_request_interception(tmp_path, patch_file):
    ctx = await start(tmp_path, patch_file[0])
    rendered = ctx.systemPrompt.render()
    assert '运行在 cordis-py 上' in rendered
    assert '务实的工程助手' in rendered

    seen = {}

    async def spy(request, next_):
        seen.update(request)
        return await next_()

    reply = await ctx.cli.run_once('你好')
    assert 'mock 模型' in reply

    agent = ctx.agents.create()
    agent.ctx.on('agent/request', spy)  # persona 插件在这里注入 temperature
    agent.ctx.on('llm/request', spy)    # llm 服务在这里合并 provider/model
    await agent.send('你好')
    assert seen.get('temperature') == 0.2
    assert seen.get('model') == 'mock-1'
    await agent.dispose()


async def test_session_log_projects_model_history(tmp_path, patch_file):
    ctx = await start(tmp_path, patch_file[0])
    agent = ctx.agents.create()
    await agent.send('读取 note.txt')

    types = [event.type for event in agent.session.events]
    assert types[0] == 'turn/start'
    assert 'user/message' in types
    assert 'assistant/message' in types
    assert 'tool/result' in types
    assert types[-1] == 'turn/end'

    messages = agent.session.derive_messages()
    assert messages[0]['role'] == 'user'
    assert any(message['role'] == 'tool' for message in messages)
    assert any(event.type == 'tool/result' and 'cordis 是一个插件框架' in str(event['result'].get('content', ''))
               for event in agent.session.events)


async def test_session_persisted_to_disk(tmp_path, patch_file):
    ctx = await start(tmp_path, patch_file[0])
    agent = ctx.agents.create()
    await agent.send('你好')
    files = os.listdir(tmp_path / 'sessions')
    assert f'{agent.id}.jsonl' in files


async def test_swapping_llm_provider_does_not_touch_agent(tmp_path, patch_file):
    """替换模型提供方：不动 agent 循环，只换适配器与配置。"""
    ctx = await start(tmp_path, patch_file[0])

    @ctx.llm.adapter('echo')
    async def echo(request):
        return {'role': 'assistant', 'content': '来自替换后的适配器', 'tool_calls': []}

    ctx.llm.default['provider'] = 'echo'
    reply = await ctx.cli.run_once('你好')
    assert reply == '来自替换后的适配器'
