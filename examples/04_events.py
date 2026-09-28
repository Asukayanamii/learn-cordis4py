"""04 · 事件：五种分发模式。

| 模式      | 调用                        | 语义                                   |
|-----------|-----------------------------|----------------------------------------|
| emit      | ctx.emit(name, ...)         | 同步广播，不等待、不收集返回值         |
| parallel  | await ctx.parallel(...)     | 所有监听器并发运行并一起等待           |
| serial    | await ctx.serial(...)       | 依次等待，首个非假返回值胜出           |
| bail      | ctx.bail(name, ...)         | serial 的同步版本                      |
| waterfall | ctx.waterfall(name, ...,fn) | 环绕中间件：不调用 next() 即短路（否决）|

waterfall 是"拦截/策略"的实现方式：只观察的监听器必须调用 next()；
拥有决策权的监听器可以直接返回，从而否决下游与默认行为。

运行：python examples/04_events.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cordis import Context  # noqa: E402
from cordis.utils import maybe_await  # noqa: E402


async def main():
    ctx = Context()

    # ---------- emit：广播 ----------
    print('--- emit ---')
    ctx.on('demo/emit', lambda value: print('  监听器 A 收到', value))
    ctx.on('demo/emit', lambda value: print('  监听器 B 收到', value))
    ctx.emit('demo/emit', 1)

    # ---------- parallel：并发 ----------
    print('--- parallel ---')
    async def slow():
        await asyncio.sleep(0.01)
        print('  慢监听器完成')

    ctx.on('demo/parallel', slow)
    ctx.on('demo/parallel', lambda: print('  快监听器完成'))
    await ctx.parallel('demo/parallel')

    # ---------- serial / bail：首个有效返回值胜出 ----------
    print('--- serial（首个非假返回值胜出）---')
    async def vote(name, answer):
        print(f'  {name} 投票: {answer}')
        return answer

    ctx.on('demo/serial', lambda: vote('A', None))
    ctx.on('demo/serial', lambda: vote('B', '通过'))
    ctx.on('demo/serial', lambda: vote('C', '不应被调用'))
    print('  结果:', await ctx.serial('demo/serial'))

    # ---------- waterfall：包裹与短路 ----------
    print('--- waterfall ---')
    async def outer(decision, next_):
        result = await next_()
        return f'[{result}]'  # 包装下游结果

    async def blocker(decision, next_):
        if decision.get('blocked'):
            print('  blocker 短路了链条（不调用 next）')
            return '** 被拒绝 **'
        return await next_()

    ctx.on('demo/waterfall', outer)
    ctx.on('demo/waterfall', blocker)

    async def default(decision, next_):
        return f"默认行为: {decision['text']}"

    print('  正常:', await maybe_await(ctx.waterfall('demo/waterfall', {'text': 'hello'}, default)))
    print('  拦截:', await maybe_await(ctx.waterfall('demo/waterfall', {'text': 'x', 'blocked': True}, default)))

    # ---------- 监听器随插件卸载 ----------
    print('--- 监听器是 effect ---')
    def plugin(ctx):
        ctx.on('demo/emit', lambda value: print('  插件监听器收到', value))

    fiber = ctx.plugin(plugin)
    await fiber
    ctx.emit('demo/emit', 2)
    await fiber.dispose()
    print('  插件卸载后：')
    ctx.emit('demo/emit', 3)


asyncio.run(main())
