"""``ctx.systemPrompt``：提示词片段的组装。

每个插件都可以贡献自己的提示词片段，注册即 effect：插件卸载时片段自动消失。
这与 dsh 的 ``ctx.systemPrompt`` 子系统对应——**添加模型可见上下文**意味着
在提示词组装里加一段，而不是拼接字符串。
"""

import time

from cordis import Service

name = 'systemPrompt'


class SystemPromptService(Service):
    def __init__(self, ctx, config=None):
        super().__init__(ctx, 'systemPrompt')
        self.sections: list[dict] = []
        base = (config or {}).get('base')
        if base:
            self.section('base', base, order=0)

    def section(self, name: str, text: str, order: int = 100) -> dict:
        """注册一个提示词片段；随调用方插件卸载自动移除。"""
        entry = {'name': name, 'text': text, 'order': order, 'ts': time.time()}
        self.sections.append(entry)
        self.sections.sort(key=lambda item: (item['order'], item['ts']))
        self.ctx.cleanup(lambda: self.sections.remove(entry) if entry in self.sections else None,
                         f'ctx.systemPrompt.section({name!r})')
        return entry

    def render(self) -> str:
        return '\n\n'.join(item['text'].strip() for item in self.sections if item['text'].strip())


plugin = SystemPromptService
