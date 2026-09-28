"""上下文：Cordis 的核心对象。

所有服务、事件与生命周期 API 都通过 ``ctx`` 访问：

- **上下文是服务的容器**：``ctx.tools`` / ``ctx.llm`` 这类属性读取会走
  服务解析（``__getattr__``），而不是导入具体实现；
- **``extend()`` / ``isolate()`` / ``intercept()`` 派生作用域子上下文**，
  不修改父上下文；
- **``ctx.on`` / ``ctx.emit`` ... 是事件总线的门面**，
  ``ctx.plugin`` / ``ctx.inject`` 来自插件注册表，
  ``ctx.effect`` 来自当前 fiber —— 它们都默认作用于"调用方自己"，
  因此注册会随调用方插件卸载自动撤销。

Python 没有 JS 的 Proxy，这里的适配是：

- 属性读取用 ``__getattr__``（只在本类没有该属性时触发）实现服务解析；
- 属性写入用 ``__setattr__`` 实现 "必须 provide 后才能赋值"；
- 服务方法的**调用方绑定**由 :class:`~cordis.reflect.ServiceBindingProxy`
  完成（``Service.ctx`` 会优先返回调用方上下文）。
"""

from __future__ import annotations

from typing import Any, Callable, Iterator, Optional

from .events import EventsService
from .fiber import Fiber
from .logger import ConsoleExporter, LoggerService, log_level_from_env
from .reflect import ReflectService, ServiceBindingProxy
from .registry import RegistryService
from .utils import Symbol

__all__ = ['Context']


