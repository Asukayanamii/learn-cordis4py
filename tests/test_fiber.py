"""fiber 生命周期与 effect 测试。"""

import asyncio
from contextlib import contextmanager

import pytest

from cordis import Context, CordisError, FiberState, Service, ValidationError, Schema

pytestmark = pytest.mark.anyio


async def test_lifecycle_states_and_transitions():
    ctx = Context()
    states = []
    ctx.on('internal/status', lambda fiber, old: states.append((fiber.state.name, old.name)))

    def plugin(ctx):
        states.append(('apply',))

    fiber = ctx.plugin(plugin)
    await fiber
    assert fiber.state is FiberState.ACTIVE
    assert ('apply',) in states
    assert states[0] == ('LOADING', 'PENDING')

    await fiber.dispose()
    assert fiber.state is FiberState.DISPOSED
    assert fiber.uid is None


async def test_plugin_config_and_failure():
    ctx = Context()

    def broken(ctx):
        raise RuntimeError('boom')

    fiber = ctx.plugin(broken)
    with pytest.raises(RuntimeError, match='boom'):
        await fiber
    assert fiber.state is FiberState.FAILED


async def test_config_validation_error():
    ctx = Context()

    class Config(Schema):
        port = Schema.integer().required()

    def plugin(ctx, config):
        raise AssertionError('不应运行')

    plugin.Config = Config
    fiber = ctx.plugin(plugin, {'port': 'x'})
    with pytest.raises(ValidationError):
        await fiber
    assert fiber.state is FiberState.FAILED


async def test_config_validation_result_visible():
    ctx = Context()

    class Config(Schema):
        port = Schema.integer().default(8080)

    captured = {}

    def plugin(ctx, config):
        captured.update(config)

    plugin.Config = Config
    await ctx.plugin(plugin, {})
    assert captured == {'port': 8080}


async def test_effect_disposal_is_reverse_order_and_idempotent():
    ctx = Context()
    order = []

    def plugin(ctx):
        ctx.cleanup(lambda: order.append('first'), 'a')
        ctx.cleanup(lambda: order.append('second'), 'b')
        ctx.cleanup(lambda: order.append('third'), 'c')

    fiber = ctx.plugin(plugin)
    await fiber
    handle = ctx.effect(lambda: order.append('outer'))
    handle()
    handle()  # 幂等
    assert order == ['outer']

    await fiber.dispose()
    assert order == ['outer', 'third', 'second', 'first']


async def test_effect_forms():
    ctx = Context()
    log = []

    def generator_effect():
        log.append('setup')
        yield lambda: log.append('gen-cleanup')
        yield lambda: log.append('gen-cleanup-2')

    @contextmanager
    def cm_effect():
        log.append('cm-enter')
        try:
            yield
        finally:
            log.append('cm-exit')

    async def async_effect():
        log.append('async-setup')
        return lambda: log.append('async-cleanup')

    def plugin(ctx):
        ctx.effect(generator_effect)
        ctx.effect(cm_effect)
        ctx.effect(async_effect)

    fiber = ctx.plugin(plugin)
    await fiber
    await asyncio.sleep(0)
    assert 'setup' in log and 'cm-enter' in log and 'async-setup' in log

    await fiber.dispose()
    assert 'gen-cleanup-2' in log and 'gen-cleanup' in log
    assert 'cm-exit' in log
    assert 'async-cleanup' in log


async def test_effect_on_disposed_fiber_raises():
    ctx = Context()
    captured = {}

    def plugin(ctx):
        captured['ctx'] = ctx

    fiber = ctx.plugin(plugin)
    await fiber
    await fiber.dispose()
    with pytest.raises(CordisError):
        captured['ctx'].effect(lambda: None)


async def test_async_disposer_awaited_on_dispose():
    ctx = Context()
    log = []

    def plugin(ctx):
        def disposer():
            async def work():
                await asyncio.sleep(0)
                log.append('async-disposed')
            return work()
        ctx.cleanup(disposer)

    fiber = ctx.plugin(plugin)
    await fiber
    await fiber.dispose()
    assert log == ['async-disposed']


async def test_effect_returns_disposer_from_plugin_body():
    ctx = Context()
    log = []

    def plugin(ctx):
        return lambda: log.append('body-disposer')

    fiber = ctx.plugin(plugin)
    await fiber
    await fiber.dispose()
    assert log == ['body-disposer']


async def test_restart_and_update():
    ctx = Context()
    versions = []

    def plugin(ctx, config):
        versions.append(config)

    await ctx.plugin(plugin, {'v': 1})
    assert versions == [{'v': 1}]

    fiber = ctx.registry.get(plugin).fibers
    instance = list(fiber)[0]
    await instance.restart()
    assert versions == [{'v': 1}, {'v': 1}]

    instance.update({'v': 2})
    await instance
    assert versions == [{'v': 1}, {'v': 1}, {'v': 2}]


async def test_child_plugins_disposed_with_parent():
    ctx = Context()
    log = []

    def child(ctx):
        ctx.cleanup(lambda: log.append('child'))

    def parent(ctx):
        ctx.plugin(child)
        ctx.cleanup(lambda: log.append('parent'))

    fiber = ctx.plugin(parent)
    await fiber
    await fiber.dispose()
    # 逆序释放：父插件自己的清理先跑，随后才递归卸载子插件
    assert log == ['parent', 'child']


async def test_inject_waits_for_service_and_reloads():
    ctx = Context()
    events = []

    class Svc(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'svc')

    def consumer(ctx):
        events.append('loaded')
        ctx.cleanup(lambda: events.append('unloaded'))

    fiber = ctx.inject(['svc'], consumer)
    await fiber
    assert fiber.state is FiberState.PENDING
    assert events == []

    provider = ctx.plugin(Svc)
    await provider
    await fiber
    assert events == ['loaded']
    assert fiber.state is FiberState.ACTIVE

    # 提供方卸载 → 依赖方也随之卸载
    await provider.dispose()
    await fiber
    assert events == ['loaded', 'unloaded']
    assert fiber.state is FiberState.PENDING

    # 提供方回来 → 依赖方重新加载
    provider2 = ctx.plugin(Svc)
    await provider2
    await fiber
    assert events == ['loaded', 'unloaded', 'loaded']
    assert fiber.state is FiberState.ACTIVE
