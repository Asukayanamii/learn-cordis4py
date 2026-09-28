"""Fiber（纤维）：一个**已加载插件实例**的运行时句柄。

dsh 的 Cordis 用 fiber 承载一个插件的全部生命周期：

    PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED
                   ↘ FAILED

- **PENDING**：已声明，但 ``inject`` 声明的服务尚未全部就绪；
- **LOADING / ACTIVE**：插件函数正在执行 / 已执行完成；
- **FAILED**：插件函数或配置校验抛出了异常；
- **UNLOADING / DISPOSED**：清理函数正在运行 / 一切已拆除。

Fiber 同时是"可逆副作用"的账本：``ctx.effect()``、``ctx.on()``、
``ctx.provide()``、``ctx.plugin()`` 注册的东西都会挂到当前 fiber 上，
卸载时按逆序释放，因此插件永远不需要手写 ``remove_listener`` /
``cancel_timer``。
"""

from __future__ import annotations

import asyncio
from enum import IntEnum
from typing import Any, Awaitable, Callable, Iterator, Optional

from .schema import validate_config
from .utils import (
    CordisError, DisposableList, call_plugin, get_running_loop, is_awaitable, spawn,
)

__all__ = [
    'Fiber', 'FiberState', 'EffectMeta', 'Disposer', 'PluginRuntime',
    'collect_effect', 'resolve_config',
]


class FiberState(IntEnum):
    PENDING = 0
    LOADING = 1
    ACTIVE = 2
    FAILED = 3
    DISPOSED = 4
    UNLOADING = 5


INACTIVE = '__INACTIVE__'


class EffectMeta:
    """effect 的诊断元数据（``fiber.get_effects()`` 返回的就是它）。"""

    __slots__ = ('label', 'children')

    def __init__(self, label: str) -> None:
        self.label = label
        self.children: list['EffectMeta'] = []

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'EffectMeta({self.label!r}, children={len(self.children)})'


def _collect_single(value: Any, collect: Callable[[Callable[[], Any]], None]) -> None:
    if value is None:
        return
    if callable(value):
        collect(value)
        return
    raise TypeError('Invalid effect: expected a disposer, None, or an iterable of them')


def _is_effect_iterable(value: Any) -> bool:
    if isinstance(value, (str, bytes, dict)):
        return False
    return hasattr(value, '__iter__') or hasattr(value, '__aiter__')


def collect_effect(effect: Any, collect: Callable[[Callable[[], Any]], None]) -> Any:
    """解释一次 effect 主体（或插件主体）的返回值。

    与 TS 版 ``_execute`` 一致，接受以下形态：

    - ``None``：没有需要清理的东西；
    - 可调用对象：一个 disposer；
    - 上下文管理器（``contextlib.contextmanager`` 等）：立即 ``__enter__``，
      并把 ``__exit__`` 登记为 disposer（Python 风格的 setup/teardown）；
    - 可等待对象：等待后得到一个 disposer；
    - 可迭代 / 异步可迭代对象：逐个产生 disposer（生成器 effect）。

    返回 ``None`` 表示已经完成；否则返回一个"待等待的后续协程"。
    """
    if effect is None:
        return None
    # 上下文管理器（contextlib.contextmanager 等）本身可能也是可调用对象，
    # 因此必须在 callable 之前判断。
    if hasattr(effect, '__enter__') and hasattr(effect, '__exit__'):
        effect.__enter__()
        collect(lambda: effect.__exit__(None, None, None))
        return None
    if hasattr(effect, '__aenter__') and hasattr(effect, '__aexit__'):
        async def aenter() -> None:
            await effect.__aenter__()
            collect(lambda: effect.__aexit__(None, None, None))
        return aenter()
    if callable(effect):
        collect(effect)
        return None
    if is_awaitable(effect):
        async def waiter() -> None:
            value = await effect
            _collect_single(value, collect)
        return waiter()
    if _is_effect_iterable(effect):
        if hasattr(effect, '__aiter__'):
            async def async_iter() -> None:
                async for item in effect:
                    _collect_single(item, collect)
            return async_iter()

        def sync_iter() -> None:
            for item in effect:
                _collect_single(item, collect)
        sync_iter()
        return None
    raise TypeError('Invalid effect: expected a disposer, None, or an iterable of them')


async def _run_disposable(item: Callable[[], Any], logger: Any) -> None:
    """调用一个 disposer 并等待其异步部分；异常记录日志而不打断清理。"""
    try:
        result = item()
    except Exception as error:
        logger.error(error)
        return
    if is_awaitable(result):
        try:
            await result
        except Exception as error:
            logger.error(error)


