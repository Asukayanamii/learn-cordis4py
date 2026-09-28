"""第 7 章 · 实战：用 Cordis 搭一个智能体核心（单文件版）。

到这里你已经掌握了四个原语：**插件 / 服务 / 注入 / 事件 / effect**。
智能体核心不过是把它们组合起来：

.. code-block:: text

    ┌──────────────┐   llm/request (waterfall)   ┌──────────────┐
    │  agent loop  │ ──────────────────────────► │  ctx.llm     │ ← 适配器可替换
    │ (ctx.agents) │                             └──────────────┘
    │              │   tools/pre-execute (waterfall：策略可拒绝)
    │              │ ──────────────────────────► ┌──────────────┐
    │              │   tools/execute / post      │  ctx.tools   │ ← 工具即插件注册
    └──────┬───────┘                             └──────────────┘
           │ session/event（持久事实）
           ▼
    ┌──────────────┐
    │ ctx.sessions │  → derive_messages() 投影出模型历史
    └──────────────┘

本章用**仓库里的真实框架**（``cordis`` 包）实现：约 200 行就是一个能跑
"模型 → 工具 → 模型"完整回合的智能体核心。更完整的版本（配置驱动、
多插件、真实模型适配器、CLI REPL）见 ``examples/agent/``。

运行：
    python tutorial/ch07_agent.py "读取 pyproject.toml"
    python tutorial/ch07_agent.py            # 交互模式
"""

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:  # pragma: no cover
    pass

from cordis import Context, Service  # noqa: E402
from cordis.utils import maybe_await  # noqa: E402


# ============================================================== 1) 工具服务
class Tools(Service):
    """工具注册表 + 执行流水线（三个 waterfall 事件是它的扩展点）。"""

    def __init__(self, ctx):
        super().__init__(ctx, 'tools')
        self.store = {}

    def tool(self, name, description, parameters=None):
        """装饰器：@ctx.tools.tool('read_file', '读取文件', {...})"""
        def decorator(func):
            self.store[name] = {'name': name, 'description': description,
                                'parameters': parameters or {'type': 'object', 'properties': {}},
                                'execute': func}
            # 关键：注册绑定到"调用方 fiber"（服务内的 self.ctx 是调用方上下文），
            # 因此插件卸载时工具自动消失。
            self.ctx.cleanup(lambda: self.store.pop(name, None), f'tools.register({name})')
            return func
        return decorator

    def schemas(self):
        return [{'name': tool['name'], 'description': tool['description'],
                 'parameters': tool['parameters']} for tool in self.store.values()]

    async def execute(self, name, arguments, ctx=None):
        call = {'id': f'call_{len(self.store)}', 'name': name, 'arguments': dict(arguments or {})}
        self.ctx.emit('tool/call', call)

        async def run(call_, next_):
            tool = self.store.get(call_['name'])
            if tool is None:
                return {'ok': False, 'error': f'未知工具: {call_["name"]}'}
            try:
                output = await maybe_await(tool['execute'](ctx, **call_['arguments']))
                return {'ok': True, 'content': str(output)}
            except Exception as error:
                return {'ok': False, 'error': f'{type(error).__name__}: {error}'}

        async def keep(call_, result_, next_):
            return result_

        result = await maybe_await(self.ctx.waterfall('tools/pre-execute', call, run))
        result = await maybe_await(self.ctx.waterfall('tools/post-execute', call, result, keep))
        call['result'] = result
        self.ctx.emit('tool/result', call)
        return result


# ============================================================== 2) 会话日志
class Sessions(Service):
    """仅追加的会话日志；模型历史由它投影而来（"模型可见即已记录"）。"""

    def __init__(self, ctx):
        super().__init__(ctx, 'sessions')
        self.log = []

    def append(self, type_, **payload):
        event = {'type': type_, 'seq': len(self.log) + 1, **payload}
        self.log.append(event)
        self.ctx.emit('session/event', event)
        return event

    def derive_messages(self):
        messages = []
        for event in self.log:
            if event['type'] == 'user/message':
                messages.append({'role': 'user', 'content': event['content']})
            elif event['type'] == 'assistant/message':
                messages.append(event['message'])
            elif event['type'] == 'tool/result':
                messages.append({'role': 'tool', 'tool_call_id': event['call']['id'],
                                 'content': str(event['result'].get('content') or event['result'].get('error'))})
        return messages


