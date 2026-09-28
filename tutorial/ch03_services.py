"""第 3 章 · 服务与依赖注入：插件之间只认"名字"。

本章给框架加上服务：

- ``ctx.provide(name, value)`` 注册一项能力，归**当前 fiber** 所有（卸载即注销）；
- ``ctx.name`` 读取服务；根上下文能读任意已注册服务；
- 插件用 ``inject = ['storage']`` 声明依赖——**依赖没就绪就保持 PENDING**，
  服务出现自动加载、服务消失自动卸载。加载顺序由依赖关系决定，而不是代码顺序。

一个刻意保留的简化：真实 Cordis 里 ``ctx.service.method()`` 会把服务方法内的
``self.ctx`` 绑定到**调用方**上下文（这样服务里的注册会挂到调用方 fiber 上，
见 ``cordis/reflect.py`` 的 ServiceBindingProxy）。mini 版直接返回服务本身，
``self.ctx`` 永远是提供方上下文。

对照仓库里的完整实现：``cordis/reflect.py``、``cordis/service.py``。

运行：python tutorial/ch03_services.py
"""

from __future__ import annotations

import asyncio
import inspect
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
        self.fiber: Fiber = meta.get('fiber')
        if parent is None and self.fiber is None:
            self.services: dict[str, dict] = {}     # 服务表（挂在根上）
            self.fibers: list[Fiber] = []           # 所有 fiber（用于依赖变更通知）
            self.fiber = Fiber.root(self)

    # ---------------------------------------------------------------- 作用域
    def extend(self, **meta) -> 'Context':
        child = Context(self, **meta)
        if 'fiber' not in meta:
            child.fiber = self.fiber
        return child

    @property
    def root(self) -> 'Context':
        ctx = self
        while ctx.parent is not None:
            ctx = ctx.parent
        return ctx

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        parent = self.__dict__.get('parent')
        if parent is not None:
            try:
                return getattr(parent, name)        # 顺着作用域链找元数据
            except AttributeError:
                pass
        root = (parent.root if parent is not None else self)
        impl = root.services.get(name)
        if impl is not None:
            return impl['value']                    # 命中了服务
        fiber = self.__dict__.get('fiber')
        if fiber is not None and name in fiber.inject:
            raise RuntimeError(f'依赖 {name!r} 尚未就绪（fiber 处于 {fiber.state}）')
        raise AttributeError(name)

    # ---------------------------------------------------------------- 服务
    def provide(self, name, value, check=None):
        """注册服务；返回撤销句柄。服务归当前 fiber 所有。"""
        fiber = self.fiber
        root = self.root

        def setup():
            root.services[name] = {'name': name, 'value': value, 'fiber': fiber, 'check': check}
            if fiber.state == State.ACTIVE:
                notify(root, name)

            def uninstall():
                root.services.pop(name, None)
                notify(root, name)      # 依赖方会因此卸载（并在服务回来时重载）

            return uninstall

        return self.effect(setup, label=f'ctx.provide({name})')

    def get(self, name, strict=True):
        """读取服务原始值（未提供返回 None；strict=False 时连未激活的也能读到）。"""
        impl = self.root.services.get(name)
        if impl is None:
            return None
        if strict and impl['fiber'].state != State.ACTIVE:
            return None
        return impl['value']

    def set(self, name, value):
        """覆盖已提供服务的值（只有提供方 fiber 能改）。"""
        impl = self.root.services.get(name)
        if impl is None:
            raise KeyError(f'服务 {name!r} 尚未提供')
        if impl['fiber'] is not self.fiber:
            raise RuntimeError(f'服务 {name!r} 由别的 fiber 提供，不能在这里修改')
        impl['value'] = value

    # ---------------------------------------------------------------- effect
    def effect(self, execute, label: str = 'effect'):
        fiber = self.fiber
        if fiber.state in (State.UNLOADING, State.DISPOSED):
            raise RuntimeError('这个 fiber 已卸载，无法注册 effect')
        result = execute()
        if result is None:
            return fiber._push_effect(label, [])
        if hasattr(result, '__enter__') and hasattr(result, '__exit__'):
            result.__enter__()
            return fiber._push_effect(label, [lambda: result.__exit__(None, None, None)])
        if callable(result):
            return fiber._push_effect(label, [result])
        raise TypeError('effect 必须返回 None、清理函数或上下文管理器')

    def cleanup(self, disposer, label: str = 'cleanup'):
        return self.fiber._push_effect(label, [disposer])

    # ---------------------------------------------------------------- 插件
    def plugin(self, plugin, config=None) -> 'Fiber':
        return Fiber(self, plugin, config)

    def inject(self, deps, callback) -> 'Fiber':
        """等待 deps 就绪后运行 callback。"""
        return self.plugin({'inject': deps, 'apply': callback,
                            'name': getattr(callback, '__name__', 'injected')})


