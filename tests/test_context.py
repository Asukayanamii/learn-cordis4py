"""上下文 / 服务解析测试。"""

import pytest

from cordis import Context, CordisError, Service, Symbol


@pytest.fixture
def ctx():
    return Context()


@pytest.mark.anyio
async def test_provide_and_get(ctx):
    dispose = ctx.provide('config', {'port': 80})
    assert ctx.get('config') == {'port': 80}
    assert ctx.config == {'port': 80}
    dispose()
    assert ctx.get('config') is None
    with pytest.raises(AttributeError):
        ctx.config


@pytest.mark.anyio
async def test_unknown_property_raises_attribute_error(ctx):
    with pytest.raises(AttributeError):
        ctx.not_a_service
    assert not hasattr(ctx, 'not_a_service')


@pytest.mark.anyio
async def test_service_class_registers_and_disposes(ctx):
    class Foo(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'foo')

    fiber = ctx.plugin(Foo)
    await fiber
    assert isinstance(ctx.foo, Foo)
    await fiber.dispose()
    assert ctx.get('foo') is None


@pytest.mark.anyio
async def test_set_requires_matching_fiber(ctx):
    dispose = ctx.provide('foo', {'a': 1})
    ctx.set('foo', {'a': 2})
    assert ctx.get('foo') == {'a': 2}
    with pytest.raises(CordisError):
        ctx.set('bar', 1)
    dispose()


@pytest.mark.anyio
async def test_setattr_requires_provide(ctx):
    # 根上下文允许任意属性；插件上下文则必须 provide 后才能赋值
    ctx.whatever = 1
    assert ctx.whatever == 1

    errors = []

    def plugin(ctx):
        try:
            ctx.something = 1
        except CordisError as error:
            errors.append(error)

    await ctx.plugin(plugin)
    assert len(errors) == 1

    dispose = ctx.provide('thing', 1)
    ctx.thing = 2
    assert ctx.get('thing') == 2
    dispose()


@pytest.mark.anyio
async def test_extend_creates_child_scope(ctx):
    child = ctx.extend({'label': 'child'})
    assert child.label == 'child'
    assert child.root is ctx
    assert child.fiber is ctx.fiber
    assert not hasattr(ctx, 'label')


@pytest.mark.anyio
async def test_isolate_hides_outer_service(ctx):
    ctx.provide('svc', 'outer')
    isolated = ctx.isolate('svc')
    with pytest.raises(AttributeError):
        isolated.svc
    inner = isolated.provide('svc', 'inner')
    assert isolated.svc == 'inner'
    assert ctx.svc == 'outer'
    inner()


@pytest.mark.anyio
async def test_isolate_same_label_shares_scope(ctx):
    label = Symbol('shared')
    a = ctx.isolate('svc', label)
    b = ctx.isolate('svc', label)
    a.provide('svc', 'value')
    assert b.svc == 'value'
    assert ctx.get('svc') is None


@pytest.mark.anyio
async def test_isolated_plugin_does_not_see_outer_service(ctx):
    """隔离作用域下加载的插件注入同名服务时，会等待隔离作用域自己的提供方。"""
    ctx.provide('svc', 'outer')
    captured = {}

    def consumer(ctx):
        captured['value'] = ctx.svc

    scope = ctx.isolate('svc')
    fiber = scope.plugin({'inject': ['svc'], 'apply': consumer})
    await fiber  # PENDING：不满足注入
    assert fiber.state.name == 'PENDING'
    assert 'value' not in captured

    scope.provide('svc', 'inner')
    await fiber
    assert fiber.state.name == 'ACTIVE'
    assert captured['value'] == 'inner'


@pytest.mark.anyio
async def test_intercept_config_merges_for_child_plugins(ctx):
    class Svc(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'svc')
            self.config = self.resolve_config()

    scoped = ctx.intercept('svc', {'timeout': 30})
    await scoped.plugin(Svc)
    assert ctx.svc.config == {'timeout': 30}
    assert ctx._intercepts == []


@pytest.mark.anyio
async def test_accessor_and_mixin(ctx):
    ctx.accessor('answer', lambda accessing: 42)
    assert ctx.answer == 42

    class Tools(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'tools')

        def register(self, name):
            return f'registered:{name}'

    await ctx.plugin(Tools)
    ctx.mixin('tools', ['register'])
    assert ctx.register('x') == 'registered:x'


@pytest.mark.anyio
async def test_service_binding_binds_registration_to_caller(ctx):
    """通过 ctx.service.method() 调用时，服务内的 self.ctx 是调用方上下文。"""

    class MyRegistry(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'myRegistry')
            self.items = []

        def register(self, item):
            self.items.append(item)
            self.ctx.cleanup(lambda: self.items.remove(item))
            return item

    provider = ctx.plugin(MyRegistry)
    await provider

    cleaned = []

    def consumer(ctx):
        ctx.myRegistry.register('a')
        ctx.cleanup(lambda: cleaned.append('consumer'))

    fiber = ctx.inject(['myRegistry'], consumer)
    await fiber
    assert ctx.myRegistry.items == ['a']

    await fiber.dispose()
    assert ctx.myRegistry.items == []
    assert cleaned == ['consumer']

    await provider.dispose()
    assert ctx.get('myRegistry') is None


@pytest.mark.anyio
async def test_service_check_predicate(ctx):
    """服务可声明 check() 谓词：不可用时依赖方保持 PENDING。"""

    ready = {'value': False}
    captured = {}

    class Gated(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'gated')

        def check(self):
            return ready['value']

    provider = ctx.plugin(Gated)
    await provider

    fired = []

    def consumer(ctx):
        fired.append(True)

    consumer.inject = ['gated']
    fiber = ctx.plugin(consumer)
    await fiber
    # check() 未通过：依赖方保持 PENDING，注入未就绪的访问会得到明确错误
    assert fiber.state.name == 'PENDING'
    assert fired == []
    with pytest.raises(CordisError) as info:
        fiber.ctx.gated
    assert 'inactive context' in str(info.value)

    ready['value'] = True
    await provider.restart()
    await fiber
    assert fired == [True]
    assert fiber.state.name == 'ACTIVE'