class Context:
    """根与子依赖容器。"""

    #: 允许直接赋值的框架内部属性
    _INTERNAL_ATTRS = frozenset({'root', 'baseUrl', 'fiber', 'reflect', 'registry'})
    #: 核心服务：对外是"调用方绑定"的代理，内部用下划线属性访问
    _SERVICE_ATTRS = {'events': '_events', 'logger': '_logger'}

    def __init__(self) -> None:
        object.__setattr__(self, '_isolate_own', {})
        object.__setattr__(self, '_intercepts', [])
        object.__setattr__(self, '_own', {})
        object.__setattr__(self, '_binding_cache', {})
        self.root = self
        self.baseUrl: Optional[str] = None
        self.fiber = Fiber(self, None, {}, None)
        self.reflect = ReflectService(self)
        self.registry = RegistryService(self)
        self.events = EventsService(self)
        self.logger = LoggerService(self)
        # 默认安装控制台日志出口（级别可用 CORDIS_LOG_LEVEL 控制）
        self._logger.exporter(ConsoleExporter({'default': log_level_from_env()}))
        # 构造期注册的内建 effect 就地"固化"：不进入任何可卸载列表
        self.fiber._disposables.clear()

    # ================================================================ 基础
    @staticmethod
    def is_(value: Any) -> bool:
        """判断一个值是否为 Cordis 上下文。"""
        return isinstance(value, Context)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'Context <{self.fiber.name}>'

    @property
    def events(self) -> EventsService:
        """事件总线（绑定到调用方，方法内的 ``service.ctx`` 即当前上下文）。"""
        return self._bind_service('_events')

    @property
    def logger(self) -> LoggerService:
        """日志服务（默认用当前 fiber 的名字记录）。"""
        return self._bind_service('_logger')

    def _bind_service(self, attr: str) -> Any:
        service = object.__getattribute__(self, attr)
        cache = self.__dict__.get('_binding_cache')
        if cache is None:
            return service
        proxy = cache.get(attr)
        if proxy is None:
            proxy = ServiceBindingProxy(service, self)
            cache[attr] = proxy
        return proxy

    # ================================================================ 属性解析
    def __getattr__(self, name: str) -> Any:
        if name.startswith('_'):
            raise AttributeError(name)
        own = self.__dict__.get('_own')
        if own and name in own:
            return own[name]
        reflect = self.__dict__.get('reflect')
        if reflect is None:
            raise AttributeError(name)
        prop = reflect.props.get(name)
        if prop is not None and prop.kind == 'accessor' and prop.get is not None:
            return prop.get(self)
        return reflect.resolve(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        target = Context._SERVICE_ATTRS.get(name, name)
        if name.startswith('_') or name in Context._INTERNAL_ATTRS or name in Context._SERVICE_ATTRS:
            object.__setattr__(self, target, value)
            return
        reflect = self.__dict__.get('reflect')
        fiber = self.__dict__.get('fiber')
        prop = reflect.props.get(name) if reflect is not None else None
        if prop is None:
            # 未声明的属性：根上下文允许（用于应用级配置），插件上下文拒绝
            if reflect is None or fiber is None or fiber.runtime is None:
                object.__setattr__(self, name, value)
                return
            from .utils import CordisError
            raise CordisError('SERVICE_NOT_PROVIDED', f'cannot set property "{name}" without provide')
        if prop.kind == 'accessor':
            if prop.set is None:
                raise AttributeError(f'property "{name}" is read-only')
            prop.set(self, value)
            return
        reflect.set(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f'cannot delete property "{name}" from context')

    # ================================================================ 作用域派生
    def extend(self, meta: dict[str, Any] | None = None) -> 'Context':
        """创建携带额外元数据的子上下文（父上下文不受影响）。"""
        child = object.__new__(type(self))
        child.__dict__.update(self.__dict__)
        child.__dict__['_own'] = dict(self.__dict__.get('_own') or {})
        child.__dict__['_isolate_own'] = dict(self.__dict__.get('_isolate_own') or {})
        child.__dict__['_intercepts'] = list(self.__dict__.get('_intercepts') or [])
        child.__dict__['_binding_cache'] = {}
        for key, value in (meta or {}).items():
            if key.startswith('_') or key in Context._INTERNAL_ATTRS or key in Context._SERVICE_ATTRS:
                object.__setattr__(child, Context._SERVICE_ATTRS.get(key, key), value)
            else:
                child.__dict__['_own'][key] = value
        return child

    def isolate(self, name: str, label: Any = None) -> 'Context':
        """让 ``name`` 服务在子上下文中拥有独立作用域。

        同一个 ``label`` 传给两次 ``isolate()`` 会让两个作用域合并；
        不传则自动新建唯一标签。
        """
        child = self.extend()
        child.__dict__['_isolate_own'][name] = label if label is not None else Symbol(name)
        return child

    def intercept(self, name: str, config: Any) -> 'Context':
        """为在子上下文下启动的插件追加服务拦截配置（祖先条目在前）。"""
        child = self.extend()
        child.__dict__['_intercepts'].append((name, config))
        return child

    # ================================================================ 服务 API
    def get(self, name: str, strict: bool = True) -> Any:
        """读取服务原始值（不做调用方绑定）；未提供时返回 ``None``。"""
        return self.reflect.get(self, name, strict)

    def set(self, name: str, value: Any) -> None:
        """覆盖已提供服务的值（只有提供方 fiber 可以设置）。"""
        self.reflect.set(self, name, value)

    def provide(self, name: str, value: Any = None, check: Callable[[], bool] | None = None) -> Any:
        """注册归当前 fiber 所有的服务实现，返回撤销句柄。"""
        return self.reflect.provide(self, name, value, check)

    def accessor(self, name: str, get: Callable[[Any], Any], set: Callable[[Any, Any], Any] | None = None) -> Any:
        """声明一个计算型上下文属性。"""
        return self.reflect.accessor(self, name, get, set)

    def mixin(self, source: str, keys: Any) -> Any:
        """把某个服务的成员直接暴露到 ``ctx`` 上。"""
        return self.reflect.mixin(self, source, keys)

    # ================================================================ 生命周期
    def effect(self, execute: Callable[[], Any], label: str = 'anonymous') -> Any:
        """在当前 fiber 上注册一个支持清理的 effect。

        ``execute`` 会立即执行；它返回的 disposer（或上下文管理器）会在
        卸载时释放。只登记清理函数时可用 :meth:`cleanup`。
        """
        return self.fiber.effect(execute, label)

    def cleanup(self, disposer: Callable[[], Any], label: str = 'ctx.cleanup()') -> Any:
        """登记一个在 fiber 卸载时执行的清理函数（不立即执行）。"""
        return self.fiber.effect(lambda: disposer, label)

    def plugin(self, plugin: Any, config: Any = None) -> Fiber:
        """在当前上下文中加载插件，返回 fiber。"""
        return self.registry.plugin(self, plugin, config)

    def inject(self, deps: Any, callback: Callable[..., Any]) -> Fiber:
        """等待 ``deps`` 就绪后运行回调（``ctx.plugin({inject, apply})`` 的简写）。"""
        return self.registry.inject(self, deps, callback)

    # ================================================================ 事件 API
    def on(
        self,
        name: Any,
        listener: Callable[..., Any],
        prepend: bool = False,
        global_: bool = False,
    ) -> Any:
        """注册监听器（随当前 fiber 卸载自动移除）。"""
        return self._events.on(name, listener, ctx=self, prepend=prepend, global_=global_)

    def once(
        self,
        name: Any,
        listener: Callable[..., Any],
        prepend: bool = False,
        global_: bool = False,
    ) -> Any:
        """注册一次性监听器。"""
        return self._events.once(name, listener, ctx=self, prepend=prepend, global_=global_)

    def emit(self, name: Any, *args: Any) -> None:
        """同步广播事件。"""
        self._events.emit(name, *args)

    async def parallel(self, name: Any, *args: Any) -> None:
        """并发分发事件并等待全部监听器。"""
        await self._events.parallel(name, *args)

    async def serial(self, name: Any, *args: Any) -> Any:
        """按顺序分发事件，首个 bail 值胜出。"""
        return await self._events.serial(name, *args)

    def bail(self, name: Any, *args: Any) -> Any:
        """同步版 serial。"""
        return self._events.bail(name, *args)

    def waterfall(self, name: Any, *args: Any) -> Any:
        """环绕中间件式分发；最后一个参数是最内层的 ``next``。"""
        return self._events.waterfall(name, *args)

    def __iter__(self) -> Iterator[str]:  # pragma: no cover - 防御性：避免被误当作可迭代对象
        raise TypeError('Context is not iterable')
