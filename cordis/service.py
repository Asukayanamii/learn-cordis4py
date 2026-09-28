"""服务基类。

一个**服务**是一个插件提供、其他插件通过 ``ctx`` 消费的具名能力::

    class GreeterService(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'greeter')

        def greet(self, who: str) -> str:
            return f'Hello, {who}!'

    ctx.plugin(GreeterService)     # 注册服务
    ctx.greeter.greet('world')     # 任何插件都可以消费

两个要点：

- ``super().__init__(ctx, name)`` 立即把实例注册到 ``ctx`` 上，并归属当前
  fiber；插件卸载时服务自动注销、依赖方自动重载。
- ``self.ctx`` 优先返回**调用方上下文**：当插件 A 调用 ``ctx.greeter.foo()``
  时，``foo`` 里的 ``self.ctx`` 就是 A 的上下文，A 在 ``foo`` 内注册的东西
  会挂到 A 的 fiber 上（对应 TS 版的可追踪代理）。
"""

from __future__ import annotations

from typing import Any, Callable

from .utils import CallerBound

__all__ = ['Service']


class Service(CallerBound):
    """所有服务的基类：``super().__init__(ctx, name)`` 即完成注册。"""

    #: 声明配置 schema（``Schema`` 实例或类声明式子类）
    Config: Any = None
    #: 声明依赖的服务（数组或 ``{name: 拦截配置}`` 映射）
    inject: Any = None
    #: 声明该服务默认注册名（可选，等价于 ``super().__init__(ctx, 'name')``）
    provide: str | None = None

    name: str

    def __init__(self, ctx: Any, name: str | None = None) -> None:
        resolved = name or type(self).provide
        if not resolved:
            raise TypeError(f'{type(self).__name__} 需要一个服务名（super().__init__(ctx, name)）')
        self._service_ctx = ctx
        self.name = resolved
        ctx.reflect.provide(ctx, resolved, self, self._availability())

    # ================================================================ 调用方绑定
    @property
    def owner_ctx(self) -> Any:
        """服务自身的上下文（永远是提供方 fiber 的上下文）。"""
        return self._service_ctx

    def _availability(self) -> Callable[[], bool] | None:
        """如果子类定义了 ``check(self) -> bool``，把它作为可用性谓词。"""
        check = getattr(type(self), 'check', None)
        if check is None or not callable(check):
            return None
        return self.check

    # ================================================================ 配置
    def resolve_config(self, base: Any = None, head: Any = None) -> Any:
        """合并祖先前置的拦截配置：祖先条目在前，``base`` 最先、``head`` 最后。

        服务声明了 ``Config.merge`` 时使用它，否则做浅合并。
        """
        configs: list[Any] = []
        for name, config in self._service_ctx._intercepts:
            if name == self.name:
                configs.append(config)
        if base is not None:
            configs.insert(0, base)
        if head is not None:
            configs.append(head)

        schema = type(self).Config
        merge = getattr(schema, 'merge', None)
        if merge is not None:
            return merge(*configs)
        result: dict[str, Any] = {}
        for config in configs:
            if config:
                result.update(config)
        return result
