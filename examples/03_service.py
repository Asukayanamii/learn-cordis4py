"""03 · 服务与依赖注入：能力通过 ctx 共享。

- 服务是插件提供、其他插件消费的具名能力（``ctx.greeter``）；
- 消费者用 ``inject`` 声明依赖，框架保证依赖就绪后才启动；
- 服务消失时依赖方自动卸载，服务恢复后自动重新加载（配置替换就靠这个）；
- ``ctx.service.method()`` 调用时，服务内的 ``self.ctx`` 是**调用方**上下文，
  因此服务里的注册会挂到调用方 fiber 上（工具注册、监听器注册都得益于此）。

运行：python examples/03_service.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cordis import Context, Service  # noqa: E402


class StorageA(Service):
    """提供方 A：内存存储。"""

    def __init__(self, ctx):
        super().__init__(ctx, 'storage')
        self.data = {}

    def put(self, key, value):
        self.data[key] = value
        return f'已写入 {key}'

    def get(self, key):
        return self.data.get(key)


class StorageB(Service):
    """提供方 B：带前缀的存储（用于演示替换提供方）。"""

    def __init__(self, ctx):
        super().__init__(ctx, 'storage')
        self.data = {}

    def put(self, key, value):
        self.data[key] = f'B:{value}'
        return f'已写入 {key}（提供方 B）'

    def get(self, key):
        return self.data.get(key)


def consumer(ctx):
    # ctx.storage 返回"绑定到调用方"的服务代理：isinstance 仍然成立，
    # 且服务方法内的 self.ctx 是调用方上下文（注册会挂到本插件上）。
    print('  consumer 加载，storage 是 StorageA:', isinstance(ctx.storage, StorageA))
    print('  ', ctx.storage.put('answer', 42))
    ctx.cleanup(lambda: print('  consumer 卸载（storage 不可用时自动发生）'))


async def main():
    ctx = Context()

    print('--- 消费方先启动：保持 PENDING，直到服务出现 ---')
    fiber = ctx.inject(['storage'], consumer)
    await fiber
    print('  consumer 状态:', fiber.state.name)

    print('--- 挂载提供方 A：消费方自动激活 ---')
    provider = ctx.plugin(StorageA)
    await provider
    await fiber
    print('  consumer 状态:', fiber.state.name)

    print('--- 替换为提供方 B：消费方自动卸载并重新加载 ---')
    await provider.dispose()
    await fiber
    print('  替换期间 consumer 状态:', fiber.state.name)
    await ctx.plugin(StorageB)
    await fiber
    print('  consumer 状态:', fiber.state.name)

    print('--- 可选依赖：ctx.get() 探测，不需要 inject（返回原始值，不做调用方绑定）---')
    print('  ctx.get("storage") ->', type(ctx.get('storage')).__name__)
    print('  ctx.get("nope")    ->', ctx.get('nope'))


asyncio.run(main())
