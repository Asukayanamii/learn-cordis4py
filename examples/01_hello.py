"""01 · 第一个插件：函数插件与上下文。

插件就是"接受 ctx 的函数"。通过 ctx 注册的一切，都会在插件卸载时自动清理。

运行：python examples/01_hello.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cordis import Context, Service  # noqa: E402


def hello(ctx):
    """最简单的插件：只打印一行。"""
    ctx.logger.info('hello from my first plugin')


def counter(ctx, config):
    """带配置的插件：配置由 fiber.config 校验后传入。"""
    ctx.logger.info('counter 启动，起始值=%s', (config or {}).get('start', 0))


class Greeter(Service):
    """类插件（Service 子类）：向其他插件提供能力。"""

    def __init__(self, ctx):
        super().__init__(ctx, 'greeter')

    def greet(self, who):
        return f'Hello, {who}!'


def consumer(ctx):
    ctx.logger.info(ctx.greeter.greet('world'))


async def main():
    ctx = Context()

    fiber = ctx.plugin(hello)
    await fiber
    print('1) 函数插件：', fiber.state.name, '（uid =', fiber.uid, '）')

    fiber2 = ctx.plugin(counter, {'start': 3})
    await fiber2
    print('2) 带配置的插件：', fiber2.config)

    # 类插件注册为服务；消费方声明 inject 后等到服务就绪才启动
    await ctx.plugin(Greeter)
    fiber3 = ctx.inject(['greeter'], consumer)
    await fiber3
    print('3) 服务与依赖注入：', ctx.greeter.greet('cordis'))

    # 卸载：注册过的东西一起撤销
    await fiber3.dispose()
    print('4) 卸载后 consumer 的状态：', fiber3.state.name)


asyncio.run(main())