class Disposer:
    """一次 effect 的撤销句柄。

    - ``disposer()`` 触发释放，返回可等待对象（纯同步时返回 ``None``）；
    - ``await disposer`` 触发并等待释放完成；
    - 重复调用是幂等的。

    释放规则：清理函数按注册顺序的**逆序启动**（同步部分立即执行），
    返回 awaitable 的会被**并发等待**。这与 dsh 的 disposable 语义一致：
    "调用返回的清理函数或卸载 fiber，先到先得"。
    """

    __slots__ = ('_fiber', '_disposables', 'meta', '_started', '_task')

    def __init__(self, fiber: Optional['Fiber'], meta: EffectMeta) -> None:
        self._fiber = fiber
        self._disposables: list[Callable[[], Any]] = []
        self.meta = meta
        self._started = False
        self._task: Any = None

    def __call__(self) -> Any:
        return self._start()

    def __await__(self) -> Iterator[Any]:
        task = self._start()
        if task is not None:
            yield from task.__await__()

    def _start(self) -> Any:
        if self._started:
            return self._task
        self._started = True
        logger = self._fiber.ctx.logger if self._fiber is not None else None
        pending: list[Any] = []
        for disposable in reversed(self._disposables):
            try:
                result = disposable()
            except Exception as error:
                if logger is not None:
                    logger.error(error)
                continue
            if is_awaitable(result):
                pending.append(result)
        self._disposables.clear()
        if self._fiber is not None:
            self._fiber._disposables.delete(self)
        if pending:
            self._task = spawn(_await_all(pending, logger))
        return self._task

    async def join(self) -> None:
        """确保已触发释放，并等待完成。"""
        task = self._start()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


async def _await_all(awaitables: list[Any], logger: Any) -> None:
    results = await asyncio.gather(*awaitables, return_exceptions=True)
    if logger is None:
        return
    for result in results:
        if isinstance(result, BaseException):
            logger.error(result)


def resolve_config(runtime: Any, config: Any) -> Any:
    """用插件声明的 ``Config`` schema 校验并归一化原始配置。"""
    schema = getattr(runtime, 'Config', None) if runtime is not None else None
    if schema is None:
        return config
    return validate_config(schema, config)


class PluginRuntime:
    """同一个插件回调（callback）被多次 ``ctx.plugin()`` 时共享的运行时记录。"""

    __slots__ = ('name', 'callback', 'fibers', 'Config')

    def __init__(self, name: str | None, callback: Any, schema: Any = None) -> None:
        self.name = name
        self.callback = callback
        self.fibers = DisposableList['Fiber']()
        self.Config = schema


