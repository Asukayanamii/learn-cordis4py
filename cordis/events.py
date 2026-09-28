"""事件总线：``ctx.on`` / ``ctx.emit`` 等。

Cordis 的事件系统是整个框架的"横向通信层"：监听者不需要知道谁在发事件，
服务也不需要知道谁在监听。共有 5 种分发模式，事件采用哪种模式是它公开约定
的一部分：

===========  ==========================  ==========================================
模式         调用                        语义
===========  ==========================  ==========================================
emit         ``ctx.emit(name, ...)``     同步广播；不等待、不收集返回值
parallel     ``await ctx.parallel(...)`` 所有监听器并发运行并一起等待
serial       ``await ctx.serial(...)``   按注册顺序依次等待；首个非假返回值胜出
bail         ``ctx.bail(name, ...)``     serial 的同步版本
waterfall    ``ctx.waterfall(name, ...)`` 环绕中间件：监听器收到 ``next`` 续延
===========  ==========================  ==========================================

waterfall 是拦截与策略的实现方式：只观察的监听器必须调用 ``next()``；
不调用就直接返回代表**有意短路**（否决链条其余部分，包括最内层默认行为）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Iterable, Optional

from .utils import CallerBound, maybe_await

__all__ = ['EventsService', 'Hook', 'is_bailed']


def is_bailed(value: Any) -> bool:
    """返回值是否应该中止 bail/serial 分发。"""
    return value is not None and value is not False


class Hook:
    """一条已注册的监听器记录。"""

    __slots__ = ('ctx', 'callback', 'prepend', 'global_')

    def __init__(self, ctx: Any, callback: Callable[..., Any], prepend: bool = False, global_: bool = False) -> None:
        self.ctx = ctx
        self.callback = callback
        self.prepend = prepend
        self.global_ = global_

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'<Hook {self.callback!r}>'


class EventsService(CallerBound):
    """事件总线。它的方法也会以 ``ctx.on`` / ``ctx.emit`` ... 的形式暴露。"""

    def __init__(self, ctx: Any) -> None:
        self._service_ctx = ctx
        self._hooks: dict[Any, list[Hook]] = {}

        # 框架内建事件：
        # - internal/listener：注册监听器时的拦截点（可替换默认注册行为）
        # - internal/update：fiber 配置更新时的 waterfall（装载器以此持久化配置）
        self.on('internal/listener', self._handle_internal_listener, ctx=self.ctx)
        self.on('internal/update', self._handle_internal_update, prepend=True, global_=True, ctx=self.ctx)

    # ================================================================ 分发
    def dispatch(
        self,
        mode: str,
        name: Any,
        args: list[Any],
        this_ctx: Any = None,
        filter_fn: Callable[[Any], bool] | None = None,
    ) -> list[Callable[..., Any]]:
        """解析出本次分发的监听器，并应用上下文过滤。"""
        if not str(name).startswith('internal/'):
            self._hooks_dispatch_internal(mode, name, args, this_ctx)
        hooks = self._hooks.get(name)
        if not hooks:
            return []
        if filter_fn is None and this_ctx is not None:
            filter_fn = getattr(this_ctx, '_filter', None)
        result: list[Callable[..., Any]] = []
        for hook in list(hooks):
            if not hook.global_ and filter_fn is not None and not filter_fn(hook.ctx):
                continue
            result.append(hook.callback)
        return result

    def _hooks_dispatch_internal(self, mode: str, name: Any, args: list[Any], this_ctx: Any) -> None:
        for callback in self.dispatch('emit', 'internal/dispatch', [mode, name, args, this_ctx]):
            callback(mode, name, args, this_ctx)

    def emit(self, name: Any, *args: Any) -> None:
        """同步广播：按注册顺序调用监听器，忽略返回值与 promise。"""
        callbacks = self.dispatch('emit', name, list(args))
        for callback in callbacks:
            callback(*args)

    def emit_filtered(self, name: Any, filter_fn: Callable[[Any], bool], *args: Any) -> None:
        """带上下文过滤的广播（框架内部用于 ``internal/service`` 的隔离通知）。"""
        for callback in self.dispatch('emit', name, list(args), filter_fn=filter_fn):
            callback(*args)

    async def parallel(self, name: Any, *args: Any) -> None:
        """并发运行所有监听器，并一起等待。"""
        callbacks = self.dispatch('parallel', name, list(args))
        if not callbacks:
            return
        import asyncio
        results = await asyncio.gather(
            *[maybe_await(callback(*args)) for callback in callbacks],
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise ExceptionGroup('parallel event errors', errors)

    async def serial(self, name: Any, *args: Any) -> Any:
        """按顺序 await 监听器，直到某个监听器返回 bail 值。"""
        for callback in self.dispatch('serial', name, list(args)):
            result = await maybe_await(callback(*args))
            if is_bailed(result):
                return result
        return None

    def bail(self, name: Any, *args: Any) -> Any:
        """同步版的 serial。"""
        for callback in self.dispatch('bail', name, list(args)):
            result = callback(*args)
            if is_bailed(result):
                return result
        return None

    def waterfall(self, name: Any, *args: Any) -> Any:
        """环绕中间件式分发：最后一个参数是最内层的 ``next``。

        监听器按注册顺序从外到内包裹；不调用 ``next()`` 直接返回即短路。
        """
        args = list(args)
        if not args:
            raise TypeError('waterfall requires an innermost next callback')
        inner = args.pop()
        if not callable(inner):
            raise TypeError('waterfall requires the last argument to be a callable next')
        callbacks = self.dispatch('waterfall', name, list(args))

        def next_(*_ignored: Any) -> Any:
            callback = callbacks.pop(0) if callbacks else inner
            return callback(*args, next_)

        return next_()

    # ================================================================ 注册
    def on(
        self,
        name: Any,
        listener: Callable[..., Any],
        prepend: bool = False,
        global_: bool = False,
        ctx: Any = None,
    ) -> Any:
        """注册监听器（随 ``ctx.fiber`` 卸载自动移除），返回撤销句柄。"""
        caller = ctx if ctx is not None else self.ctx
        caller.fiber.assert_active()
        result = self.bail('internal/listener', caller, name, listener, prepend, global_)
        if result:
            return result
        return self.register(f'ctx.on({name!r})', self._hooks.setdefault(name, []), listener, caller, prepend, global_)

    def once(
        self,
        name: Any,
        listener: Callable[..., Any],
        prepend: bool = False,
        global_: bool = False,
        ctx: Any = None,
    ) -> Any:
        """注册一次性监听器，首次调用后自动撤销。"""
        caller = ctx if ctx is not None else self.ctx
        state: dict[str, Any] = {}

        def wrapper(*args: Any) -> Any:
            disposer = state.get('disposer')
            if disposer is not None:
                disposer()
            return listener(*args)

        state['disposer'] = self.on(name, wrapper, prepend=prepend, global_=global_, ctx=caller)
        return state['disposer']

    def register(
        self,
        label: str,
        hooks: list[Hook],
        callback: Callable[..., Any],
        ctx: Any,
        prepend: bool = False,
        global_: bool = False,
    ) -> Any:
        """把监听器作为 effect 挂到注册方的 fiber 上。"""
        def install() -> Callable[[], bool]:
            hook = Hook(ctx, callback, prepend, global_)
            if prepend:
                hooks.insert(0, hook)
            else:
                hooks.append(hook)
            return lambda: self.unregister(hooks, callback)

        return ctx.fiber.effect(install, label)

    def unregister(self, hooks: list[Hook], callback: Callable[..., Any]) -> bool:
        for index, hook in enumerate(hooks):
            if hook.callback is callback:
                del hooks[index]
                return True
        return False

    # ================================================================ 内建事件
    def _handle_internal_listener(
        self,
        caller: Any,
        name: Any,
        listener: Callable[..., Any],
        prepend: bool,
        global_: bool,
    ) -> Any:
        """把 ``internal/update`` 监听器收集到注册方 fiber 上（按 fiber 排序执行）。"""
        if name == 'internal/update' and not global_:
            hooks = caller.fiber._hooks.setdefault('internal/update', [])
            if prepend:
                hooks.insert(0, listener)
            else:
                hooks.append(listener)
            return lambda: hooks.remove(listener) if listener in hooks else False
        return None

    def _handle_internal_update(self, fiber: Any, config: Any, no_save: bool, next_: Callable[[], Any]) -> Any:
        """``internal/update`` 的外层包装：先跑该 fiber 自己的更新钩子，再委托下去。"""
        callbacks = list(getattr(fiber, '_hooks', {}).get('internal/update', []))

        def advance() -> Any:
            callback = callbacks.pop(0) if callbacks else next_
            return callback(config, no_save, advance)

        return advance()
