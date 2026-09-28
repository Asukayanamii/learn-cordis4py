"""反射层：``ctx.reflect``。

它负责三件事：

1. **服务存储**：以"隔离标签 → 实现"的形式保存每个服务（``store``）；
2. **属性声明**：维护 ``props``（服务属性与访问器），上下文代理的
   ``ctx.xxx`` 读取最终都落到这里；
3. **调用方绑定**：服务被别的插件访问时，返回一个轻量代理，让服务方法内的
   ``self.ctx`` 指向**调用方上下文**（``Service.ctx`` 会优先返回它），
   因此服务里的注册（如 ``ctx.tools.register(...)``）会挂到调用方 fiber，
   随调用方卸载而自动撤销——这正是 TS 版可追踪代理在这里的等价物。
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, Optional

from .fiber import FiberState
from .utils import CordisError, Symbol, current_caller, is_awaitable

__all__ = ['ReflectService', 'Property', 'Impl', 'ServiceBindingProxy']


class Property:
    """一条上下文属性声明：服务或访问器。"""

    __slots__ = ('kind', 'get', 'set')

    def __init__(self, kind: str, get: Callable[..., Any] | None = None, set: Callable[..., Any] | None = None) -> None:
        self.kind = kind
        self.get = get
        self.set = set

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'<Property {self.kind}>'


class Impl:
    """一个具体的服务实现记录。"""

    __slots__ = ('name', 'fiber', 'value', 'check', 'bindings')

    def __init__(self, name: str, fiber: Any, value: Any = None, check: Callable[[], bool] | None = None) -> None:
        self.name = name
        self.fiber = fiber
        self.value = value
        self.check = check
        self.bindings: dict[int, 'ServiceBindingProxy'] = {}


class ServiceBindingProxy:
    """把服务方法调用绑定到调用方上下文的轻量代理。"""

    __slots__ = ('_target', '_ctx')

    def __init__(self, target: Any, ctx: Any) -> None:
        object.__setattr__(self, '_target', target)
        object.__setattr__(self, '_ctx', ctx)

    @property
    def __class__(self) -> type:  # 让 isinstance(proxy, MyService) 成立
        return type(object.__getattribute__(self, '_target'))

    def __getattr__(self, name: str) -> Any:
        if name.startswith('_'):
            raise AttributeError(name)
        target = object.__getattribute__(self, '_target')
        ctx = object.__getattribute__(self, '_ctx')
        value = getattr(target, name)
        if not callable(value):
            return value
        return _bind_callable(target, value, ctx)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        target = object.__getattribute__(self, '_target')
        ctx = object.__getattribute__(self, '_ctx')
        return _bind_callable(target, target, ctx)(*args, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        target = object.__getattribute__(self, '_target')
        return f'<ServiceBinding {type(target).__name__}>'


def _bind_callable(service: Any, method: Callable[..., Any], ctx: Any) -> Callable[..., Any]:
    """包装服务可调用对象：调用期间 ``current_caller`` 指向调用方上下文。"""

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        token = current_caller.set(ctx)
        try:
            result = method(*args, **kwargs)
        finally:
            current_caller.reset(token)
        if is_awaitable(result):
            return _await_with_caller(result, ctx)
        return result

    return wrapper


async def _await_with_caller(awaitable: Any, ctx: Any) -> Any:
    token = current_caller.set(ctx)
    try:
        return await awaitable
    finally:
        current_caller.reset(token)


class ReflectService:
    """服务解析、注册与访问器：上下文代理背后的反射层。"""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        #: 隔离标签 → 服务实现
        self.store: dict[Any, Impl] = {}
        #: 属性名 → 声明（服务 / 访问器）
        self.props: dict[str, Property] = {}
        #: 服务名的全局默认隔离标签（首个 provide 时分配）
        self.labels: dict[str, Symbol] = {}

    # ================================================================ 隔离标签
    def label_of(self, ctx: Any, name: str) -> Any:
        """计算某个上下文里服务名的有效隔离标签。"""
        own = getattr(ctx, '_isolate_own', None)
        if own and name in own:
            return own[name]
        return self.labels.get(name)

    # ================================================================ 查询
    def _get_impl(self, ctx: Any, name: str, strict: bool = True) -> Impl | None:
        key = self.label_of(ctx, name)
        if key is None:
            return None
        impl = self.store.get(key)
        if impl is None:
            return None
        if strict and impl.fiber.state != FiberState.ACTIVE:
            return None
        return impl

    def get(self, ctx: Any, name: str, strict: bool = True) -> Any:
        """读取服务原始值（不做调用方绑定）。"""
        impl = self._get_impl(ctx, name, strict)
        if impl is None:
            return None
        return impl.value

    def resolve(self, ctx: Any, name: str) -> Any:
        """沿 fiber 链解析 ``ctx.name``，返回（必要时绑定到调用方的）服务值。

        解析规则：

        - **根上下文**：按隔离标签直接查全局实现（``ctx.get`` 的语义）；
        - **插件上下文**：从当前 fiber 沿父链查找；
          fiber 快照（``inject`` 注入的、或自己提供的服务）优先，
          然后向祖先 fiber 查找；
        - 隔离标签发生变化即到达作用域边界，**不会**越界拿到外层实现；
        - 声明了 ``inject`` 但当前不可用时，抛出明确的
          ``SERVICE_INACTIVE`` 错误（这正是"插件为何没输出"的常见答案）。
        """
        fiber = ctx.fiber
        key = self.label_of(ctx, name)

        if fiber.runtime is None:
            impl = self.store.get(key) if key is not None else None
            if impl is None:
                raise AttributeError(f'cannot get property "{name}" without inject')
            return self._bind(ctx, impl)

        impl: Impl | None = None
        while True:
            store = fiber.store
            if store is not None and name in store and self.label_of(fiber.ctx, name) == key:
                impl = store[name]
                break
            if name in fiber.inject:
                raise CordisError(
                    'SERVICE_INACTIVE',
                    f'cannot get required service "{name}" in inactive context',
                )
            parent_ctx = fiber.parent
            if fiber.runtime is None or self.label_of(parent_ctx, name) != key:
                break
            fiber = parent_ctx.fiber

        if impl is None:
            raise AttributeError(f'cannot get property "{name}" without inject')
        return self._bind(ctx, impl)

    def _bind(self, ctx: Any, impl: Impl) -> Any:
        """如果服务由其他 fiber 提供，返回绑定到调用方的代理。"""
        value = impl.value
        if getattr(value, '_cordis_service', False) is not True:
            return value
        owner_ctx = getattr(value, '_service_ctx', None)
        if owner_ctx is not None and owner_ctx.fiber is ctx.fiber:
            return value
        uid = ctx.fiber.uid
        if uid is None:
            return value
        proxy = impl.bindings.get(uid)
        if proxy is None:
            proxy = ServiceBindingProxy(value, ctx)
            impl.bindings[uid] = proxy
        return proxy

    # ================================================================ 注册
    def provide(
        self,
        ctx: Any,
        name: str,
        value: Any = None,
        check: Callable[[], bool] | None = None,
    ) -> Any:
        """注册一个归当前 fiber 所有的服务实现，返回撤销句柄。"""
        fiber = ctx.fiber

        def install() -> Callable[[], Any]:
            prop = self.props.get(name)
            if prop is None:
                self.props[name] = Property('service')
            elif prop.kind != 'service':
                raise CordisError('PROPERTY_DECLARED', f'property "{name}" is already declared as {prop.kind}')

            self.labels.setdefault(name, Symbol(name))
            key = self.label_of(ctx, name)
            existing = self.store.get(key)
            if existing is not None:
                raise CordisError(
                    'SERVICE_DUPLICATE',
                    f'service "{name}" has been registered at <{existing.fiber.name}>',
                )
            impl = Impl(name, fiber, value, check)
            self.store[key] = impl
            # 只有"在自己作用域内提供"时才写入 fiber 快照；
            # 隔离作用域里提供的实现不会污染提供方 fiber 的直接查找。
            if fiber.store is not None and self.label_of(fiber.ctx, name) == key:
                fiber.store[name] = impl
            if fiber.state == FiberState.ACTIVE:
                self.notify([name], ctx)

            async def _await_dependents(fibers: list[Any]) -> None:
                await asyncio.gather(*[f.await_() for f in fibers], return_exceptions=True)

            def uninstall() -> Any:
                # 同步前缀立即生效：服务立刻消失、依赖方立刻被唤醒；
                # 需要等待的尾部以 awaitable 形式返回。
                self.store.pop(key, None)
                dependents = self.notify([name], ctx)
                if fiber.store is not None and fiber.store.get(name) is impl:
                    fiber.store.pop(name, None)
                if dependents:
                    return _await_dependents(dependents)
                return None

            return uninstall

        return fiber.effect(install, f'ctx.provide({name!r})')

    def set(self, ctx: Any, name: str, value: Any) -> None:
        """覆盖已提供服务的值（只有提供方 fiber 可以设置）。"""
        impl = self._get_impl(ctx, name, strict=False)
        if impl is None:
            raise CordisError('SERVICE_NOT_PROVIDED', f'cannot set property "{name}" without provide')
        if impl.fiber is not ctx.fiber:
            raise CordisError('SERVICE_NOT_PROVIDED', f'cannot set property "{name}" in multiple fibers')
        impl.value = value

    def accessor(
        self,
        ctx: Any,
        name: str,
        get: Callable[[Any], Any],
        set: Callable[[Any, Any], Any] | None = None,
    ) -> Any:
        """声明一个由 get/set 钩子支持的计算型上下文属性。"""

        def install() -> Callable[[], Any]:
            if name in self.props:
                raise CordisError('PROPERTY_DECLARED', f'property "{name}" is already declared as {self.props[name].kind}')
            self.props[name] = Property('accessor', get, set)
            return lambda: self.props.pop(name, None)

        return ctx.fiber.effect(install, f'ctx.accessor({name!r})')

    def mixin(self, ctx: Any, source: str, keys: Any) -> Any:
        """把某个服务的成员直接暴露到 ``ctx`` 上（例如 ``ctx.on``）。"""
        entries = list(keys.items()) if isinstance(keys, dict) else [(key, key) for key in keys]

        def install_entries() -> list[Any]:
            handles = []
            for key, alias in entries:
                def getter(accessing: Any, _source: str = source, _key: Any = key) -> Any:
                    service = accessing.get(_source)
                    if service is None:
                        raise CordisError('SERVICE_INACTIVE', f'cannot mixin from missing service "{_source}"')
                    return getattr(service, _key)

                def setter(accessing: Any, value: Any, _source: str = source, _key: Any = key) -> None:
                    service = accessing.get(_source)
                    setattr(service, _key, value)

                handles.append(self.accessor(ctx, alias, getter, setter))
            return handles

        return ctx.fiber.effect(install_entries, f'ctx.mixin({source!r})')

    # ================================================================ 通知
    def notify(self, names: list[str], origin_ctx: Any = None) -> list[Any]:
        """重新评估所有依赖这些服务的 fiber，返回被刷新的 fiber 列表。"""
        origin = origin_ctx if origin_ctx is not None else self.ctx
        origin_labels = {name: self.label_of(origin, name) for name in names}

        refreshed: list[Any] = []
        for runtime in list(self.ctx.registry.values()):
            for fiber in list(runtime.fibers):
                has_update = False
                for name in names:
                    if name not in fiber.inject:
                        continue
                    if self.label_of(fiber.ctx, name) != origin_labels[name]:
                        continue
                    has_update = True
                    fiber._check_impl(name)
                if not has_update:
                    continue
                fiber._refresh()
                refreshed.append(fiber)

        for name in names:
            def filter_for(ctx: Any, _name: str = name) -> bool:
                return self.label_of(ctx, _name) == origin_labels[_name]

            impl = self._get_impl(origin, name, strict=False)
            self.ctx.events.emit_filtered('internal/service', filter_for, name, impl.value if impl else None)
        return refreshed
