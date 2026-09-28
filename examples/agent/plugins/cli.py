"""CLI 插件：交互式 REPL 与一次性任务。

CLI 自身也是插件：它只依赖 ``ctx.agents``，因此可以被替换成 Web UI、
headless runner 或 SDK 服务器，而 agent 核心一行都不用改。
"""

import asyncio

from cordis import Service

name = 'cli'

inject = ['agents']


class CliService(Service):
    def __init__(self, ctx, config=None):
        super().__init__(ctx, 'cli')
        self.config = dict(config or {})
        self.agent = None

    def session_agent(self):
        if self.agent is None:
            # 读取自己的依赖 → owner_ctx（服务自身的上下文，已注入 agents）
            self.agent = self.owner_ctx.agents.create(profile=self.config.get('profile', 'default'))
        return self.agent

    async def run_once(self, prompt: str) -> str:
        """跑一个一次性任务（独立会话）。"""
        agent = self.owner_ctx.agents.create()
        self.owner_ctx.logger('cli').info('session %s', agent.id)
        reply = await agent.send(prompt)
        return reply

    async def repl(self) -> None:
        """交互式对话：整个 REPL 共享一个会话，历史由会话日志投影。"""
        agent = self.session_agent()
        print(f'会话 {agent.id} 已开始。输入任务回车执行；/transcript 查看记录，/quit 退出。')
        while True:
            try:
                text = (await asyncio.to_thread(input, 'you> ')).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text in ('/quit', '/exit'):
                break
            if text == '/transcript':
                print(agent.session.transcript() or '(暂无记录)')
                continue

            prefix = self.config.get('prompt_prefix')
            reply = await agent.send(f'{prefix}{text}' if prefix else text)
            print(f'agent> {reply}')


plugin = CliService
