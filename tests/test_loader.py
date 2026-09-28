"""Loader（cordis.yml 插件树）测试。"""

import os
import textwrap

import pytest

from cordis import Context
from cordis.loader import Loader, LoaderError, start_app

pytestmark = pytest.mark.anyio


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding='utf-8')
    return path


@pytest.fixture
def app(tmp_path):
    """一个最小应用：greeter 服务 + consumer 插件。"""

    write(tmp_path / 'plugins' / 'greeter.py', '''
        from cordis import Service

        class Greeter(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, 'greeter')
                self.greeting = (config or {}).get('greeting', 'Hello')

            def greet(self, who):
                return f'{self.greeting}, {who}!'

        plugin = Greeter
    ''')
    write(tmp_path / 'plugins' / 'consumer.py', '''
        inject = ['greeter']

        def apply(ctx):
            ctx.greeter.greet('world')
    ''')
    write(tmp_path / 'cordis.yml', '''
        - id: greeter
          name: ./plugins/greeter.py
          config:
            greeting: Hello

        - id: consumer
          name: ./plugins/consumer.py
    ''')
    return tmp_path


async def test_loads_plugin_tree(app):
    ctx = await start_app(str(app / 'cordis.yml'))
    status = {item['id']: item['state'] for item in ctx.loader.status()}
    assert status == {'greeter': 'active', 'consumer': 'active'}
    assert ctx.greeter.greeting == 'Hello'


async def test_plugin_receives_config(app):
    ctx = await start_app(str(app / 'cordis.yml'))
    assert ctx.get('greeter').greet('x') == 'Hello, x!'


async def test_patch_replaces_config_and_inserts(app):
    write(app / 'patch.yml', '''
        - id: greeter
          config:
            greeting: 你好

        - insert:
            - id: extra
              name: ./plugins/extra.py
    ''')
    write(app / 'plugins' / 'extra.py', '''
        def apply(ctx):
            ctx.provide('extra', 'loaded')
    ''')
    ctx = await start_app(str(app / 'cordis.yml'), patches=[str(app / 'patch.yml')])
    assert ctx.greeter.greeting == '你好'
    assert ctx.get('extra') == 'loaded'


async def test_patch_disable_and_remove(app):
    write(app / 'patch.yml', '''
        - id: consumer
          disabled: true
    ''')
    ctx = await start_app(str(app / 'cordis.yml'), patches=[str(app / 'patch.yml')])
    status = {item['id']: item['state'] for item in ctx.loader.status()}
    assert status['consumer'] == 'disabled'
    assert status['greeter'] == 'active'


async def test_unknown_patch_target_fails(app):
    write(app / 'patch.yml', '''
        - id: nope
          config: {}
    ''')
    with pytest.raises(LoaderError):
        await start_app(str(app / 'cordis.yml'), patches=[str(app / 'patch.yml')])


async def test_group_entries(app):
    write(app / 'group.yml', '''
        - id: core
          group: true
          config:
            - id: greeter
              name: ./plugins/greeter.py
            - id: consumer
              name: ./plugins/consumer.py
    ''')
    ctx = await start_app(str(app / 'group.yml'))
    status = ctx.loader.status()
    assert status[0]['id'] == 'core'
    assert [child['state'] for child in status[0]['children']] == ['active', 'active']


async def test_broken_plugin_does_not_break_others(app):
    write(app / 'cordis.yml', '''
        - id: broken
          name: ./plugins/broken.py
        - id: greeter
          name: ./plugins/greeter.py
    ''')
    write(app / 'plugins' / 'broken.py', '''
        def apply(ctx):
            raise RuntimeError('boom')
    ''')
    ctx = await start_app(str(app / 'cordis.yml'))
    status = {item['id']: item['state'] for item in ctx.loader.status()}
    assert status['broken'] == 'failed'
    assert status['greeter'] == 'active'


async def test_missing_module_reported(app):
    write(app / 'cordis.yml', '''
        - id: ghost
          name: ./plugins/ghost.py
    ''')
    ctx = await start_app(str(app / 'cordis.yml'))
    assert ctx.loader.status()[0]['state'] == 'failed'


async def test_hot_reload_entry(app):
    ctx = await start_app(str(app / 'cordis.yml'))
    assert ctx.greeter.greeting == 'Hello'

    write(app / 'plugins' / 'greeter.py', '''
        from cordis import Service

        class Greeter(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, 'greeter')
                self.greeting = 'Reloaded'

        plugin = Greeter
    ''')
    await ctx.loader.reload_entry('greeter')
    assert ctx.greeter.greeting == 'Reloaded'


async def test_reload_config_rebuilds_tree(app):
    ctx = await start_app(str(app / 'cordis.yml'))
    assert {item['id'] for item in ctx.loader.status()} == {'greeter', 'consumer'}

    write(app / 'cordis.yml', '''
        - id: greeter
          name: ./plugins/greeter.py
    ''')
    await ctx.loader.reload_config()
    assert {item['id'] for item in ctx.loader.status()} == {'greeter'}


async def test_config_path_interpolation(app):
    """配置里的 ${baseDir} / ${cwd} 会展开为绝对路径。"""
    write(app / 'paths.yml', '''
        - id: probe
          name: ./plugins/probe.py
          config:
            base: ${baseDir}
            cwd: ${cwd}
    ''')
    write(app / 'plugins' / 'probe.py', '''
        captured = {}

        def apply(ctx, config):
            captured.update(config)
            ctx.provide('probe', config)
    ''')
    ctx = await start_app(str(app / 'paths.yml'))
    captured = ctx.get('probe')
    assert captured['base'] == str(app)
    assert os.path.isabs(captured['cwd'])


async def test_dump_config(app):
    ctx = await start_app(str(app / 'cordis.yml'))
    text = ctx.loader.dump_config()
    assert 'greeter' in text and 'consumer' in text


async def test_unloading_loader_disposes_tree(app):
    ctx = await start_app(str(app / 'cordis.yml'))
    assert ctx.get('greeter') is not None
    await ctx.loader.dispose()
    assert ctx.get('greeter') is None
