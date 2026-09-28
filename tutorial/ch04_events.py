"""第 4 章 · 事件：插件之间的横向通信。

服务是"直接调用"，事件是"广播通知"：发事件的人不需要知道谁在听。
本章实现五种分发模式——事件采用哪种模式，是它公开约定的一部分：

===========  =============================  ====================================
模式         调用                          语义
===========  =============================  ====================================
emit         ``ctx.emit(name, ...)``        同步广播，不等待、不收集返回值
parallel     ``await ctx.parallel(...)``    所有监听器并发运行并一起等待
serial       ``await ctx.serial(...)``      依次 await，首个非假返回值胜出
bail         ``ctx.bail(name, ...)``        serial 的同步版本
waterfall    ``ctx.waterfall(name, ...,f)`` 环绕中间件：不调用 next() 即短路
===========  =============================  ====================================

两条纪律：
1. **监听器是 effect**：``ctx.on(...)`` 注册的监听器随插件卸载自动移除；
2. **只观察的 waterfall 监听器必须调用 next()**；直接返回 = 有意的否决。

对照仓库里的完整实现：``cordis/events.py``。

运行：python tutorial/ch04_events.py
"""

from __future__ import annotations

import asyncio
import inspect


class State:
    PENDING = 'PENDING'
    LOADING = 'LOADING'
    ACTIVE = 'ACTIVE'
    UNLOADING = 'UNLOADING'
    DISPOSED = 'DISPOSED'
    FAILED = 'FAILED'


