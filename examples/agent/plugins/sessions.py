"""``ctx.sessions``：仅追加的会话日志。

会话日志是**模型所见上下文的唯一来源**（dsh 的原则："模型可见即已记录"）：
模型历史由 ``derive_messages()`` 从日志投影出来，而不是由 agent 循环就地拼装。
这带来三个好处：

1. 恢复/续跑只需要重新投影日志；
2. 任何插件都能观察 ``session/event`` 做出反应（UI、遥测、持久化）；
3. 上下文工程（裁剪、压缩、注入）变成对日志的操作，可测试、可回放。
"""

import json
import os
import time
import uuid

from cordis import Service

name = 'sessions'


class SessionEvent(dict):
    """一条持久事实：``{type, seq, ts, ...payload}``。"""

    @property
    def type(self):
        return self['type']


class Session:
    def __init__(self, id, meta=None):
        self.id = id
        self.meta = dict(meta or {})
        self.events: list[SessionEvent] = []

    # ------------------------------------------------------------------ 写入
    def append(self, type_, **payload) -> SessionEvent:
        event = SessionEvent(type=type_, seq=len(self.events) + 1, ts=time.time(), **payload)
        self.events.append(event)
        return event

    # ------------------------------------------------------------------ 投影
    def derive_messages(self) -> list[dict]:
        """把持久事件投影为模型消息历史。"""
        messages: list[dict] = []
        for event in self.events:
            if event.type == 'user/message':
                messages.append({'role': 'user', 'content': event['content']})
            elif event.type == 'system/message':
                messages.append({'role': 'system', 'content': event['content']})
            elif event.type == 'assistant/message':
                messages.append(event['message'])
            elif event.type == 'tool/result':
                messages.append({
                    'role': 'tool',
                    'tool_call_id': event['call']['id'],
                    'content': str(event['result'].get('content', event['result'].get('error', ''))),
                })
        return messages

    def transcript(self) -> str:
        lines = []
        for event in self.events:
            if event.type == 'user/message':
                lines.append(f"user> {event['content']}")
            elif event.type == 'assistant/message':
                message = event['message']
                if message.get('content'):
                    lines.append(f"agent> {message['content']}")
                for call in message.get('tool_calls') or []:
                    lines.append(f"  · 调用工具 {call['name']}({json.dumps(call['arguments'], ensure_ascii=False)})")
            elif event.type == 'tool/result':
                text = str(event['result'].get('content') or event['result'].get('error') or '')
                lines.append(f"  · 工具结果 {event['call']['name']}: {text[:120]}")
        return '\n'.join(lines)

    # ------------------------------------------------------------------ 持久化
    def save(self, directory: str) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f'{self.id}.jsonl')
        with open(path, 'w', encoding='utf-8') as stream:
            for event in self.events:
                stream.write(json.dumps(event, ensure_ascii=False) + '\n')
        return path


class SessionsService(Service):
    """会话仓库：``ctx.sessions.create()`` / ``ctx.sessions.get(id)``。"""

    def __init__(self, ctx, config=None):
        super().__init__(ctx, 'sessions')
        self.directory = (config or {}).get('directory')
        self.store: dict[str, Session] = {}

    def create(self, id=None, meta=None) -> Session:
        session = Session(id or uuid.uuid4().hex[:12], meta)
        self.store[session.id] = session
        self.ctx.emit('session/created', session)
        return session

    def get(self, id) -> Session:
        return self.store.get(id)

    def append(self, session: Session, type_, **payload) -> SessionEvent:
        event = session.append(type_, **payload)
        self.ctx.emit('session/event', session, event)
        return event

    def save(self, session: Session) -> str:
        if self.directory:
            return session.save(self.directory)
        return ''


#: loader 优先读取 ``plugin`` 导出；类插件（Service 子类）会以条目 config 构造
plugin = SessionsService
