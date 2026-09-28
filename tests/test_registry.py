"""插件注册表与依赖注入测试。"""

import pytest

from cordis import Context, CordisError, Service

pytestmark = pytest.mark.anyio


async def test_three_plugin_shapes():
    ctx = Context()
    log = []

    def function_plugin(ctx):
        log.append('function')

    class ObjectPlugin:
        name = 'object-plugin'

        def apply(self, ctx):
            log.append('object')

    class ClassPlugin(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'classPlugin')
            log.append('class')

    await ctx.plugin(function_plugin)
    await ctx.plugin(ObjectPlugin())
    await ctx.plugin(ClassPlugin)
    assert log == ['function', 'object', 'class']
    assert ctx.get('classPlugin') is not None


async def test_plugin_metadata_from_module_style_attributes():
    ctx = Context()
    captured = {}

    def plugin(ctx, config):
        captured.update(config or {})

    plugin.name = 'configured'
    await ctx.plugin(plugin, {'a': 1})
    assert captured == {'a': 1}


async def test_invalid_plugin_raises():
    ctx = Context()
    with pytest.raises(CordisError):
        ctx.plugin(42)


async def test_inject_array_and_dict_forms():
    ctx = Context()
    log = []

    class Svc(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'svc')

    await ctx.plugin(Svc)

    def a(ctx):
        log.append('a')

    def b(ctx):
        log.append('b')

    a.inject = ['svc']
    b.inject = {'svc': {'option': 1}}
    await ctx.plugin(a)
    await ctx.plugin(b)
    assert sorted(log) == ['a', 'b']


async def test_ctx_inject_helper_waits():
    ctx = Context()
    order = []

    def consumer(ctx):
        order.append('consumer')

    fiber = ctx.inject(['dep'], consumer)
    await fiber
    assert order == []

    class Dep(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'dep')

    await ctx.plugin(Dep)
    await fiber
    assert order == ['consumer']


async def test_registry_has_get_delete():
    ctx = Context()

    def plugin(ctx):
        pass

    fiber = ctx.plugin(plugin)
    await fiber
    assert ctx.registry.has(plugin)
    runtime = ctx.registry.get(plugin)
    assert runtime is not None and len(runtime.fibers) == 1

    ctx.registry.delete(plugin)
    await fiber
    assert not ctx.registry.has(plugin)
    assert fiber.state.name == 'DISPOSED'


async def test_multiple_fibers_share_runtime():
    ctx = Context()
    calls = []

    def plugin(ctx, config):
        calls.append(config)

    f1 = ctx.plugin(plugin, 1)
    f2 = ctx.plugin(plugin, 2)
    await f1
    await f2
    runtime = ctx.registry.get(plugin)
    assert len(runtime.fibers) == 2
    assert calls == [1, 2]


async def test_plugin_disposed_by_parent_context():
    ctx = Context()
    scope = ctx.extend()
    log = []

    def plugin(ctx):
        ctx.cleanup(lambda: log.append('cleanup'))

    fiber = scope.plugin(plugin)
    await fiber
    await fiber.dispose()
    assert log == ['cleanup']


async def test_service_replacement_reloads_dependents():
    ctx = Context()
    seen = []

    class ProviderA(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'cap')
            self.tag = 'A'

    class ProviderB(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'cap')
            self.tag = 'B'

    def consumer(ctx):
        seen.append(ctx.cap.tag)

    consumer.inject = ['cap']
    a = ctx.plugin(ProviderA)
    await a
    fiber = ctx.plugin(consumer)
    await fiber
    assert seen == ['A']

    # 替换提供方：卸载 A，挂载 B，消费方自动重启
    await a.dispose()
    await fiber
    assert fiber.state.name == 'PENDING'

    b = ctx.plugin(ProviderB)
    await b
    await fiber
    assert seen == ['A', 'B']
