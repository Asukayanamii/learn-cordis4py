"""插件注册表：``ctx.plugin`` / ``ctx.inject``。

Cordis 接受三种插件形态，它们在运行时被归一化成同一个 ``callback``：

.. code-block:: python

    def apply(ctx, config): ...            # 1. 函数插件

    class MyPlugin:                        # 2. 对象插件
        def apply(self, ctx, config): ...

    class MyService(Service):              # 3. 类插件（Service 子类）
        def __init__(self, ctx):
            super().__init__(ctx, 'myService')

``ctx.plugin(plugin, config)`` 返回一个 :class:`~cordis.fiber.Fiber`：
``await fiber`` 会等到它加载完成，加载失败时抛出异常。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from .fiber import Fiber, PluginRuntime
from .utils import CordisError

__all__ = ['RegistryService', 'Inject', 'resolve_plugin']


class Inject:
    """依赖声明的归一化工具。"""

    @staticmethod
    def resolve(inject: Any) -> dict[str, Any]:
        """把数组/映射形式的 ``inject`` 归一化为 ``{服务名: 拦截配置}``。"""
        result: dict[str, Any] = {}
        if not inject:
            return result
        if isinstance(inject, dict):
            for name, config in inject.items():
                result[name] = config
            return result
        for name in inject:
            result[name] = None
        return result


class ResolvedPlugin:
    """归一化之后的插件描述。"""

    __slots__ = ('kind', 'callback', 'identity', 'name', 'Config', 'inject', 'provide')

    def __init__(self, kind: str, callback: Any, identity: Any) -> None:
        self.kind = kind
        self.callback = callback
        self.identity = identity
        self.name = getattr(identity, 'name', None) or getattr(identity, '__name__', None)
        self.Config = getattr(identity, 'Config', None)
        self.inject = getattr(identity, 'inject', None)
        self.provide = getattr(identity, 'provide', None)


def resolve_plugin(plugin: Any) -> Optional[ResolvedPlugin]:
    """把各种插件形态归一化为可执行的 callback；无法识别时返回 ``None``。

    支持：函数 / 类 / 带 ``apply`` 方法的对象 / 带 ``apply`` 键的字典
    （字典形式与 dsh 的 ``{ name, inject, apply }`` 对象插件对应）。
    """
    if isinstance(plugin, dict):
        apply = plugin.get('apply')
        if not callable(apply):
            return None
        resolved = ResolvedPlugin('object', apply, plugin)
        resolved.name = plugin.get('name')
        resolved.Config = plugin.get('Config')
        resolved.inject = plugin.get('inject')
        resolved.provide = plugin.get('provide')
        return resolved
    if isinstance(plugin, type):
        return ResolvedPlugin('class', plugin, plugin)
    if callable(plugin):
        return ResolvedPlugin('function', plugin, plugin)
    apply = getattr(plugin, 'apply', None)
    if callable(apply):
        return ResolvedPlugin('object', apply, plugin)
    return None


class RegistryService:
    """插件注册表：归一化插件形态、追踪 runtime、启动 fiber。"""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._counter = 0
        self._internal: dict[Any, PluginRuntime] = {}
        self._keep_alive: dict[int, Any] = {}

    def _key(self, identity: Any) -> Any:
        """计算插件身份的字典键；不可哈希的对象（如字典）按 id 记并保活。"""
        try:
            hash(identity)
        except TypeError:
            key = id(identity)
            self._keep_alive[key] = identity
            return key
        return identity

    # ================================================================ 计数与遍历
    @property
    def counter(self) -> int:
        self._counter += 1
        return self._counter

    def __len__(self) -> int:
        return len(self._internal)

    def keys(self) -> Iterable[Any]:
        return self._internal.keys()

    def values(self) -> Iterable[PluginRuntime]:
        return self._internal.values()

    def entries(self) -> Iterable[tuple[Any, PluginRuntime]]:
        return self._internal.items()

    def has(self, plugin: Any) -> bool:
        resolved = resolve_plugin(plugin)
        if resolved is None:
            return False
        return self._key(resolved.identity) in self._internal

    def get(self, plugin: Any) -> Optional[PluginRuntime]:
        resolved = resolve_plugin(plugin)
        if resolved is None:
            return None
        return self._internal.get(self._key(resolved.identity))

    def delete(self, plugin: Any) -> Optional[PluginRuntime]:
        """注销插件：dispose 它的所有 fiber 并移除 runtime 记录。"""
        resolved = resolve_plugin(plugin)
        if resolved is None:
            return None
        runtime = self._internal.pop(self._key(resolved.identity), None)
        if runtime is None:
            return None
        for fiber in list(runtime.fibers):
            fiber.dispose()
        return runtime

    # ================================================================ 启动插件
    def plugin(self, ctx: Any, plugin: Any, config: Any = None) -> Fiber:
        """在当前上下文中启动插件，返回对应的 fiber。"""
        resolved = resolve_plugin(plugin)
        if resolved is None:
            raise CordisError('INVALID_PLUGIN', f'{CordisError.Code.INVALID_PLUGIN}, received {type(plugin).__name__}')
        ctx.fiber.assert_active()

        key = self._key(resolved.identity)
        runtime = self._internal.get(key)
        if runtime is None:
            name = resolved.name
            if name == 'apply':
                name = None
            runtime = PluginRuntime(name, resolved.callback, resolved.Config)
            self._internal[key] = runtime

        fiber = Fiber(ctx, config, Inject.resolve(resolved.inject), runtime)
        return fiber

    def inject(self, ctx: Any, deps: Any, callback: Callable[..., Any]) -> Fiber:
        """等待依赖就绪后再运行回调（``ctx.plugin({inject, apply})`` 的简写）。"""
        name = getattr(callback, '__name__', None)
        return self.plugin(ctx, {'inject': deps, 'apply': callback, 'name': name}, None)
