"""第 6 章 · Loader：用 YAML 把插件组合成"应用"。

前面几章我们都是"用代码挂插件"。真实产品（包括 dsh）里，**应用是一份配置**：
``cordis.yml`` 列出有哪些插件、各自的配置与依赖；叠加层（patch）按 id 打补丁；
装载器把这一切变成运行中的插件树。

本章实现（自包含，约 200 行）：

- 解析 ``cordis.yml``（条目：id / name / config / inject / disabled / group）；
- 模块解析：``./plugins/x.py``（相对路径）、``包.模块``、``./x.py:attr``；
- **patch 叠加层**：按 id 替换 config、禁用条目、insert 新条目（与 dsh 语义一致）；
- **热重载**：文件变化后重新导入模块并重挂该条目；
- 装载器本身也是插件：``ctx.loader``，卸载它 = 卸载整棵树。

对照仓库里的完整实现：``cordis/loader/``（loader.py + entry.py）、
``examples/agent/cordis.yml``（一个真实的插件树配置）。

运行：python tutorial/ch06_loader.py
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
import textwrap

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


# ============================================================== 条目的运行时表示
class Entry:
    def __init__(self, options, app):
        self.options = options
        self.app = app
        self.id = options['id']
        self.plugin = None
        self.file = None
        self.state = 'inactive'      # inactive | active | disabled | failed
        self.error = None
        self.children: list['Entry'] = []
        self.cleanups: list = []

    @property
    def disabled(self):
        return bool(self.options.get('disabled'))

    def start(self):
        if self.disabled:
            self.state = 'disabled'
            return
        if self.options.get('group'):
            self.children = [Entry(opts, self.app) for opts in self.options.get('config') or []]
            for child in self.children:
                child.start()
            self.state = 'active'
            return
        try:
            self.plugin, self.file = load_plugin(self.options['name'], self.app.base_dir)
            self.state = 'active'
            print(f'  [loader] {self.id} 已加载 ← {self.options["name"]}')
        except Exception as error:
            self.state = 'failed'
            self.error = error
            print(f'  [loader] {self.id} 加载失败: {error}')

    def stop(self):
        for child in self.children:
            child.stop()
        while self.cleanups:
            self.cleanups.pop()()
        self.state = 'inactive'
        self.plugin = None

    def reload(self):
        """重新导入模块并重启该条目（热重载）。"""
        self.stop()
        self.start()


# ============================================================== 模块解析与导入
def split_attr(name):
    """``./plugins/x.py:apply`` → (路径, 'apply')；兼容 Windows 盘符。"""
    if ':' not in name:
        return name, None
    head, _, tail = name.rpartition(':')
    return (head, tail) if tail.isidentifier() else (name, None)


def load_plugin(name, base_dir):
    """导入模块并取出插件对象；返回 (plugin, 文件路径)。"""
    module_name, attr = split_attr(name)
    is_path = module_name.startswith('.') or module_name.endswith('.py') or '/' in module_name

    if is_path:
        path = os.path.abspath(os.path.join(base_dir, module_name))
        if not os.path.exists(path):
            raise FileNotFoundError(f'插件模块不存在: {path}')
        # 每次导入都用唯一的模块名 → 文件改动后重新导入即可拿到新代码（热重载的关键）
        unique = f'mini_plugin_{abs(hash(path)) & 0xffffff:x}_{load_plugin.counter}'
        load_plugin.counter += 1
        spec = importlib.util.spec_from_file_location(unique, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_name)
        path = getattr(module, '__file__', None)

    if attr:
        return getattr(module, attr), path
    if hasattr(module, 'plugin'):
        return module.plugin, path
    if callable(getattr(module, 'apply', None)):
        return {'apply': module.apply, 'name': getattr(module, 'name', None) or module_name}, path
    raise AttributeError(f'模块 {name} 既没有导出 plugin，也没有导出 apply')


load_plugin.counter = 0


# ============================================================== 配置与 patch
def read_yaml(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f'配置文件不存在: {path}')
    with open(path, 'r', encoding='utf-8') as stream:
        return yaml.safe_load(stream) or []


def apply_patches(entries, ops):
    """按 dsh 语义应用叠加层：insert / 按 id 更新 / remove。"""
    entries = list(entries)
    counter = [0]

    def ensure_id(raw):
        if isinstance(raw, list):
            return {'id': f'anon-{counter[0]}', 'group': True, 'config': raw}
        raw = dict(raw)
        counter[0] += 1
        raw.setdefault('id', f'entry-{counter[0]}')
        return raw

    entries = [ensure_id(raw) for raw in entries]

    def find(target_list, entry_id):
        for index, raw in enumerate(target_list):
            if raw.get('id') == entry_id:
                return target_list, index
            children = raw.get('config')
            if raw.get('group') and isinstance(children, list):
                found = find(children, entry_id)
                if found:
                    return found
        return None

    for op in ops or []:
        if 'insert' in op:
            items = op['insert'] if isinstance(op['insert'], list) else [op['insert']]
            entries.extend(ensure_id(item) for item in items)
            continue
        found = find(entries, op.get('id'))
        if found is None:
            raise KeyError(f'patch 指向了不存在的条目: {op.get("id")!r}')
        holder, index = found
        if op.get('remove'):
            del holder[index]
            continue
        updated = dict(holder[index])
        for key in ('name', 'config', 'disabled', 'inject'):
            if key in op:
                updated[key] = op[key]
        holder[index] = updated
    return entries


# ============================================================== 装载器（本身就是"插件"）
class Loader:
    """一个应用组合根：读取配置 → 建树 → 启动/重载/诊断。"""

    def __init__(self, path='cordis.yml', patches=()):
        self.path = os.path.abspath(path)
        self.base_dir = os.path.dirname(self.path) or os.getcwd()
        self.patch_files = list(patches)
        self.entries: list[Entry] = []
        self.store: dict[str, Entry] = {}
        self.loaded_at = {}

    # ---------------------------------------------------------------- 装载
    def load(self):
        data = read_yaml(self.path)
        ops = []
        for patch in self.patch_files:
            patch_path = patch if os.path.isabs(patch) else os.path.join(self.base_dir, patch)
            ops.extend(read_yaml(patch_path))
        entries = apply_patches(data, ops)
        self.entries = [Entry(options, self) for options in entries]
        for entry in self.entries:
            entry.start()
        self._index(self.entries)
        self._snapshot()
        return self

    def _index(self, entries):
        self.store = {}
        self._index_into(entries)

    def _index_into(self, entries):
        for entry in entries:
            self.store[entry.id] = entry
            self._index_into(entry.children)

    def _snapshot(self):
        """记录文件时间戳，供热重载轮询。"""
        self.loaded_at = {}
        for entry in self.store.values():
            if entry.file:
                self.loaded_at[entry.file] = os.path.getmtime(entry.file)

    # ---------------------------------------------------------------- 诊断
    def status(self):
        def describe(entry):
            info = {'id': entry.id, 'state': entry.state, 'name': entry.options.get('name')}
            if entry.children:
                info['children'] = [describe(child) for child in entry.children]
            if entry.error:
                info['error'] = str(entry.error)
            return info
        return [describe(entry) for entry in self.entries]

    def dump(self):
        """导出生效后的配置树（dsh 的 --dump-config 就是这个意思）。"""
        def render(entry):
            if entry.children:
                return {'id': entry.id, 'group': True, 'config': [render(c) for c in entry.children]}
            item = {'id': entry.id, 'name': entry.options.get('name')}
            if entry.options.get('config') is not None:
                item['config'] = entry.options['config']
            if entry.disabled:
                item['disabled'] = True
            return item
        return yaml.safe_dump([render(entry) for entry in self.entries], allow_unicode=True, sort_keys=False)

    # ---------------------------------------------------------------- 热重载
    def changed(self):
        """返回文件已变化的条目 id 列表（简化版：轮询 mtime）。"""
        result = []
        for entry in self.store.values():
            if not entry.file:
                continue
            try:
                mtime = os.path.getmtime(entry.file)
            except OSError:
                continue
            if self.loaded_at.get(entry.file) != mtime:
                result.append(entry.id)
        return result

    def reload(self, entry_id):
        entry = self.store[entry_id]
        entry.reload()
        self._snapshot()
        print(f'  [loader] 已热重载 {entry_id}')

    def dispose(self):
        for entry in self.entries:
            entry.stop()
        print('  [loader] 插件树已卸载')


# ---------------------------------------------------------------------- 演示
def make_demo_app(root):
    """在临时目录里生成一个小应用：配置 + 两个插件 + 一个 patch。"""
    os.makedirs(os.path.join(root, 'plugins'), exist_ok=True)
    with open(os.path.join(root, 'plugins', 'greeter.py'), 'w', encoding='utf-8') as stream:
        stream.write(textwrap.dedent('''
            name = 'greeter'

            def apply(ctx, config=None):
                greeting = (config or {}).get('greeting', 'Hello')
                print(f'  [greeter] 启动，greeting={greeting!r}')
                return lambda: print('  [greeter] 清理')
        '''))
    with open(os.path.join(root, 'plugins', 'reporter.py'), 'w', encoding='utf-8') as stream:
        stream.write(textwrap.dedent('''
            name = 'reporter'

            def apply(ctx):
                print('  [reporter] 启动（它不依赖 greeter，但加载顺序无关）')
        '''))
    with open(os.path.join(root, 'cordis.yml'), 'w', encoding='utf-8') as stream:
        stream.write(textwrap.dedent('''
            - id: greeter
              name: ./plugins/greeter.py
              config:
                greeting: Hello

            - id: reporter
              name: ./plugins/reporter.py

            - id: core-demo
              group: true
              config:
                - id: inner
                  name: ./plugins/reporter.py
        '''))
    with open(os.path.join(root, 'fix.patch.yml'), 'w', encoding='utf-8') as stream:
        stream.write(textwrap.dedent('''
            - id: greeter
              config:
                greeting: 你好

            - id: inner
              disabled: true

            - insert:
                - id: extra
                  name: ./plugins/reporter.py
        '''))


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as root:
        make_demo_app(root)
        config_path = os.path.join(root, 'cordis.yml')
        patch_path = os.path.join(root, 'fix.patch.yml')

        print('--- 1) 直接装载 cordis.yml ---')
        loader = Loader(config_path).load()
        for info in loader.status():
            print('   ', info)

        print('--- 2) 应用 patch：改配置、禁用条目、插入新条目 ---')
        loader = Loader(config_path, patches=[patch_path]).load()
        for info in loader.status():
            print('   ', info)
        print('   dump 生效后的树:')
        for line in loader.dump().splitlines():
            print('     ', line)

        print('--- 3) 热重载：改插件文件 → 重新导入 → 新代码生效 ---')
        plugin_path = os.path.join(root, 'plugins', 'greeter.py')
        with open(plugin_path, 'w', encoding='utf-8') as stream:
            stream.write(textwrap.dedent('''
                name = 'greeter'

                def apply(ctx, config=None):
                    print('  [greeter] 我是改动后的新版本！')
                    return lambda: print('  [greeter] 新版本清理')
            '''))
        print('   检测到变化:', loader.changed())
        loader.reload('greeter')

        print('--- 4) 坏模块只影响自己：其他条目照常工作 ---')
        with open(os.path.join(root, 'bad.yml'), 'w', encoding='utf-8') as stream:
            stream.write('- id: broken\n  name: ./plugins/ghost.py\n- id: ok\n  name: ./plugins/reporter.py\n')
        loader2 = Loader(os.path.join(root, 'bad.yml')).load()
        for info in loader2.status():
            print('   ', info)

        print('--- 5) 卸载整棵树 ---')
        loader.dispose()

    print('\n本章完。下一章：用这些机制搭一个真正的智能体核心。')
