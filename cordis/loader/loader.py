"""Loader：``cordis.yml`` 驱动的插件树。

启动器只需要做三件事：创建根 :class:`~cordis.context.Context`、挂载
``Loader`` 插件、调用 ``cordis.loader.run_app``。**有哪些插件、如何配置**
全部来自 YAML 文件——这正是 dsh "应用 = 一棵插件树 + 若干叠加层" 的最小形态。

用法::

    from cordis import Context
    from cordis.loader import run_app

    ctx = run_app()                      # 读取当前目录的 cordis.yml
    ctx = run_app('app.yml', patches=['fix.yml'])

叠加层（patch）语义与 dsh 一致：

.. code-block:: yaml

    - insert:                     # 插入新条目（可指定 parent: <group-id>）
        - id: fs-local
          name: ./plugins/fs.py
    - id: llm                     # 按 id 定位，替换其整个 config
      config: { model: deepseek-chat }
    - id: noisy                   # 禁用某个条目
      disabled: true
    - id: legacy                  # 移除条目
      remove: true
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
import types
from typing import Any, Optional

from ..registry import Inject, resolve_plugin
from ..schema import Schema
from ..service import Service
from ..utils import get_running_loop

from .entry import Entry

__all__ = ['Loader', 'LoaderError', 'EntryTree', 'run_app']


class LoaderError(Exception):
    """配置项/叠加层非法时抛出。"""


def _read_yaml(path: str) -> Any:
    try:
        import yaml
    except ImportError as error:  # pragma: no cover - 环境缺依赖
        raise LoaderError('加载 YAML 配置需要 PyYAML：pip install pyyaml') from error
    if not os.path.exists(path):
        raise LoaderError(f'config file not found: {path}')
    with open(path, 'r', encoding='utf-8') as stream:
        return yaml.safe_load(stream)


def _looks_like_path(name: str) -> bool:
    return name.startswith('.') or name.endswith('.py') or '/' in name or '\\' in name


def interpolate(value: Any, base_dir: str) -> Any:
    """把配置字符串里的 ``${baseDir}`` / ``${cwd}`` 展开为绝对路径。

    配置文件里的相对路径不应该随"从哪个目录启动进程"而变，
    因此路径相关的配置推荐写成 ``${baseDir}/sessions``。
    """
    if isinstance(value, str):
        if '${' not in value:
            return value
        return value.replace('${baseDir}', base_dir).replace('${cwd}', os.getcwd())
    if isinstance(value, list):
        return [interpolate(item, base_dir) for item in value]
    if isinstance(value, dict):
        return {key: interpolate(item, base_dir) for key, item in value.items()}
    return value


def _split_attr(name: str) -> tuple[str, Optional[str]]:
    """把 ``./plugins/x.py:apply`` 拆成路径与属性名（兼容 Windows 盘符）。"""
    if ':' not in name:
        return name, None
    head, _, tail = name.rpartition(':')
    if not tail or not tail.isidentifier():
        return name, None
    if '/' in tail or '\\' in tail:
        return name, None
    return head, tail


class EntryTree:
    """条目树：负责模块导入、条目构建与 patch 应用。"""

    def __init__(self, loader: 'Loader', ctx: Any, base_dir: str) -> None:
        self.loader = loader
        self.ctx = ctx
        self.base_dir = base_dir
        self.store: dict[str, Entry] = {}
        self._counter = 0
        self._import_counter = 0

    # ================================================================ 条目构建
    def next_id(self) -> str:
        self._counter += 1
        return f'entry-{self._counter}'

    def normalize(self, raw: Any, parent: Optional[Entry] = None) -> dict:
        """把 YAML 原始条目规范化为 ``{id, name, config, disabled, group, inject}``。"""
        if isinstance(raw, list):
            return {'id': self.next_id(), 'group': True, 'config': raw}
        if not isinstance(raw, dict):
            raise LoaderError(f'invalid entry: {raw!r}')
        options = dict(raw)
        options.setdefault('id', self.next_id())
        if options.get('group'):
            options['config'] = list(options.get('config') or [])
        elif 'name' not in options:
            raise LoaderError(f'entry {options["id"]!r} requires a "name"')
        if 'config' in options:
            options['config'] = interpolate(options['config'], self.base_dir)
        return options

    def build(self, raw: Any, parent: Optional[Entry] = None) -> Entry:
        options = self.normalize(raw, parent)
        if options['id'] in self.store:
            raise LoaderError(f'duplicate entry id: {options["id"]}')
        entry = Entry(self, options, parent)
        self.store[options['id']] = entry
        return entry

    def register(self, entry: Entry) -> None:
        self.store[entry.id] = entry
        for child in entry.children:
            self.register(child)

    def reset(self) -> None:
        self.store.clear()

    def resolve(self, entry_id: str) -> Entry:
        entry = self.store.get(entry_id)
        if entry is None:
            raise LoaderError(f'cannot resolve entry {entry_id!r}')
        return entry

    # ================================================================ patch
    def apply_patches(self, entries: list[Any], ops: list[Any]) -> list[Any]:
        """按 dsh 语义应用叠加层：insert / 按 id 更新 / remove。"""
        result = list(entries)
        for op in ops or []:
            if not isinstance(op, dict):
                raise LoaderError(f'invalid patch operation: {op!r}')

            if 'insert' in op:
                target = result
                parent_id = op.get('parent') or op.get('id')
                if parent_id is not None:
                    target = self._find_group_list(result, parent_id)
                insert_at = op.get('position')
                items = op['insert'] if isinstance(op['insert'], list) else [op['insert']]
                if insert_at is None:
                    target.extend(items)
                else:
                    target[int(insert_at):int(insert_at)] = items
                continue

            entry_id = op.get('id')
            if entry_id is None:
                raise LoaderError(f'patch operation requires "id" or "insert": {op!r}')
            found = self._find_entry(result, entry_id)
            if found is None:
                raise LoaderError(f'patch targets unknown entry {entry_id!r}')
            holder, index = found
            if op.get('remove'):
                del holder[index]
                continue
            raw = holder[index]
            if isinstance(raw, list):
                raise LoaderError(f'cannot patch anonymous group {entry_id!r}')
            updated = dict(raw)
            for key in ('name', 'config', 'disabled', 'inject', 'group'):
                if key in op:
                    updated[key] = op[key]
            if 'config' not in op:
                updated['config'] = raw.get('config')
            holder[index] = updated
        return result

    def _find_entry(self, entries: list[Any], entry_id: str):
        for index, raw in enumerate(entries):
            if isinstance(raw, dict):
                raw_id = raw.get('id')
                if raw_id == entry_id:
                    return entries, index
                if isinstance(raw.get('config'), list):
                    found = self._find_entry(raw['config'], entry_id)
                    if found is not None:
                        return found
            elif isinstance(raw, list):
                found = self._find_entry(raw, entry_id)
                if found is not None:
                    return found
        return None

    def _find_group_list(self, entries: list[Any], group_id: str) -> list[Any]:
        for raw in entries:
            if isinstance(raw, dict):
                if raw.get('id') == group_id:
                    if not isinstance(raw.get('config'), list):
                        raise LoaderError(f'entry {group_id!r} is not a group')
                    return raw['config']
                if isinstance(raw.get('config'), list):
                    try:
                        return self._find_group_list(raw['config'], group_id)
                    except LoaderError as error:
                        if 'is not a group' in str(error):
                            raise
            elif isinstance(raw, list):
                try:
                    return self._find_group_list(raw, group_id)
                except LoaderError as error:
                    if 'is not a group' in str(error):
                        raise
        raise LoaderError(f'cannot resolve group {group_id!r}')

    # ================================================================ 模块导入
    def import_plugin(self, name: Optional[str]) -> tuple[Any, Optional[str]]:
        """导入模块并取出插件对象；返回 ``(plugin, file_path)``。"""
        if not name:
            raise LoaderError('entry requires a "name" (plugin module specifier)')
        module_name, attr = _split_attr(name)
        if _looks_like_path(module_name):
            path = module_name if os.path.isabs(module_name) else os.path.join(self.base_dir, module_name)
            path = os.path.abspath(path)
            module = self._import_file(path)
            display = os.path.splitext(os.path.basename(path))[0]
            return self._extract(module, attr, path, display), path
        module = importlib.import_module(module_name)
        return self._extract(module, attr, module_name, module_name), getattr(module, '__file__', None)

    def _import_file(self, path: str) -> types.ModuleType:
        if not os.path.exists(path):
            raise LoaderError(f'plugin module not found: {path}')
        self._import_counter += 1
        unique = f'cordis_plugin_{abs(hash(path)) & 0xffffff:x}_{self._import_counter}'
        spec = importlib.util.spec_from_file_location(unique, path)
        if spec is None or spec.loader is None:
            raise LoaderError(f'cannot load module: {path}')
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(unique, None)
            raise
        return module

    def reload_module(self, name: str) -> Any:
        """重新导入模块（用于热重载）。

        文件模块每次 ``import_plugin`` 都是全新导入，无需额外处理；
        包模块需要 ``importlib.reload``。
        """
        module_name, attr = _split_attr(name)
        if _looks_like_path(module_name):
            return None
        module = importlib.reload(importlib.import_module(module_name))
        return self._extract(module, attr, module_name, module_name)

    def _extract(self, module: types.ModuleType, attr: Optional[str], label: str, display: str) -> Any:
        if attr:
            plugin = getattr(module, attr, None)
            if plugin is None:
                raise LoaderError(f'module {label!r} has no attribute {attr!r}')
            return plugin

        plugin = getattr(module, 'plugin', None)
        if plugin is None:
            plugin = getattr(module, 'apply', None)
            if not callable(plugin):
                raise LoaderError(f'module {label!r} exports neither "plugin" nor "apply"')

        # 模块级元数据（name / inject / Config）对两种导出形式都生效
        module_name = getattr(module, 'name', None)
        module_inject = getattr(module, 'inject', None)
        module_config = getattr(module, 'Config', None)
        if not (module_name or module_inject or module_config):
            return plugin

        def pick(key: str, fallback: Any) -> Any:
            own = getattr(plugin, key, None)
            if own in (None, {}):
                return fallback
            return own

        return {
            'apply': plugin,
            'name': module_name or getattr(plugin, '__name__', None) or display,
            'inject': pick('inject', module_inject),
            'Config': pick('Config', module_config),
        }

    def wrap_plugin(self, plugin: Any, options: dict) -> Any:
        """把条目级 ``inject`` 合并进插件声明。"""
        inject = options.get('inject')
        if not inject:
            return plugin
        resolved = resolve_plugin(plugin)
        if resolved is None:
            raise LoaderError(f'invalid plugin in entry {options.get("id")!r}')
        merged = Inject.resolve(getattr(resolved.identity, 'inject', None))
        for service, config in Inject.resolve(inject).items():
            merged[service] = config
        return {
            'apply': resolved.callback,
            'name': options.get('name') or resolved.name,
            'inject': merged,
            'Config': resolved.Config,
        }


class Loader(Service):
    """插件树服务（``ctx.loader``）。"""

    class Config(Schema):
        path = Schema.string().default('cordis.yml').description('主配置文件')
        patches = Schema.array(Schema.string()).default([]).description('叠加层文件（按序应用）')
        watch = Schema.boolean().default(False).description('轮询监视插件文件并热重载')
        interval = Schema.number().default(1.0).description('热重载轮询间隔（秒）')
        base_dir = Schema.string().description('相对路径的解析基准（默认为配置文件所在目录）')

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, 'loader')
        self.options = dict(config or {})
        self.log = ctx.logger('loader')
        self.entries: list[Entry] = []
        self.tree: Optional[EntryTree] = None
        self.path: Optional[str] = None
        self.watch_handle: Any = None
        self._watch_files: dict[str, float] = {}

    # ================================================================ 启动
    async def init(self) -> None:
        path = self.options.get('path') or 'cordis.yml'
        self.path = os.path.abspath(path)
        base_dir = self.options.get('base_dir') or os.path.dirname(self.path) or os.getcwd()
        self.tree = EntryTree(self, self.ctx, os.path.abspath(base_dir))
        await self.reload_config()
        if self.options.get('watch'):
            self._start_watch()

    def _load_ops(self) -> tuple[list[Any], list[Any]]:
        data = []
        if os.path.exists(self.path):
            data = _read_yaml(self.path) or []
        elif self.options.get('patches'):
            self.log.debug('no base config at %s; applying patches only', self.path)
        else:
            raise LoaderError(f'config file not found: {self.path}')
        if not isinstance(data, list):
            raise LoaderError(f'config file must be a list of entries: {self.path}')

        ops: list[Any] = []
        for patch in self.options.get('patches') or []:
            patch_path = patch if os.path.isabs(patch) else os.path.join(self.tree.base_dir, patch)
            loaded = _read_yaml(patch_path) or []
            if not isinstance(loaded, list):
                raise LoaderError(f'patch file must be a list of operations: {patch_path}')
            ops.extend(loaded)
        return data, ops

    async def reload_config(self) -> None:
        """重新读取配置文件与叠加层，重建整棵插件树。"""
        data, ops = self._load_ops()
        entries = self.tree.apply_patches(data, ops)

        for entry in self.entries:
            await entry.stop()
        self.tree.reset()
        self.entries = [self.tree.build(raw) for raw in entries]
        for entry in self.entries:
            await entry.start(self)
        self.log.info('plugin tree ready: %d entries', len(self.entries))
        if self.options.get('watch'):
            self._scan_files()

    # ================================================================ 单条热重载
    async def reload_entry(self, entry_id: str) -> None:
        entry = self.tree.resolve(entry_id)
        await entry.reload(self)
        self.log.info('reloaded entry %C', entry_id)
        self._scan_files()

    async def reload(self, entry_id: Optional[str] = None) -> None:
        """重新加载：指定 id 时只重载该条目，否则重建整棵树。"""
        if entry_id is None:
            await self.reload_config()
            return
        entry = self.tree.resolve(entry_id)
        if entry.options.get('name') and not entry.options.get('group'):
            plugin, file = self.tree.import_plugin(entry.options['name'])
            entry.file = file
        await entry.reload(self)

    # ================================================================ 热重载
    def _start_watch(self) -> None:
        interval = float(self.options.get('interval') or 1.0)
        if get_running_loop() is None:
            self.log.warn('watch 需要异步入口（asyncio），已忽略：请使用 cordis.loader.start_app')
            return

        def install() -> Any:
            task = asyncio.create_task(self._watch_loop(interval))

            def dispose() -> None:
                task.cancel()

            return dispose

        self.watch_handle = self.ctx.effect(install, 'loader.watch()')
        self._scan_files()

    def _scan_files(self) -> None:
        self._watch_files = {}
        for entry in self.entries:
            for path in entry.file_candidates():
                try:
                    self._watch_files[path] = os.path.getmtime(path)
                except OSError:
                    continue

    async def _watch_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            for entry in list(self.entries):
                changed = False
                for path in entry.file_candidates():
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        continue
                    if self._watch_files.get(path) not in (None, mtime):
                        changed = True
                    self._watch_files[path] = mtime
                if changed:
                    self.log.info('detected change, reloading %C', entry.id)
                    try:
                        await self.reload_entry(entry.id)
                    except Exception as error:  # pragma: no cover - 重载失败不应杀掉 watcher
                        self.log.error(error)

    # ================================================================ 诊断
    async def dispose(self) -> None:
        """卸载 Loader：整棵插件树随之卸载。"""
        result = self._service_ctx.fiber.dispose()
        if result is not None:
            await result

    def report_error(self, entry: Entry, error: Exception) -> None:
        self.log.error('entry %s failed: %s', entry.id, error)

    def dump_config(self) -> str:
        """导出当前生效的插件树（对应 dsh 的 ``--dump-config``）。"""
        import yaml

        def render(entry: Entry) -> Any:
            if entry.children:
                return {'id': entry.id, 'group': True, 'config': [render(child) for child in entry.children]}
            item: dict[str, Any] = {'id': entry.id, 'name': entry.options.get('name')}
            if entry.options.get('config') is not None:
                item['config'] = entry.options['config']
            if entry.options.get('disabled'):
                item['disabled'] = True
            if entry.options.get('inject'):
                item['inject'] = entry.options['inject']
            return item

        return yaml.safe_dump([render(entry) for entry in self.entries], allow_unicode=True, sort_keys=False)

    def status(self) -> list[dict]:
        def describe(entry: Entry) -> dict:
            info = {'id': entry.id, 'state': entry.state, 'name': entry.options.get('name')}
            if entry.children:
                info['children'] = [describe(child) for child in entry.children]
            if entry.error is not None:
                info['error'] = str(entry.error)
            return info

        return [describe(entry) for entry in self.entries]


def run_app(
    path: str = 'cordis.yml',
    patches: Optional[list[str]] = None,
    watch: bool = False,
    config: Optional[dict] = None,
) -> Any:
    """创建根上下文并挂载 Loader（同步入口，适合脚本与 ``python -m``）。

    返回根 ``Context``；可用 ``ctx.loader`` 访问插件树。
    """
    from ..context import Context

    ctx = Context()
    options = dict(config or {})
    options.setdefault('path', path)
    if patches:
        options.setdefault('patches', list(patches))
    if watch:
        options.setdefault('watch', True)
    ctx.plugin(Loader, options)
    return ctx
