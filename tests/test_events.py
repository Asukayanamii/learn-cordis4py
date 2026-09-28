"""事件分发模式测试。"""

import asyncio

import pytest

from cordis import Context
from cordis.utils import maybe_await

pytestmark = pytest.mark.anyio


async def test_emit_is_sync_broadcast():
    ctx = Context()
    order = []
    ctx.on('demo', lambda value: order.append(('a', value)))
    ctx.on('demo', lambda value: order.append(('b', value)))
    ctx.emit('demo', 1)
    assert order == [('a', 1), ('b', 1)]


async def test_parallel_runs_all_and_awaits():
    ctx = Context()
    order = []

    async def slow():
        await asyncio.sleep(0)
        order.append('slow')

    ctx.on('demo', lambda: order.append('sync'))
    ctx.on('demo', slow)
    await ctx.parallel('demo')
    assert sorted(order) == ['slow', 'sync']


async def test_parallel_aggregates_errors():
    ctx = Context()

    async def broken():
        raise ValueError('boom')

    ctx.on('demo', broken)
    with pytest.raises(ExceptionGroup) as info:
        await ctx.parallel('demo')
    assert any(isinstance(error, ValueError) for error in info.value.exceptions)


async def test_serial_stops_at_first_bail_value():
    ctx = Context()
    order = []

    async def first():
        order.append('first')
        return None

    async def second():
        order.append('second')
        return 'winner'

    async def third():  # pragma: no cover - 不应执行
        order.append('third')
        return 'loser'

    ctx.on('demo', first)
    ctx.on('demo', second)
    ctx.on('demo', third)
    assert await ctx.serial('demo') == 'winner'
    assert order == ['first', 'second']


async def test_bail_sync_and_once():
    ctx = Context()
    calls = []

    def listener(value):
        calls.append(value)
        return True

    ctx.once('demo', listener)
    assert ctx.bail('demo', 1) is True
    assert calls == [1]
    assert ctx.bail('demo', 2) is None
    assert calls == [1]


async def test_waterfall_wraps_and_short_circuits():
    ctx = Context()
    seen = []

    async def outer(value, next):
        result = await next()
        return result.upper()

    async def blocker(value, next):
        seen.append(value)
        if 'blocked' in value:
            return '** blocked **'
        return await next()

    ctx.on('demo', outer)
    ctx.on('demo', blocker)

    async def default(value, next):
        return value

    assert await ctx.waterfall('demo', 'hello', default) == 'HELLO'
    assert await ctx.waterfall('demo', 'blocked words', default) == '** BLOCKED **'
    assert seen == ['hello', 'blocked words']


async def test_waterfall_normal_listener_must_call_next():
    ctx = Context()
    log = []

    def observer(value, next):
        log.append(value)  # 忘了 next()：下游与默认行为都被吞掉
        return 'swallowed'

    ctx.on('demo', observer)

    async def default(value, next):
        log.append('default')
        return value

    assert await maybe_await(ctx.waterfall('demo', 'x', default)) == 'swallowed'
    assert log == ['x']


async def test_prepend_order():
    ctx = Context()
    order = []
    ctx.on('demo', lambda: order.append('normal'))
    ctx.on('demo', lambda: order.append('prepended'), prepend=True)
    ctx.emit('demo')
    assert order == ['prepended', 'normal']


async def test_listeners_are_removed_with_owner():
    ctx = Context()
    order = []

    def plugin(ctx):
        ctx.on('demo', lambda: order.append('plugin'))
        ctx.cleanup(lambda: order.append('cleanup'))

    fiber = ctx.plugin(plugin)
    await fiber
    ctx.emit('demo')
    assert order == ['plugin']

    await fiber.dispose()
    ctx.emit('demo')
    assert order == ['plugin', 'cleanup']


async def test_once_self_disposes():
    ctx = Context()
    calls = []
    ctx.once('demo', lambda: calls.append(1))
    ctx.emit('demo')
    ctx.emit('demo')
    assert calls == [1]


async def test_internal_dispatch_event_observes_framework():
    ctx = Context()
    seen = []

    def observer(mode, name, args, this_ctx):
        seen.append((mode, name))

    ctx.on('internal/dispatch', observer)
    ctx.emit('hello', 1)
    await ctx.parallel('hello')
    assert ('emit', 'hello') in seen
    assert ('parallel', 'hello') in seen


async def test_isolated_service_listener_filtering():
    """internal/service 通知会按隔离作用域过滤。"""
    ctx = Context()
    seen = []
    ctx.on('internal/service', lambda name, value: seen.append(name))
    ctx.provide('svc', 1)
    assert 'svc' in seen
