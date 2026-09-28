"""``ctx.llm``：模型适配器 seam（接口声明 + 适配器注册）。

一个 **seam（接缝）** 包含三种角色：声明接口的服务、实现它的提供方、以及使用它
的消费方。这里 ``LLMService`` 是接口与注册表；``llm_mock`` / ``llm_openai`` 是
提供方；agent 循环是消费方。替换提供方不需要改动消费方一行代码。
"""

from cordis import Service
from cordis.utils import maybe_await

name = 'llm'


class LLMService(Service):
    def __init__(self, ctx, config=None):
        super().__init__(ctx, 'llm')
        self.default = dict(config or {})
        self.adapters = {}

    def adapter(self, provider):
        """装饰器：注册一个模型适配器 ``async (request) -> assistant message``。"""
        def decorator(handler):
            self.adapters[provider] = handler
            self.ctx.cleanup(lambda: self.adapters.pop(provider, None), f'ctx.llm.adapter({provider!r})')
            self.ctx.logger.debug('llm adapter registered: %s', provider)
            return handler
        return decorator

    async def chat(self, messages=None, tools=None, **options) -> dict:
        """发起一次模型调用；返回助手消息 ``{role, content, tool_calls}``。

        请求会先经过 ``llm/request`` waterfall，插件可以整体改写它
        （切换模型、裁剪历史、注入上下文……）。
        """
        request = {**self.default, **options, 'messages': list(messages or []), 'tools': list(tools or [])}

        async def send(request_, next_):
            provider = request_.get('provider', 'mock')
            handler = self.adapters.get(provider)
            if handler is None:
                raise LookupError(f'没有可用的模型适配器: {provider!r}（已注册: {sorted(self.adapters)}）')
            return await maybe_await(handler(request_))

        return await maybe_await(self.ctx.waterfall('llm/request', request, send))


plugin = LLMService