# ============================================================== 3) 模型服务（mock）
class LLM(Service):
    """模型 seam：适配器可替换（第 7 章的 mock 适配器 + agent 循环共用一个接口）。"""

    def __init__(self, ctx):
        super().__init__(ctx, 'llm')

    async def chat(self, messages, tools=None, **options):
        request = {'messages': messages, 'tools': tools or [], **options}

        async def send(request_, next_):
            return await self._mock(request_)

        # llm/request：插件可以整体改写请求（换模型、裁剪历史、加缓存标记……）
        return await maybe_await(self.ctx.waterfall('llm/request', request, send))

    async def _mock(self, request):
        messages = request['messages']
        task = next((m['content'] for m in reversed(messages) if m.get('role') == 'user'), '')
        tool_results = [m for m in messages if m.get('role') == 'tool']
        if tool_results:
            summary = '\n'.join(f"- {item['content'][:160]}" for item in tool_results[-2:])
            return {'role': 'assistant', 'content': f'任务完成，工具结果如下：\n{summary}', 'tool_calls': []}
        read = re.search(r'(?:读取|read)\s*[「"]?([^\s」"]+)', task)
        if read:
            return {'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'mock_1', 'name': 'read_file', 'arguments': {'path': read.group(1)}}]}
        run = re.search(r'(?:运行|执行|run)\s+(.+)', task)
        if run:
            return {'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'mock_1', 'name': 'shell', 'arguments': {'command': run.group(1).strip()}}]}
        return {'role': 'assistant', 'content': f'（mock 模型）收到任务：{task}', 'tool_calls': []}


# ============================================================== 4) agent 循环
class Agents(Service):
    """轮次流程：pre-step → 组装 → request → 模型 → 工具 → ... → turn/end。"""

    inject = ['llm', 'tools', 'sessions']

    def __init__(self, ctx, config=None):
        super().__init__(ctx, 'agents')
        self.max_steps = int((config or {}).get('max_steps', 3))

    async def run(self, task):
        sessions = self.owner_ctx.sessions
        sessions.append('turn/start')

        async def accept(message, next_):
            return message

        decision = await maybe_await(self.ctx.waterfall('agent/pre-step', {'content': task}, accept))
        if not decision:
            sessions.append('turn/end')
            self.ctx.emit('agent/turn-stopping', None)
            return '（输入被策略拒绝）'
        sessions.append('user/message', content=decision['content'])

        for step in range(self.max_steps):
            sessions.append('step/start', step=step)
            request = {'messages': sessions.derive_messages(), 'tools': self.owner_ctx.tools.schemas()}

            async def use(request_, next_):
                return request_

            request = await maybe_await(self.ctx.waterfall('agent/request', request, use))
            reply = await self.owner_ctx.llm.chat(**request)
            sessions.append('assistant/message', message=reply)
            calls = reply.get('tool_calls') or []
            if not calls:
                sessions.append('step/end', step=step)
                sessions.append('turn/end')
                return reply.get('content')

            for call in calls:
                result = await self.owner_ctx.tools.execute(call['name'], call['arguments'], ctx=self.ctx)
                sessions.append('tool/result', call=call, result=result)
            sessions.append('step/end', step=step)

            stopping = await self.ctx.serial('agent/turn-stopping', None)
            if stopping:
                break
        sessions.append('turn/end')
        return '（达到最大步数）'


# ============================================================== 5) 插件：内置工具
def builtin_tools(ctx, config=None):
    workspace = os.path.abspath((config or {}).get('workspace') or os.getcwd())

    @ctx.tools.tool('read_file', '读取文件内容', {
        'type': 'object',
        'properties': {'path': {'type': 'string', 'description': '文件路径'}},
        'required': ['path'],
    })
    async def read_file(agent_ctx, path):
        full = path if os.path.isabs(path) else os.path.join(workspace, path)
        with open(full, 'r', encoding='utf-8', errors='replace') as stream:
            return stream.read(4000)

    @ctx.tools.tool('shell', '执行 shell 命令（危险工具，需要策略放行）', {
        'type': 'object',
        'properties': {'command': {'type': 'string'}},
        'required': ['command'],
    })
    async def shell(agent_ctx, command):
        import subprocess
        done = subprocess.run(command, shell=True, cwd=workspace, capture_output=True, text=True, timeout=20)
        return (done.stdout or '') + (done.stderr or '')


builtin_tools.inject = ['tools']


# ============================================================== 6) 插件：策略（审批）
def policy(ctx, config=None):
    """在 tools/pre-execute 上做策略：**不调用 next() 直接返回 = 否决**。"""
    config = config or {}
    # 注意 Python 的"假值"陷阱：[] 是假值，所以这里不能用 `config.get('deny') or ['shell']`
    deny = set(config['deny'] if 'deny' in config else ['shell'])
    allowed = set(config.get('allow') or [])

    async def guard(call, next_):
        if call['name'] in deny:
            ctx.logger.warn('策略拒绝了 %s', call['name'])
            return {'ok': False, 'error': f'策略拒绝了工具 {call["name"]}'}
        if call['name'] == 'shell' and call['name'] not in allowed:
            return {'ok': False, 'error': 'shell 需要显式放行（allow 列表）'}
        return await next_()

    ctx.on('tools/pre-execute', guard)


policy.inject = ['tools']


# ============================================================== 7) 插件：遥测（观察者）
def telemetry(ctx, config=None):
    """只观察、不干预：监听事件即可。

    注意：``tool/result`` 是 **emit**（同步）事件，监听器必须是同步函数；
    写成 async def 会返回一个没人等待的协程（框架会给出 RuntimeWarning）。
    需要异步等待请用 parallel / serial 分发的事件。
    """

    def on_result(call):
        status = '成功' if call['result'].get('ok') else f'失败（{call["result"].get("error")}）'
        ctx.logger.info('工具 %s %s', call['name'], status)

    ctx.on('tool/result', on_result)


telemetry.inject = ['tools']


# ============================================================== 组装并运行
async def settle(ctx):
    """等待所有已注册的 fiber 完成加载（真实项目里由 loader 负责）。"""
    for runtime in list(ctx.registry.values()):
        for fiber in list(runtime.fibers):
            await fiber


async def build_app(workspace=None, policy_config=None):
    """用代码挂载插件树；真实项目里这一段就是 cordis.yml（见第 6 章）。"""
    ctx = Context()
    ctx.plugin(Sessions)
    ctx.plugin(Tools)
    ctx.plugin(LLM)
    ctx.plugin(Agents, {'max_steps': 3})
    ctx.plugin(builtin_tools, {'workspace': workspace or os.getcwd()})
    ctx.plugin(policy, policy_config or {})
    ctx.plugin(telemetry)
    await settle(ctx)
    return ctx


def show_session(ctx):
    """把会话日志里与工具相关的部分打出来（这就是"可回放"的来源）。"""
    for event in ctx.sessions.log:
        if event['type'] == 'assistant/message' and event['message'].get('tool_calls'):
            for call in event['message']['tool_calls']:
                print(f"  step: 模型请求工具 {call['name']}({call['arguments']})")
        elif event['type'] == 'tool/result':
            result = event['result']
            text = (result.get('content') or result.get('error') or '')[:70].replace('\n', ' ')
            print(f"  result: {text}")


async def main():
    task = ' '.join(sys.argv[1:]) or '读取 pyproject.toml'

    print(f'--- 应用 1：默认策略（shell 在 deny 列表里）---')
    print(f'任务: {task}')
    ctx = await build_app()
    print('agent>', await ctx.agents.run(task))
    show_session(ctx)

    print('\n--- 应用 2：同一个 agent、同样的任务，只换策略插件（放行 shell）---')
    ctx2 = await build_app(policy_config={'deny': [], 'allow': ['shell']})
    print('agent>', await ctx2.agents.run('运行 echo hello-cordis'))
    show_session(ctx2)

    print('\n提示：把策略换成"人工审批"只需再写一个 tools/pre-execute 监听器；'
          '把 mock 模型换成真实模型只需注册新的 llm 适配器。agent 循环不用改一行。')
    print('完整版本见 examples/agent/（配置驱动 + 真实模型适配器 + REPL）。')


if __name__ == '__main__':
    asyncio.run(main())
