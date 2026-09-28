"""``ctx.agents``：Agent 接口与默认驱动器（agent loop）。

轮次流程是 dsh ``core/agent-loop`` 的微缩版：

.. code-block:: text

    turn/start
      领取输入 → agent/pre-step（waterfall：可拒绝/改写）
      组装提示词片段 + 工具 schema
      agent/request（waterfall：可改写模型请求）
      模型流式响应 → assistant/message（持久事件）
      tool/call* → tools/* 流水线 → tool/result*（持久事件）
      还有工具调用 → 下一个 step；否则结束
    agent/turn-stopping（serial：可让循环停下）
    turn/end

要点：**模型可见即已记录**——进入请求的一切都先写进会话日志，
模型历史由日志投影而来，因此恢复、fork、遥测都只依赖日志。
"""

from cordis import Service
from cordis.utils import maybe_await

name = 'agents'

inject = ['llm', 'tools', 'sessions', 'systemPrompt']


class AgentsService(Service):
    """依赖声明：``inject`` 让本插件等到这些服务就绪后才启动。"""

    inject = ['llm', 'tools', 'sessions', 'systemPrompt']

    def __init__(self, ctx, config=None):
        super().__init__(ctx, 'agents')
        self.defaults = dict(config or {})
        self.store: dict[str, 'Agent'] = {}

    def create(self, session=None, **meta) -> 'Agent':
        # 读取自己的依赖 / 登记服务自身的资源 → 用 owner_ctx（服务自身的上下文）
        owner = self.owner_ctx
        session = session or owner.sessions.create(meta=meta)
        # 每个 agent 拥有自己的 fiber（"按 agent 作用域的注册"），
        # 因此 agent 卸载时，插件为它注册的东西会一起撤销。
        scope = owner.plugin({'name': f'agent/{session.id}', 'apply': lambda ctx, config: None})
        agent = Agent(owner, session, self.defaults, scope)
        self.store[agent.id] = agent
        owner.cleanup(lambda: self.store.pop(agent.id, None) if agent.id in self.store else None,
                      f'ctx.agents.create({agent.id!r})')
        owner.emit('agent/created', agent)
        return agent

    def get(self, agent_id):
        return self.store.get(agent_id)


class Agent:
    """一个会话上的智能体。每个 agent 拥有自己的子上下文（``agent.ctx``），

    通过它注册的监听器/工具/提示词片段都归属该 agent 的 fiber。
    """

    def __init__(self, ctx, session, defaults, scope):
        self.service_ctx = ctx
        self.session = session
        self.defaults = dict(defaults)
        self.id = session.id
        self.inbox: list[str] = []
        self.pending_injections: list[str] = []
        self.state = 'idle'
        self.last_reply: str | None = None
        self.scope = scope  # type: Fiber —— agent 自己的 fiber
        self.ctx = scope.ctx.extend({'agent': self})
        self.log = ctx.logger(f'agent:{self.id}')

    async def dispose(self) -> None:
        """卸载该 agent 的作用域（agent 专属注册随之撤销）。"""
        result = self.scope.dispose()
        if result is not None:
            await result

    # ------------------------------------------------------------------ 对外 API
    async def send(self, text: str) -> str:
        """投递一条用户消息，跑完一个轮次，返回最终回复。"""
        self.inbox.append(text)
        await self.run_turn()
        return self.last_reply or '（没有产出回复）'

    def inject(self, text: str) -> None:
        """注入只有模型可见的上下文，落到下一次请求中。"""
        self.pending_injections.append(text)

    # ------------------------------------------------------------------ 轮次
    async def run_turn(self) -> None:
        sessions = self.service_ctx.sessions
        sessions.append(self.session, 'turn/start')
        self.state = 'running'

        async def accept(message, next_):
            return {'content': message['content']}

        try:
            while self.inbox:
                text = self.inbox.pop(0)
                decision = await maybe_await(self.ctx.waterfall(
                    'agent/pre-step', {'content': text}, accept))
                if not decision:
                    self.log.warn('输入被 agent/pre-step 拒绝')
                    continue
                sessions.append(self.session, 'user/message', content=decision['content'])
                await self.run_steps()
        finally:
            self.state = 'idle'
            sessions.append(self.session, 'turn/end')
            saved = sessions.save(self.session)
            if saved:
                self.log.debug('会话已保存: %s', saved)
            await maybe_await(self.ctx.serial('agent/turn-stopping', self))

    async def run_steps(self) -> None:
        max_steps = int(self.defaults.get('max_steps', 4))
        sessions = self.service_ctx.sessions

        async def use_request(request, next_):
            return request

        for step in range(max_steps):
            sessions.append(self.session, 'step/start', step=step)
            request = self._assemble_request()
            # agent/request：路由/策略插件可以整体改写请求（换模型、裁剪上下文……）
            request = await maybe_await(self.ctx.waterfall('agent/request', request, use_request))

            reply = await self.ctx.llm.chat(**request)
            self.ctx.emit('agent/assistant-stream', self, {'type': 'message', 'message': reply})
            sessions.append(self.session, 'assistant/message', message=reply)
            if reply.get('content'):
                self.last_reply = reply['content']

            calls = reply.get('tool_calls') or []
            if not calls:
                sessions.append(self.session, 'step/end', step=step)
                break

            for call in calls:
                result = await self.ctx.tools.execute(
                    call['name'], call.get('arguments'), ctx=self.ctx, call_id=call.get('id'))
                sessions.append(self.session, 'tool/result', call=call, result=result)
            sessions.append(self.session, 'step/end', step=step)

            stopping = await self.ctx.serial('agent/turn-stopping', self)
            if stopping:
                self.log.info('agent/turn-stopping 让循环停下')
                break
        else:
            self.log.warn('达到最大步数 %d，本轮结束', max_steps)

        if self.last_reply is None:
            self.last_reply = '（模型只发起了工具调用，没有给出文本回复）'

    def _assemble_request(self) -> dict:
        messages = [{'role': 'system', 'content': self.ctx.systemPrompt.render()}]
        messages.extend(self.session.derive_messages())
        for injection in self.pending_injections:
            messages.append({'role': 'user', 'content': f'[系统注入] {injection}'})
        self.pending_injections.clear()
        return {
            **self.defaults,
            'messages': messages,
            'tools': self.ctx.tools.schemas(),
        }


plugin = AgentsService
