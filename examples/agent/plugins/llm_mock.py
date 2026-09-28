"""Mock 模型适配器：无需 API Key 就能完整跑通 agent 循环。

它用几条正则从用户消息里识别意图并发出工具调用，工具结果回来后给出总结——
足以演示"模型 → 工具 → 模型"的完整回合，同时保持完全确定性。
"""

import itertools
import re

inject = ['llm']

_counter = itertools.count(1)

_PATTERNS = [
    ('read_file', re.compile(r'(?:读取|读一下|看看|read)\s*[「\"]?([^\s」\"]+)')),
    ('list_dir', re.compile(r'(?:列出|列一下|list)\s*(?:目录|文件)?\s*[「\"]?([^\s」\"]*)')),
    ('shell', re.compile(r'(?:运行|执行|run|exec)\s+(.+)')),
    ('write_file', re.compile(r'(?:写入|保存到|write)\s*[「\"]?([^\s」\"]+)')),
]


def apply(ctx, config=None):
    max_steps = int((config or {}).get('max_steps', 3))

    @ctx.llm.adapter('mock')
    async def mock(request):
        messages = request['messages']
        user_messages = [m for m in messages if m.get('role') == 'user']
        tool_results = [m for m in messages if m.get('role') == 'tool']
        task = user_messages[-1]['content'] if user_messages else ''

        # 模型看到工具结果后给出最终回答
        if tool_results and len(tool_results) >= min(1, max_steps):
            summary = '\n'.join(
                f"- 第 {index + 1} 条工具结果：{result['content'][:200]}"
                for index, result in enumerate(tool_results[-3:])
            )
            return {
                'role': 'assistant',
                'content': f'任务「{task}」已完成。我调用了 {len(tool_results)} 次工具，结果如下：\n{summary}',
                'tool_calls': [],
            }

        if len(tool_results) >= max_steps:
            return {'role': 'assistant', 'content': '已达到最大工具调用步数，停止。', 'tool_calls': []}

        # 首次进入：解析意图
        for tool, pattern in _PATTERNS:
            match = pattern.search(task)
            if not match:
                continue
            argument = (match.group(1) or '').strip()
            if tool == 'shell':
                arguments = {'command': argument}
            elif tool == 'write_file':
                arguments = {'path': argument or 'note.txt', 'content': f'来自任务: {task}'}
            elif tool == 'list_dir':
                arguments = {'path': argument or '.'}
            else:
                arguments = {'path': argument}
            return {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{'id': f'mock_{next(_counter)}', 'name': tool, 'arguments': arguments}],
            }

        # 没有工具意图：直接回答（并提示可以试试工具）
        return {
            'role': 'assistant',
            'content': (
                f'（mock 模型）我收到了任务：「{task}」。\n'
                '试试包含「读取 <文件>」「列出目录」「运行 <命令>」的指令，我会调用对应工具。'
            ),
            'tool_calls': [],
        }
