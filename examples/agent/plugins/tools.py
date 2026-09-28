"""``ctx.tools``：工具注册表与带把关的执行流水线。

这是 dsh ``core/tools`` 子系统的微缩版，展示了两个关键设计：

1. **注册是 effect**：``ctx.tools.register(...)`` / ``@ctx.tools.tool(...)``
   通过调用方上下文注册，插件卸载时工具自动消失（无需手动清理）。
2. **执行是流水线**：每个工具调用依次经过三个 waterfall 事件——

   ``tools/pre-execute`` → ``tools/execute`` → ``tools/post-execute``

   策略插件可以在 ``pre-execute`` 里改写参数、直接短路（拒绝），
   或者在 ``post-execute`` 里审查/脱敏结果，而**不需要修改工具本身**。
"""

import itertools

from cordis import Service
from cordis.utils import maybe_await

name = 'tools'

_call_counter = itertools.count(1)


class Tool:
    def __init__(self, name, description, parameters=None, execute=None, dangerous=False):
        self.name = name
        self.description = description
        self.parameters = parameters or {'type': 'object', 'properties': {}}
        self.execute = execute
        self.dangerous = dangerous  #: 危险工具：建议由审批策略把关

    def schema(self) -> dict:
        return {'name': self.name, 'description': self.description, 'parameters': self.parameters}


class ToolCall:
    """一次工具调用的完整记录（贯穿整条流水线，也是会话日志的一部分）。"""

    def __init__(self, name, arguments, ctx=None, id=None):
        self.id = id or f'call_{next(_call_counter)}'
        self.name = name
        self.arguments = dict(arguments or {})
        self.ctx = ctx
        self.result = None

    def __repr__(self):
        return f'<ToolCall {self.name}({self.arguments})>'


class ToolsService(Service):
    def __init__(self, ctx, config=None):
        super().__init__(ctx, 'tools')
        self.store: dict[str, Tool] = {}

    # ------------------------------------------------------------------ 注册
    def register(self, tool: Tool) -> Tool:
        if tool.name in self.store:
            raise ValueError(f'tool {tool.name!r} already registered')
        self.store[tool.name] = tool
        self.ctx.cleanup(lambda: self.store.pop(tool.name, None), f'ctx.tools.register({tool.name!r})')
        self.ctx.logger.debug('tool registered: %s', tool.name)
        return tool

    def unregister(self, name: str) -> None:
        self.store.pop(name, None)

    def tool(self, name, description, parameters=None, dangerous=False):
        """装饰器形式：``@ctx.tools.tool('read_file', '读取文件', {...})``。"""
        def decorator(func):
            self.register(Tool(name, description, parameters, func, dangerous))
            return func
        return decorator

    # ------------------------------------------------------------------ 查询
    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self.store.values()]

    def __contains__(self, name):
        return name in self.store

    # ------------------------------------------------------------------ 执行
    async def execute(self, tool: str, arguments: dict | None = None, ctx=None, call_id=None) -> dict:
        """执行一次工具调用，返回 ``{ok, content?, error?}`` 结果字典。"""
        call = ToolCall(tool, arguments, ctx if ctx is not None else self.ctx, call_id)
        self.ctx.emit('tool/call', call)

        async def execute_tool(call_: ToolCall, next_) -> dict:
            entry = self.store.get(call_.name)
            if entry is None:
                return {'ok': False, 'error': f'unknown tool: {call_.name}'}
            if entry.execute is None:
                return {'ok': False, 'error': f'tool {call_.name} has no executor'}
            try:
                output = await maybe_await(entry.execute(call_.ctx, **call_.arguments))
                return {'ok': True, 'content': output if isinstance(output, str) else str(output)}
            except TypeError as error:
                return {'ok': False, 'error': f'工具参数错误: {error}'}
            except Exception as error:
                return {'ok': False, 'error': f'{type(error).__name__}: {error}'}

        async def keep(call_, result_, next_):
            return result_

        try:
            result = await maybe_await(self.ctx.waterfall('tools/pre-execute', call, execute_tool))
            result = await maybe_await(self.ctx.waterfall('tools/post-execute', call, result, keep))
            if result is None:
                result = {'ok': False, 'error': 'tool execution was vetoed'}
        except Exception as error:
            result = {'ok': False, 'error': f'{type(error).__name__}: {error}'}

        call.result = result
        self.ctx.emit('tool/result', call)
        return result


plugin = ToolsService
