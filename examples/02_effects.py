"""02 · 生命周期与 effect：可逆副作用。

Cordis 的插件可能因为改配置、热重载、依赖消失或显式卸载而停止。
凡是"注册"（监听器、定时器、子插件、服务……）都应该是 **effect**：
卸载时按注册的逆序自动撤销，永远不用手写 remove_listener / cancel()。

Python 里可以用三种写法表达 effect 主体：
    1) ctx.cleanup(fn)          —— 直接登记清理函数
    2) ctx.effect(lambda: dis)  —— 立即执行 setup，返回 disposer
    3) ctx.effect(lambda: cm)   —— 上下文管理器（setup/teardown 最自然）

运行：python examples/02_effects.py
"""

import asyncio
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cordis import Context  # noqa: E402


@contextmanager
def heartbeat(name='heartbeat'):
    print(f'  [{name}] 启动（setup）')
    try:
        yield
    finally:
        print(f'  [{name}] 停止（teardown）')


def lifecycle_demo(ctx):
    print('lifecycle_demo 正在加载…')
    # 清理函数按注册顺序的逆序执行
    ctx.cleanup(lambda: print('  [cleanup A] 第一个注册，最后清理'))
    ctx.cleanup(lambda: print('  [cleanup B] 第二个注册'))
    ctx.effect(lambda: heartbeat('timer'))
    # 子插件：随父插件一起卸载（递归）
    ctx.plugin(lambda child: child.cleanup(lambda: print('  [child] 子插件清理')))


async def main():
    ctx = Context()

    # 1) 观察状态机：PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED
    ctx.on('internal/status', lambda fiber, old: print(f'  [{fiber.name}] {old.name} -> {fiber.state.name}'))

    print('--- 挂载插件 ---')
    fiber = ctx.plugin(lifecycle_demo)
    await fiber
    print('  当前 effect 列表:', [(meta.label) for meta in fiber.get_effects()])

    print('--- 卸载插件（逆序释放 + 递归卸载子插件）---')
    await fiber.dispose()
    print('  disposer 是幂等的：再次调用不会重复清理')
    fiber.dispose()

    print('--- 插件抛异常 → FAILED，而不是静默跳过 ---')
    def broken(ctx):
        raise RuntimeError('apply 爆炸了')

    broken_fiber = ctx.plugin(broken)
    try:
        await broken_fiber
    except RuntimeError as error:
        print('  捕获到启动错误:', error, '/ 状态:', broken_fiber.state.name)


asyncio.run(main())