class Fiber:
    """单次插件应用的运行时实例。"""

    def __init__(
        self,
        parent: Any,
        config: Any,
        inject: dict[str, Any],
        runtime: Optional[PluginRuntime] = None,
    ) -> None:
        self.parent = parent
        self.inject = inject
        self.runtime = runtime
        self.config = None
        self._config = config
        self.state = FiberState.PENDING
        self._error: Any = None
        self.inertia: Any = None
        self.store: dict[str, Any] | None = None
        self._store: dict[str, Any] = {}
        self._disposables = DisposableList[Any]()
        self._hooks: dict[str, list[Any]] = {}

        if runtime is not None:
            self.uid: int | None = parent.registry.counter
            self.ctx = parent.extend({'fiber': self})
            self._epoch: Any = INACTIVE
            self._setup_plugin()
        else:
            # 根 fiber：uid 为 0，永远处于 ACTIVE。
            self.uid = 0
            self.ctx = parent
            self.state = FiberState.ACTIVE
            self.store = {}
            self._epoch = ''

    # ================================================================ 基础属性
    @property
    def name(self) -> str:
        """插件显示名：取最近的具名祖先，否则为 ``root``。"""
        fiber: Fiber = self
        while True:
            if fiber.runtime is not None and fiber.runtime.name:
                return fiber.runtime.name
            parent = fiber.parent.fiber
            if fiber is parent:
                break
            fiber = parent
        return 'root'

    def assert_active(self) -> None:
        if self.uid is not None:
            return
        raise CordisError('INACTIVE_EFFECT')

    def __await__(self) -> Iterator[Any]:
        yield from self.await_().__await__()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'<Fiber {self.name} state={self.state.name}>'

    # ================================================================ 插件装载
    def _setup_plugin(self) -> None:
        runtime = self.runtime

        def install() -> Callable[[], Any]:
            remove = runtime.fibers.push(self)

            def uninstall() -> Any:
                # 同步前缀立即生效（uid 清空、从 registry 摘除、触发卸载），
                # 异步尾部等待卸载真正跑完。
                self.uid = None
                self._emit_plugin_disposed()
                if self.ctx.registry.has(runtime.callback):
                    remove()
                    if not len(runtime.fibers):
                        self.ctx.registry.delete(runtime.callback)
                self._set_epoch(INACTIVE)
                if self.inertia is None:
                    self.inertia = spawn(self._unload())

                async def wait_unload() -> None:
                    while self.inertia is not None:
                        await self.inertia

                if self.inertia is not None:
                    return wait_unload()
                return None

            return uninstall

        self._dispose_handle = self.parent.fiber.effect(install, 'ctx.plugin()')

        try:
            self.ctx.events.emit('internal/plugin', self)
        except Exception:
            # 发布失败：先把子插件从父子两边摘除，再让异常向上传播。
            self._dispose_handle()
            raise

        if self.uid is not None and self.parent.fiber.state != FiberState.UNLOADING:
            for name in list(self.inject):
                self._check_impl(name)
            self._refresh()

    def _emit_plugin_disposed(self) -> None:
        try:
            self.ctx.events.emit('internal/plugin', self)
        except Exception as error:  # pragma: no cover - 观察者异常不应破坏清理
            self.ctx.logger.error(error)

    # ================================================================ effect
    def effect(self, execute: Callable[[], Any], label: str = 'anonymous') -> Disposer:
        """在当前 fiber 上注册一个支持清理的 effect。

        ``execute`` 立即执行；它产生的 disposer 会被收集，并在
        "返回的 Disposer 被调用" 或 "fiber 卸载" 时按**逆序**释放。
        """
        self.assert_active()
        if self.state == FiberState.UNLOADING:
            raise CordisError('INACTIVE_EFFECT')

        meta = EffectMeta(label)
        disposer = Disposer(self, meta)

        def collect(item: Callable[[], Any]) -> None:
            # 所有权转移：如果收集到的是另一个 fiber 级 effect 的句柄，
            # 把它从属主列表中摘除，避免重复释放（对应 TS 版的 dispose 转移）。
            if isinstance(item, Disposer):
                self._disposables.delete(item)
                meta.children.append(item.meta)
            disposer._disposables.append(item)

        # 先登记再执行：这样 execute 内部发生重入卸载时清理也能被发现。
        self._disposables.push(disposer)
        try:
            result = collect_effect(execute(), collect)
        except Exception:
            disposer()
            raise
        if result is not None:
            self._spawn_effect_body(result)
        return disposer

    def _spawn_effect_body(self, result: Any) -> None:
        """异步 effect 主体：后台推进，失败时回滚并记录日志。"""
        if get_running_loop() is None:
            spawn(result)
            return

        async def runner() -> None:
            try:
                await result
            except Exception as error:
                self.ctx.logger.error(error)
                raise

        task = asyncio.ensure_future(runner())

        def on_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                try:
                    task.result()
                except Exception:
                    pass

        task.add_done_callback(on_done)

    def get_effects(self) -> list[EffectMeta]:
        """当前仍然存活、带标签的 effect 元数据。"""
        return [disposer.meta for disposer in self._disposables]

    def cleanup(self, disposer: Callable[[], Any], label: str = 'ctx.cleanup()') -> Disposer:
        """登记一个"卸载时执行"的清理函数（不会立即执行）。

        等价于 ``ctx.effect(lambda: disposer)``，但意图更直白。
        """
        return self.effect(lambda: disposer, label)

    # ================================================================ 状态机
    def _get_state(self) -> FiberState:
        if self.uid is None:
            return FiberState.DISPOSED
        if self._error is not None:
            return FiberState.FAILED
        if self._epoch != INACTIVE:
            return FiberState.ACTIVE
        return FiberState.PENDING

    def _update_state(self, callback: Callable[[], Any]) -> None:
        old_state = self.state
        new_state = callback()
        self.state = new_state if new_state is not None else self._get_state()
        if old_state == self.state:
            return
        self.ctx.events.emit('internal/status', self, old_state)
        if old_state != FiberState.ACTIVE and self.state != FiberState.ACTIVE:
            return
        reflect = self.ctx.reflect
        for impl in list(reflect.store.values()):
            if impl.fiber is not self:
                continue
            reflect.notify([impl.name], self.ctx)

    def _check_impl(self, name: str) -> None:
        impl = self.ctx.reflect._get_impl(self.ctx, name, strict=True)
        if impl is None:
            self._store.pop(name, None)
            return
        if impl.check is not None:
            try:
                if not impl.check():
                    self._store.pop(name, None)
                    return
            except Exception as error:
                impl.fiber.ctx.logger.error(error)
                self._store.pop(name, None)
                return
        self._store[name] = impl

    def _refresh(self) -> None:
        epoch: Any = ''
        for name in self.inject:
            impl = self._store.get(name)
            if impl is None:
                epoch = INACTIVE
                break
            epoch += ':' + str(impl.fiber.uid)
        self._set_epoch(epoch)

    def _set_epoch(self, epoch: Any) -> None:
        old_epoch = self._epoch
        if epoch == old_epoch:
            return
        self._epoch = epoch
        if self.inertia is not None:
            return

        def transition() -> FiberState | None:
            if epoch != INACTIVE and old_epoch == INACTIVE:
                self.inertia = spawn(self._reload())
                return FiberState.LOADING
            self.inertia = spawn(self._unload())
            return FiberState.UNLOADING

        self._update_state(transition)

    # ================================================================ 加载 / 卸载
    def _resolve_config(self, config: Any) -> Any:
        config = self.ctx.waterfall('internal/config', self, config, lambda *_: config)
        if self.runtime is not None:
            return resolve_config(self.runtime, config)
        return config

    def _plugin_result(self) -> Any:
        """执行插件主体（函数 / 类），返回供 effect 解释的结果。"""
        runtime = self.runtime
        callback = runtime.callback
        if isinstance(callback, type):
            instance = call_plugin(callback, self.ctx, self.config)
            init = getattr(instance, 'init', None)
            if callable(init):
                return init()
            return None
        return call_plugin(callback, self.ctx, self.config)

    async def _reload(self) -> None:
        self.store = dict(self._store)
        old_epoch = self._epoch
        try:
            await asyncio.sleep(0)
            # 在这个检查点之前排队的 disposer 可能已经让本次加载失效
            if self._epoch == old_epoch:
                self.config = self._resolve_config(self._config)
                result = collect_effect(self._plugin_result(), self._collect_plugin_disposer)
                if result is not None:
                    await result
                self._error = None
        except Exception as reason:
            self.ctx.logger.error(reason)
            self._error = reason
            self._epoch = INACTIVE
        self._update_state(lambda: self._finish_transition(old_epoch))

    def _collect_plugin_disposer(self, dispose: Callable[[], Any]) -> None:
        self._disposables.push(dispose)

    def _finish_transition(self, old_epoch: Any) -> FiberState | None:
        if self._epoch == old_epoch:
            self.inertia = None
            return None
        self.inertia = spawn(self._unload())
        return FiberState.UNLOADING

    async def _unload(self) -> None:
        disposers = self._disposables.clear()
        if disposers:
            await asyncio.gather(*[
                _run_disposable(disposer, self.ctx.logger) for disposer in disposers
            ])
        self.store = None
        self._update_state(lambda: self._finish_unload())

    def _finish_unload(self) -> FiberState | None:
        if self._epoch == INACTIVE:
            self.inertia = None
            return None
        self.inertia = spawn(self._reload())
        return FiberState.LOADING

    # ================================================================ 对外 API
    async def await_(self) -> 'Fiber':
        """等待当前生命周期工作完成，并重新抛出启动错误。"""
        while self.inertia is not None:
            await self.inertia
        if self._error is not None:
            raise self._error
        return self

    def dispose(self) -> Any:
        """卸载插件；返回可等待对象（``await fiber.dispose()`` 等待清理完成）。"""
        handle = getattr(self, '_dispose_handle', None)
        if handle is None:
            # 根 fiber：dispose 等价于 restart
            return spawn(self.restart())
        return handle()

    def restart(self) -> Awaitable[Any]:
        """卸载并用当前配置立即重新加载。"""
        self.assert_active()
        self._set_epoch(INACTIVE)
        self._refresh()
        return self.await_()

    def update(self, config: Any, no_save: bool = False) -> None:
        """校验并应用新配置，然后重启插件。

        先经过 ``internal/update`` waterfall，更新钩子（例如装载器持久化、
        热重载）可以否决或替换这次重启。
        """
        self.assert_active()
        self._config = config
        if self.state != FiberState.ACTIVE:
            self._error = None
            self._set_epoch(INACTIVE)
            self._refresh()
            return
        resolved = self._resolve_config(config)

        def default_next(*_args: Any) -> Any:
            self.config = resolved
            self._error = None
            # 立即驱动重启：没有事件循环时同步跑完，有循环时作为任务推进；
            # 返回 awaitable 供需要的监听器等待（对应 dsh 中 restart 返回 Promise）。
            return spawn(self.restart())

        self.ctx.waterfall('internal/update', self, resolved, no_save, default_next)