class Events:
    """事件总线：保存监听器表，并按模式分发。"""

    def __init__(self):
        self.hooks: dict[str, list[dict]] = {}

    # ---------------------------------------------------------------- 注册
    def on(self, ctx: 'Context', name, listener, prepend=False):
        fiber = ctx.fiber
        if fiber.state in (State.UNLOADING, State.DISPOSED):
            raise RuntimeError('fiber 已卸载，无法注册监听器')

        def setup():
            hook = {'ctx': ctx, 'callback': listener}
            hooks = self.hooks.setdefault(name, [])
            if prepend:
                hooks.insert(0, hook)
            else:
                hooks.append(hook)

            def remove():
                if hook in hooks:
                    hooks.remove(hook)
                    return True
                return False

            return remove

        return ctx.effect(setup, label=f'ctx.on({name!r})')

    def once(self, ctx, name, listener, prepend=False):
        holder = {}

        def wrapper(*args):
            disposer = holder.get('disposer')
            if disposer is not None:
                disposer()
            return listener(*args)

        holder['disposer'] = self.on(ctx, name, wrapper, prepend)
        return holder['disposer']

    def _listeners(self, name):
        return [hook['callback'] for hook in self.hooks.get(name, [])]

    # ---------------------------------------------------------------- 分发
    def emit(self, name, *args):
        for callback in self._listeners(name):
            callback(*args)

    async def parallel(self, name, *args):
        results = await asyncio.gather(
            *[maybe_await(callback(*args)) for callback in self._listeners(name)],
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise ExceptionGroup('parallel event errors', errors)

    async def serial(self, name, *args):
        for callback in self._listeners(name):
            result = await maybe_await(callback(*args))
            if is_bailed(result):
                return result
        return None

    def bail(self, name, *args):
        for callback in self._listeners(name):
            result = callback(*args)
            if is_bailed(result):
                return result
        return None

    def waterfall(self, name, *args):
        """最后一个参数是最内层的默认实现；监听器收到 (..., next)。"""
        args = list(args)
        inner = args.pop()
        callbacks = self._listeners(name)

        def next_(*_ignored):
            callback = callbacks.pop(0) if callbacks else inner
            return callback(*args, next_)

        return next_()


def is_bailed(value):
    """非 None / False 的返回值视为"中止链式分发"。"""
    return value is not None and value is not False


async def maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class Context:
    def __init__(self, parent: 'Context | None' = None, **meta):
        self.parent = parent
        self.meta = dict(meta)
        self.fiber: 'Fiber' = meta.get('fiber')
        if parent is None and self.fiber is None:
            self.services: dict[str, dict] = {}
            self.fibers: list['Fiber'] = []
            self.events = Events()
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
                return getattr(parent, name)
            except AttributeError:
                pass
        root = parent.root if parent is not None else self
        impl = root.services.get(name)
        if impl is not None:
            return impl['value']
        fiber = self.__dict__.get('fiber')
        if fiber is not None and name in fiber.inject:
            raise RuntimeError(f'依赖 {name!r} 尚未就绪（fiber 处于 {fiber.state}）')
        raise AttributeError(name)

    # ---------------------------------------------------------------- 事件
    def on(self, name, listener, prepend=False):
        return self.events.on(self, name, listener, prepend)

    def once(self, name, listener, prepend=False):
        return self.events.once(self, name, listener, prepend)

    def emit(self, name, *args):
        self.events.emit(name, *args)

    async def parallel(self, name, *args):
        await self.events.parallel(name, *args)

    async def serial(self, name, *args):
        return await self.events.serial(name, *args)

    def bail(self, name, *args):
        return self.events.bail(name, *args)

    def waterfall(self, name, *args):
        return self.events.waterfall(name, *args)

    # ---------------------------------------------------------------- 服务
    def provide(self, name, value, check=None):
        fiber = self.fiber
        root = self.root

        def setup():
            root.services[name] = {'name': name, 'value': value, 'fiber': fiber, 'check': check}
            if fiber.state == State.ACTIVE:
                notify(root, name)
            return lambda: (root.services.pop(name, None), notify(root, name))

        return self.effect(setup, label=f'ctx.provide({name})')

    def get(self, name, strict=True):
        impl = self.root.services.get(name)
        if impl is None:
            return None
        if strict and impl['fiber'].state != State.ACTIVE:
            return None
        return impl['value']

    def set(self, name, value):
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
        return self.plugin({'inject': deps, 'apply': callback,
                            'name': getattr(callback, '__name__', 'injected')})


class Effect:
    def __init__(self, label, disposers):
        self.label = label
        self.disposers = list(disposers)
        self.done = False

    def __call__(self):
        """像函数一样调用即释放（与真实框架的 disposer 语义一致）。"""
        return self.release()

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

    def _check(self):
        for name in self.inject:
            impl = self.ctx.root.services.get(name)
            if impl is None or impl['fiber'].state != State.ACTIVE:
                if self.state == State.ACTIVE:
                    self._dispose(keep_fiber=True)
                return
        if self.state == State.PENDING:
            self._start()

    def _start(self):
        self._loading = asyncio.ensure_future(self._load())

    async def _load(self):
        self.state = State.LOADING
        try:
            callback = self.callback
            result = callback(self.ctx, self.config) if _accepts_config(callback) else callback(self.ctx)
            if asyncio.iscoroutine(result):
                await result
            self.state = State.ACTIVE
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
                self._check()

        self._disposing = asyncio.ensure_future(finish())
        return self._disposing

    def dispose(self):
        return self._dispose()

    def __repr__(self):
        return f'<Fiber {self.name} {self.state}>'


class Service:
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
class Logger(Service):
    def __init__(self, ctx):
        super().__init__(ctx, 'log')
        self.lines = []

    def write(self, text):
        self.lines.append(text)
        print('   [log]', text)


async def main():
    ctx = Context()
    print('--- 1) emit：广播给所有监听器（忽略返回值）---')
    ctx.on('demo/emit', lambda value: print('   监听器 A 收到', value))
    ctx.on('demo/emit', lambda value: print('   监听器 B 收到', value))
    ctx.emit('demo/emit', 1)

    print('--- 2) parallel：并发等待 ---')
    async def slow():
        await asyncio.sleep(0.01)
        print('   慢监听器完成')

    ctx.on('demo/par', slow)
    ctx.on('demo/par', lambda: print('   快监听器完成'))
    await ctx.parallel('demo/par')

    print('--- 3) serial / bail：首个非假返回值胜出 ---')
    async def vote(label, answer):
        print(f'   {label} 投票: {answer}')
        return answer

    ctx.on('demo/vote', lambda: vote('A', None))
    ctx.on('demo/vote', lambda: vote('B', '通过'))
    ctx.on('demo/vote', lambda: vote('C', '不该被调用'))
    print('   结果:', await ctx.serial('demo/vote'))

    print('--- 4) waterfall：包裹与短路（策略插件的工作方式）---')
    async def wrap(decision, next_):
        return f'[{await next_()}]'

    async def policy(decision, next_):
        if decision.get('deny'):
            print('   policy 短路（不调用 next）')
            return '** 被策略拒绝 **'
        return await next_()

    async def default(decision, next_):
        return f"默认执行: {decision['text']}"

    ctx.on('demo/wf', wrap)
    ctx.on('demo/wf', policy)
    print('   放行:', await ctx.waterfall('demo/wf', {'text': 'hello'}, default))
    print('   拦截:', await ctx.waterfall('demo/wf', {'text': 'x', 'deny': True}, default))

    print('--- 5) 用事件做"观察式遥测"：监听器随插件卸载 ---')
    class ToolService(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'tools')

        def call(self, name):
            self.ctx.emit('tool/call', name)      # 服务只负责发事件
            return f'执行了 {name}'

    class Telemetry(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'telemetry')
            self.count = 0
            ctx.on('tool/call', self._on_call)     # 注册随本插件卸载自动撤销

        def _on_call(self, name):
            self.count += 1
            print(f'   [telemetry] 第 {self.count} 次工具调用: {name}')

    await ctx.plugin(ToolService)
    telemetry = ctx.plugin(Telemetry)
    await telemetry

    tools = ctx.get('tools')
    tools.call('read_file')
    tools.call('shell')

    print('   卸载 telemetry 后再调用工具（不再有统计）:')
    await telemetry.dispose()
    tools.call('write_file')

    print('--- 6) once：只触发一次 ---')
    ctx.once('demo/once', lambda: print('   只打印一次'))
    ctx.emit('demo/once')
    ctx.emit('demo/once')


if __name__ == '__main__':
    asyncio.run(main())
