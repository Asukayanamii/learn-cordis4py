"""人设与请求调优插件：向提示词组装贡献片段，并拦截模型请求。

演示 dsh 的"添加模型可见上下文"与"拦截请求"两条扩展路径：
- ``ctx.systemPrompt.section(...)``：注册提示词片段（随插件卸载撤销）；
- ``agent/request`` waterfall：不改动 agent 循环本身，只调整请求参数。
"""

inject = ['systemPrompt']

name = 'persona'


def apply(ctx, config=None):
    config = config or {}
    persona = config.get('persona') or (
        '你是一个务实的工程助手。回答简洁、直接；需要动手时先调用工具，再总结结果。'
    )
    ctx.systemPrompt.section('persona', persona, order=10)

    async def tune(request, next_):
        request.setdefault('temperature', 0.2)
        if config.get('max_tokens'):
            request.setdefault('max_tokens', int(config['max_tokens']))
        return await next_()

    ctx.on('agent/request', tune)
