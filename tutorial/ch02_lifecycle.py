"""第 2 章 · 生命周期与 effect：让注册真正可逆。

第 1 章的清理只是"账本"，本章把它做实，并引入 Cordis 的核心概念 **fiber**：
一个已加载插件实例的运行时句柄。

    PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED
                   ↘ FAILED

要点：
- ``ctx.plugin()`` 返回 fiber；``await fiber`` 等到加载完成，失败会抛出；
- **effect**：注册（监听器、定时器、子插件、服务）都会挂到当前 fiber 上，
  卸载时按**逆序**释放，不需要手写 remove/cancel；
- 子插件是父插件 effect 的一部分，因此**递归卸载**；
- 清理函数可以是异步的：卸载会等待它们完成。

对照仓库里的完整实现：``cordis/fiber.py``。

运行：python tutorial/ch02_lifecycle.py
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager


class State:
    PENDING = 'PENDING'
    LOADING = 'LOADING'
    ACTIVE = 'ACTIVE'
    UNLOADING = 'UNLOADING'
    DISPOSED = 'DISPOSED'
    FAILED = 'FAILED'


class Context:
    def __init__(self, parent: 'Context | None' = None, **meta):
        self.parent = parent
        self.meta = dict(meta)
        self.effects: list[dict] = []     # 本作用域的 effect 账本
        self.fiber: Fiber = meta.get('fiber')
        if parent is None and self.fiber is None:
            # 根 fiber：uid 为 0，永远是 ACTIVE；卸载根 = 重启整个应用
            self.fiber = Fiber.root(self)

    # ---------------------------------------------------------------- 作用域
    def extend(self, **meta) -> 'Context':
        child = Context(self, **meta)
        if 'fiber' not in meta:
            child.fiber = self.fiber          # 子作用域默认共享父级的 fiber
        return child

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        parent = self.__dict__.get('parent')
        if parent is not None and hasattr(parent, name):
            return getattr(parent, name)
        raise AttributeError(name)

    # ---------------------------------------------------------------- effect
    def effect(self, execute, label: str = 'effect'):
        """运行 setup 并登记清理函数；返回可撤销句柄。"""
        fiber = self.fiber
        if fiber is None:
            raise RuntimeError('这个上下文没有 fiber，无法注册 effect')
        result = execute()
        if result is None:
            return fiber._push_effect(label, [])
        # 注意顺序：上下文管理器本身也可能是可调用对象（contextlib 的实现如此），
        # 所以必须先判断 __enter__/__exit__，再判断 callable。
        if hasattr(result, '__enter__') and hasattr(result, '__exit__'):
            result.__enter__()
            return fiber._push_effect(label, [lambda: result.__exit__(None, None, None)])
        if callable(result):
            return fiber._push_effect(label, [result])
        raise TypeError('effect 必须返回 None、清理函数或上下文管理器')

    def cleanup(self, disposer, label: str = 'cleanup'):
        """直接登记一个清理函数（不立即执行）。"""
        fiber = self.fiber
        if fiber is None:
            raise RuntimeError('这个上下文没有 fiber，无法注册 effect')
        return fiber._push_effect(label, [disposer])

    # ---------------------------------------------------------------- 插件
    def plugin(self, plugin, config=None) -> 'Fiber':
        return Fiber(self, plugin, config)


class ContextManager:
    """上下文管理器包装：让 ``ctx.effect(cm)`` 之类的写法更自然。"""

    def __init__(self, cm):
        self.cm = cm


class Effect:
    """一组清理函数的句柄：逆序释放、幂等。"""

    def __init__(self, label, disposers):
        self.label = label
        self.disposers = list(disposers)
        self.done = False

    def release(self):
        """开始释放：同步部分立即执行，异步部分收集起来返回。"""
        if self.done:
            return None
        self.done = True
        pending = []
        for disposer in reversed(self.disposers):    # 逆序！
            result = disposer()
            if asyncio.iscoroutine(result):
                pending.append(result)
        self.disposers.clear()
        if pending:
            async def wait():
                await asyncio.gather(*pending)
            return wait()
        return None


class Fiber:
    """一个已加载插件实例：状态机 + effect 账本。"""

    def __init__(self, parent: Context, plugin, config=None):
        self.parent = parent
        self.plugin = plugin
        self.name = getattr(plugin, 'name', None) or getattr(plugin, '__name__', 'anonymous')
        self.config = config
        self.state = State.PENDING
        self.effects: list[Effect] = []
        self.error: Exception | None = None
        self.ctx = parent.extend(fiber=self)
        self._loading: asyncio.Task | None = None

        # 关键：把"卸载自己"登记为父级的一个 effect —— 子插件随父级递归卸载
        self._handle = parent.effect(lambda: lambda: self._dispose_sync(), label='ctx.plugin()')
        self._start()

    @classmethod
    def root(cls, ctx: Context) -> 'Fiber':
        """根 fiber：不属于任何插件，永远 ACTIVE。"""
        fiber = object.__new__(cls)
        fiber.parent = ctx
        fiber.plugin = None
        fiber.name = 'root'
        fiber.config = None
        fiber.state = State.ACTIVE
        fiber.effects = []
        fiber.error = None
        fiber.ctx = ctx
        fiber._loading = None
        fiber._handle = None
        return fiber

    # ---------------------------------------------------------------- 加载
    def _start(self):
        self.state = State.PENDING          # 等待依赖（第 3 章细讲）

        async def load():
            self.state = State.LOADING
            try:
                callback = self.plugin.apply if hasattr(self.plugin, 'apply') else self.plugin
                result = callback(self.ctx, self.config) if _accepts_config(callback) else callback(self.ctx)
                if asyncio.iscoroutine(result):
                    await result
                self.state = State.ACTIVE
            except Exception as error:
                self.error = error
                self.state = State.FAILED

        self._loading = asyncio.ensure_future(load())

    def __await__(self):
        yield from self._wait().__await__()
        if self.error is not None:
            raise self.error
        return self

    async def _wait(self):
        while self._loading is not None and not self._loading.done():
            await self._loading
        return self

    # ---------------------------------------------------------------- 卸载
    def _push_effect(self, label, disposers) -> Effect:
        if self.state in (State.UNLOADING, State.DISPOSED):
            raise RuntimeError('插件已卸载，无法再注册 effect')
        effect = Effect(label, disposers)
        self.effects.append(effect)
        return effect

    def _dispose_sync(self):
        """同步前缀：立刻置为 UNLOADING 并释放所有 effect。"""
        if self.state == State.DISPOSED:
            return None
        self.state = State.UNLOADING
        pending = []
        for effect in reversed(self.effects):     # 逆序释放
            result = effect.release()
            if result is not None:
                pending.append(result)
        self.effects.clear()

        async def finish():
            if pending:
                await asyncio.gather(*pending)
            self.state = State.DISPOSED

        self._disposing = asyncio.ensure_future(finish())
        return self._disposing

    def dispose(self):
        return self._dispose_sync()

    def __repr__(self):
        return f'<Fiber {self.name} {self.state}>'


def _accepts_config(callback) -> bool:
    """按签名判断插件是否需要 config 参数（Python 与 JS 的差异之一）。"""
    import inspect
    try:
        params = [p for p in inspect.signature(callback).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        return True
    return len(params) >= 2


# ---------------------------------------------------------------------- 演示
@contextmanager
def heartbeat(name='heartbeat'):
    print(f'  [{name}] setup')
    try:
        yield
    finally:
        print(f'  [{name}] teardown')


async def main():
    ctx = Context()

    print('--- 1) 状态机：PENDING → LOADING → ACTIVE ---')

    def demo(ctx):
        print('  插件主体运行，state =', ctx.fiber.state)
        ctx.cleanup(lambda: print('  [cleanup A] 先注册，后清理'))
        ctx.cleanup(lambda: print('  [cleanup B] 后注册，先清理'))
        ctx.effect(lambda: heartbeat('timer'))
        ctx.plugin(lambda child: child.cleanup(lambda: print('  [child] 子插件清理')))

    fiber = ctx.plugin(demo)
    print('  刚挂载时 state =', fiber.state)
    await fiber
    print('  加载完成后 state =', fiber.state)
    print('  effect 列表 =', [effect.label for effect in fiber.effects])

    print('--- 2) 卸载：逆序释放 + 递归卸载子插件 ---')
    await fiber.dispose()
    await asyncio.sleep(0)
    print('  卸载后 state =', fiber.state)

    print('--- 3) 插件抛异常 → FAILED（不会静默跳过）---')

    def broken(ctx):
        raise RuntimeError('apply 爆炸了')

    broken_fiber = ctx.plugin(broken)
    try:
        await broken_fiber
    except RuntimeError as error:
        print('  捕获启动错误:', error, '/ state =', broken_fiber.state)

    print('--- 4) 已卸载的插件不能再注册 ---')
    try:
        fiber.ctx.cleanup(lambda: None)
    except RuntimeError as error:
        print('  ', error)


if __name__ == '__main__':
    asyncio.run(main())
