"""cordis：Cordis 框架的 Python 复刻版。

一个"万物皆插件"的依赖注入 + 事件驱动框架：

.. code-block:: python

    import asyncio
    from cordis import Context, Service

    class Greeter(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'greeter')

        def greet(self, who):
            return f'Hello, {who}!'

    def consumer(ctx):
        ctx.logger.info(ctx.greeter.greet('world'))

    async def main():
        ctx = Context()
        ctx.plugin(Greeter)
        ctx.plugin(consumer, inject=['greeter'])   # 见 examples/01_hello.py 的写法
        await ctx.fiber.await_()

    asyncio.run(main())
"""

from .context import Context
from .events import EventsService, Hook
from .fiber import Disposer, EffectMeta, Fiber, FiberState, PluginRuntime
from .logger import ConsoleExporter, Exporter, Logger, LoggerLevel, LoggerService
from .reflect import Impl, Property, ReflectService
from .registry import Inject, RegistryService, resolve_plugin
from .schema import Issue, Schema, as_schema, validate_config
from .service import Service
from .utils import CordisError, DisposableList, Symbol, ValidationError, symbols

__version__ = '0.1.0'

__all__ = [
    'Context',
    'Service',
    'Fiber',
    'FiberState',
    'Disposer',
    'EffectMeta',
    'PluginRuntime',
    'EventsService',
    'Hook',
    'ReflectService',
    'RegistryService',
    'Logger',
    'LoggerService',
    'LoggerLevel',
    'Exporter',
    'ConsoleExporter',
    'Schema',
    'Issue',
    'as_schema',
    'validate_config',
    'Inject',
    'resolve_plugin',
    'CordisError',
    'ValidationError',
    'DisposableList',
    'Symbol',
    'symbols',
    'Property',
    'Impl',
    '__version__',
]
