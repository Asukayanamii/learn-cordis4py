"""框架内部工具：符号、可释放对象列表与错误类型。

这里刻意保持"零依赖"，因为它们是整个框架的地基：

- :class:`Symbol` 用于内部键（等价于 TypeScript 版的 ``symbol``），
  避免用户可见的属性名与框架内部状态互相污染。
- :class:`DisposableList` 是本框架"可逆副作用"的数据结构基础：
  按注册顺序保存清理函数，并支持 O(1) 按值删除、逆序取出。
- :class:`CordisError` / :class:`ValidationError` 提供稳定的错误码与
  聚合式的配置校验报错。
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from typing import Any, AsyncIterator, Callable, Generic, Iterable, Iterator, TypeVar

T = TypeVar('T')

__all__ = [
    'Symbol', 'symbols', 'DisposableList', 'CordisError', 'ValidationError',
    'is_awaitable', 'is_object', 'is_disposable', 'hyphenate',
    'get_running_loop', 'spawn', 'settle', 'current_caller',
    'accepts_config', 'call_plugin',
]

#: 当前"调用方上下文"。服务方法通过 ``ctx.service`` 访问时，
#: 框架会把它设为调用方上下文，使方法内的 ``self.ctx`` 指向调用方
#: （对应 TS 版的可追踪代理），从而让服务内部的注册绑定到调用方 fiber。
current_caller: contextvars.ContextVar[Any] = contextvars.ContextVar('cordis.caller', default=None)


class Symbol:
    """轻量级符号：以对象身份作为字典键，保证不会被字符串属性名命中。"""

    __slots__ = ('name',)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'Symbol({self.name!r})'

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f'[{self.name}]'


class symbols:
    """框架内部使用的符号集合（等价于 dsh 的 ``symbols`` 命名空间）。"""

    # 上下文相关
    shadow = Symbol('cordis.shadow')
    receiver = Symbol('cordis.receiver')
    effect = Symbol('cordis.effect')
    filter = Symbol('cordis.filter')
    isolate = Symbol('cordis.isolate')
    intercept = Symbol('cordis.intercept')
    # 服务相关
    init = Symbol('cordis.init')
    check = Symbol('cordis.check')
    config = Symbol('cordis.config')
    invoke = Symbol('cordis.invoke')
    tracker = Symbol('cordis.tracker')
    resolveConfig = Symbol('cordis.resolveConfig')
    # 插件/纤维
    entry = Symbol('cordis.entry')
    plugin = Symbol('cordis.plugin')


class CallerBound:
    """让 ``self.ctx`` 优先指向**调用方上下文**的基类。

    对应 TS 版可追踪代理（traceable proxy）的核心效果：当插件 A 执行
    ``ctx.service.method()`` 时，``method`` 内的 ``self.ctx`` 就是 A 的上下文，
    因此服务内部的注册（监听器、工具、计时器）会挂到 A 的 fiber 上，
    随 A 卸载而自动撤销。

    不在服务方法调用期间时（构造期、后台任务），``self.ctx`` 回落到
    服务自身的上下文。
    """

    _cordis_service = True

    @property
    def ctx(self) -> Any:
        caller = current_caller.get()
        if caller is not None:
            return caller
        return self._service_ctx


class DisposableList(Generic[T]):
    """有序的清理函数集合：按加入顺序迭代，``clear()`` 逆序返回。

    框架里所有"注册"（监听器、服务、子插件、effect 包装器）都会进入某个
    :class:`DisposableList`，纤维卸载时统一逆序释放，这正是
    "所有注册都是可逆副作用" 的落点。
    """

    __slots__ = ('_sn', '_map', '_index')

    def __init__(self) -> None:
        self._sn = 0
        self._map: dict[int, T] = {}
        self._index: dict[int, int] = {}

    def __len__(self) -> int:
        return len(self._map)

    def __iter__(self) -> Iterator[T]:
        return iter(list(self._map.values()))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'DisposableList({list(self._map.values())!r})'

    def push(self, value: T) -> Callable[[], bool]:
        """加入一个值，返回“按值删除”的句柄。"""
        self._sn += 1
        sn = self._sn
        self._map[sn] = value
        self._index[id(value)] = sn
        return lambda: self._map.pop(sn, None) is not None

    def delete(self, value: T) -> bool:
        """按值删除（O(1)），用于所有权转移。"""
        sn = self._index.pop(id(value), None)
        if sn is None:
            return False
        return self._map.pop(sn, None) is not None

    def clear(self) -> list[T]:
        """清空并**逆序**返回所有值（先注册的最后释放）。"""
        values = list(self._map.values())
        self._map.clear()
        self._index.clear()
        values.reverse()
        return values


class CordisError(Exception):
    """带稳定错误码的框架异常。"""

    class Code:
        INACTIVE_EFFECT = 'cannot create effect on inactive context'
        SERVICE_INACTIVE = 'cannot get required service in inactive context'
        SERVICE_NOT_PROVIDED = 'cannot set property without provide'
        SERVICE_DUPLICATE = 'service has been registered at another fiber'
        PROPERTY_DECLARED = 'property is already declared'
        INVALID_PLUGIN = 'invalid plugin, expect function, class, or object with an "apply" method'
        CIRCULAR = 'circular dependency detected'

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class ValidationError(TypeError):
    """插件配置未通过 schema 校验时抛出的错误。"""

    name = 'ValidationError'

    def __init__(self, issues: Iterable[Any]) -> None:
        lines: list[str] = []
        for issue in issues:
            if getattr(issue, 'path', None):
                lines.append(f'  - {issue.message} (at {".".join(map(str, issue.path))})')
            else:
                lines.append(f'  - {issue.message}')
        self.issues = list(issues)
        super().__init__('invalid config:\n' + '\n'.join(lines))


def is_awaitable(value: Any) -> bool:
    return inspect.isawaitable(value)


def is_object(value: Any) -> bool:
    """等价于 TS 版的 ``isObject``：非空的 dict/list/任意对象/函数。"""
    return value is not None and not isinstance(value, (bool, int, float, str, bytes))


def is_disposable(value: Any) -> bool:
    return callable(value)


def accepts_config(callback: Any) -> bool:
    """判断插件回调是否接受 ``(ctx, config)`` 两个参数。

    TS 版的 ``apply(ctx, config)`` 多传一个参数没有代价；Python 不行，
    因此这里检查签名：``def apply(ctx)`` 只传 ctx，``def apply(ctx, config)``
    才传配置。
    """
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return True
    count = 0
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
            count += 1
        elif parameter.kind is parameter.VAR_POSITIONAL:
            return True
    return count >= 2


def call_plugin(callback: Any, ctx: Any, config: Any) -> Any:
    """按签名调用插件主体（函数或类构造）。"""
    if accepts_config(callback):
        return callback(ctx, config)
    return callback(ctx)


def hyphenate(name: str) -> str:
    """把 ``MyService`` 这类驼峰名字转成 ``my-service``，用于日志名。"""
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index > 0:
            out.append('-')
        out.append(char.lower())
    return ''.join(out)


async def maybe_await(value: Any) -> Any:
    if is_awaitable(value):
        return await value
    return value


def get_running_loop() -> asyncio.AbstractEventLoop | None:
    """返回当前运行中的事件循环；不在循环内时返回 ``None``。"""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def spawn(coro: Any) -> Any:
    """在当前事件循环中并发运行协程。

    这是 Python 版对 TS 版 "一切都返回 Promise" 的适配：
    在 ``asyncio`` 环境中返回可等待的 ``Task``；在没有运行中循环的
    纯同步环境中，用一个临时循环把它驱动到完成并返回 ``None``。
    这让 ``ctx.plugin(...)`` 在同步脚本里也能直接使用。
    """
    loop = get_running_loop()
    if loop is not None:
        return loop.create_task(coro)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    return None


async def settle(task: Any) -> None:
    """等待一个可能为 ``None`` 的任务，忽略其异常（异常由持有者记录）。"""
    if task is None:
        return
    try:
        await task
    except Exception:
        pass


async def collect_async_iterable(iterable: AsyncIterator[T]) -> list[T]:
    items: list[T] = []
    async for item in iterable:
        items.append(item)
    return items