class Effect:
    def __init__(self, label, disposers):
        self.label = label
        self.disposers = list(disposers)
        self.done = False

    def release(self):
        if self.done:
            return None
        self.done = True
        pending = []
        for disposer in reversed(self.disposers):
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
    def __init__(self, parent: Context, plugin, config=None):
        self.parent = parent
        self.plugin = plugin

        # 归一化三种插件形态：函数 / 类 / 带 apply 的对象（含 dict）
        if isinstance(plugin, dict):
            self.callback = plugin['apply']
            self.name = plugin.get('name', 'anonymous')
            self.inject = resolve_inject(plugin.get('inject'))
        else:
            self.callback = plugin.apply if hasattr(plugin, 'apply') else plugin
            self.name = getattr(plugin, 'name', None) or getattr(plugin, '__name__', 'anonymous')
            self.inject = resolve_inject(getattr(plugin, 'inject', None))

        self.config = config
        self.state = State.PENDING
        self.effects: list[Effect] = []
        self.error = None
        self.ctx = parent.extend(fiber=self)
        self._loading = None

        parent.root.fibers.append(self)
        self._handle = parent.effect(lambda: lambda: self._dispose(), label='ctx.plugin()')
        self._check()

    @classmethod
    def root(cls, ctx: Context) -> 'Fiber':
        fiber = object.__new__(cls)
        fiber.parent = ctx
        fiber.plugin = None
        fiber.name = 'root'
        fiber.config = None
        fiber.state = State.ACTIVE
        fiber.effects = []
        fiber.error = None
        fiber.inject = {}
        fiber.ctx = ctx
        fiber._loading = None
        fiber._handle = None
        return fiber

    # ---------------------------------------------------------------- 依赖
    def _check(self):
        """依赖全部就绪 → 加载；依赖消失 → 卸载。（这就是"自动编排"。）"""
        for name in self.inject:
            impl = self.ctx.root.services.get(name)
            if impl is None or impl['fiber'].state != State.ACTIVE:
                if self.state == State.ACTIVE:
                    self._dispose(keep_fiber=True)
                return
            check = impl.get('check')
            if check is not None and not check():
                if self.state == State.ACTIVE:
                    self._dispose(keep_fiber=True)
                return
        if self.state == State.PENDING:
            self._start()

    # ---------------------------------------------------------------- 加载
    def _start(self):
        self.state = State.PENDING
        self._loading = asyncio.ensure_future(self._load())

    async def _load(self):
        self.state = State.LOADING
        try:
            callback = self.callback
            result = callback(self.ctx, self.config) if _accepts_config(callback) else callback(self.ctx)
            if asyncio.iscoroutine(result):
                await result
            self.state = State.ACTIVE
            # 自己提供的服务现在才"可用"（提供方 fiber 必须是 ACTIVE）——
            # 通知依赖方来敲门
            for impl in list(self.ctx.root.services.values()):
                if impl['fiber'] is self:
                    notify(self.ctx.root, impl['name'])
        except Exception as error:
            self.error = error
            self.state = State.FAILED
            raise

    def __await__(self):
        return self._wait().__await__()

    async def _wait(self):
        while self._loading is not None and not self._loading.done():
            await self._loading
        if self.error is not None:
            raise self.error
        return self

    # ---------------------------------------------------------------- 卸载
    def _push_effect(self, label, disposers) -> Effect:
        if self.state in (State.UNLOADING, State.DISPOSED):
            raise RuntimeError('fiber 已卸载，无法注册 effect')
        effect = Effect(label, disposers)
        self.effects.append(effect)
        return effect

    def _dispose(self, keep_fiber=False):
        if self.state in (State.DISPOSED, State.UNLOADING):
            return self._disposing
        self.state = State.UNLOADING
        pending = []
        for effect in reversed(self.effects):
            result = effect.release()
            if result is not None:
                pending.append(result)
        self.effects.clear()

        async def finish():
            if pending:
                await asyncio.gather(*pending)
            self.state = State.PENDING if keep_fiber else State.DISPOSED
            if keep_fiber:
                self._check()          # 依赖回来后自动重新加载

        self._disposing = asyncio.ensure_future(finish())
        return self._disposing

    def dispose(self):
        return self._dispose()

    def __repr__(self):
        return f'<Fiber {self.name} {self.state}>'


