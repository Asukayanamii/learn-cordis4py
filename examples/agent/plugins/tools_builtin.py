"""内置工具集：文件读取/写入、目录列举、命令执行。

演示 ``@ctx.tools.tool(...)`` 装饰器注册，以及"危险工具"如何交给策略插件把关
（``dangerous=True`` 只是标注，真正的把关在 approval 插件里——能力与策略分离）。
"""

import os
import subprocess

inject = ['tools']


def apply(ctx, config=None):
    workspace = os.path.abspath((config or {}).get('workspace') or os.getcwd())
    ctx.logger.info('内置工具的工作目录: %s', workspace)

    def resolve(path):
        full = path if os.path.isabs(path) else os.path.join(workspace, path)
        return os.path.abspath(full)

    @ctx.tools.tool('read_file', '读取工作区内一个文本文件的内容', {
        'type': 'object',
        'properties': {
            'path': {'type': 'string', 'description': '相对工作区的文件路径'},
            'max_bytes': {'type': 'integer', 'description': '最多读取的字节数', 'default': 20000},
        },
        'required': ['path'],
    })
    async def read_file(ctx, path, max_bytes=20000):
        full = resolve(path)
        if not os.path.isfile(full):
            raise FileNotFoundError(f'文件不存在: {path}')
        with open(full, 'r', encoding='utf-8', errors='replace') as stream:
            data = stream.read(int(max_bytes))
        return data

    @ctx.tools.tool('write_file', '把内容写入工作区内的文件（覆盖）', {
        'type': 'object',
        'properties': {
            'path': {'type': 'string', 'description': '相对工作区的文件路径'},
            'content': {'type': 'string', 'description': '要写入的完整内容'},
        },
        'required': ['path', 'content'],
    }, dangerous=True)
    async def write_file(ctx, path, content):
        full = resolve(path)
        os.makedirs(os.path.dirname(full) or '.', exist_ok=True)
        with open(full, 'w', encoding='utf-8') as stream:
            stream.write(content)
        return f'已写入 {len(content)} 字符到 {path}'

    @ctx.tools.tool('list_dir', '列出目录内容', {
        'type': 'object',
        'properties': {'path': {'type': 'string', 'description': '目录路径', 'default': '.'}},
    })
    async def list_dir(ctx, path='.'):
        full = resolve(path)
        if not os.path.isdir(full):
            raise NotADirectoryError(f'目录不存在: {path}')
        entries = sorted(os.listdir(full))
        lines = []
        for entry in entries[:200]:
            mark = '/' if os.path.isdir(os.path.join(full, entry)) else ''
            lines.append(entry + mark)
        return '\n'.join(lines) or '(空目录)'

    @ctx.tools.tool('shell', '在工作目录中执行一条 shell 命令', {
        'type': 'object',
        'properties': {
            'command': {'type': 'string', 'description': '要执行的命令'},
            'timeout': {'type': 'integer', 'description': '超时秒数', 'default': 30},
        },
        'required': ['command'],
    }, dangerous=True)
    async def shell(ctx, command, timeout=30):
        completed = subprocess.run(
            command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=float(timeout),
        )
        output = (completed.stdout or '') + (completed.stderr or '')
        return f'exit={completed.returncode}\n{output[:8000]}'