class Service:
    """服务基类：``super().__init__(ctx, name)`` 即完成注册。"""

    inject = None

    def __init__(self, ctx: Context, name: str):
        self.ctx = ctx
        self.name = name
        ctx.provide(name, self)

    def __repr__(self):
        return f'<{type(self).__name__} {self.name}>'


def resolve_inject(inject) -> dict:
    if not inject:
        return {}
    if isinstance(inject, dict):
        return dict(inject)
    return {name: None for name in inject}


def notify(root: Context, name: str):
    """服务表变化后，重新评估所有声明了该依赖的 fiber。"""
    for fiber in list(root.fibers):
        if name in fiber.inject:
            fiber._check()


def _accepts_config(callback) -> bool:
    try:
        params = [p for p in inspect.signature(callback).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        return True
    return len(params) >= 2


# ---------------------------------------------------------------------- 演示
class StorageA(Service):
    def __init__(self, ctx):
        super().__init__(ctx, 'storage')
        self.data = {}

    def put(self, key, value):
        self.data[key] = value
        return f'A 已写入 {key}'

    def get(self, key):
        return self.data.get(key)


class StorageB(Service):
    def __init__(self, ctx):
        super().__init__(ctx, 'storage')
        self.data = {}

    def put(self, key, value):
        self.data[key] = f'B:{value}'
        return f'B 已写入 {key}'

    def get(self, key):
        return self.data.get(key)


async def main():
    ctx = Context()

    print('--- 1) 消费方先启动：依赖未就绪 → 保持 PENDING ---')

    def consumer(ctx):
        print('  consumer 加载，storage =', ctx.storage)
        print('  ', ctx.storage.put('answer', 42))
        ctx.cleanup(lambda: print('  consumer 卸载'))

    consumer.inject = ['storage']
    fiber = ctx.plugin(consumer)
    await fiber
    print('  consumer 状态 =', fiber.state)

    print('--- 2) 提供方出现 → 消费方自动激活 ---')
    provider = ctx.plugin(StorageA)
    await provider
    await fiber
    print('  consumer 状态 =', fiber.state)

    print('--- 3) 替换提供方 → 消费方自动卸载并重载 ---')
    await provider.dispose()
    await fiber
    print('  替换期间 consumer 状态 =', fiber.state)
    await ctx.plugin(StorageB)
    await fiber
    print('  consumer 状态 =', fiber.state)

    print('--- 4) 可选依赖：ctx.get() 探测，不需要 inject ---')
    print('  ctx.get("storage") ->', ctx.get('storage'))
    print('  ctx.get("nope")    ->', ctx.get('nope'))

    print('--- 5) 访问未就绪的依赖会给出明确错误 ---')
    lonely = ctx.plugin({'inject': ['missing'], 'apply': lambda ctx: None, 'name': 'lonely'})
    await lonely
    try:
        lonely.ctx.missing
    except RuntimeError as error:
        print('  ', error)


if __name__ == '__main__':
    asyncio.run(main())
