"""智能体核心示例的启动器。

用法：
    python examples/agent/run.py "读取 README.md 并总结"
    python examples/agent/run.py                     # 交互式 REPL
    python examples/agent/run.py --patch "运行 python --version"   # 载入 patch（真实模型 + 放行危险工具）
    python examples/agent/run.py --dump-config       # 打印生效后的插件树
    python examples/agent/run.py --status            # 打印条目状态
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
try:  # Windows 控制台默认编码可能不是 UTF-8
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:  # pragma: no cover
    pass

from cordis.loader import start_app  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


async def main() -> None:
    argv = sys.argv[1:]
    flags = {item for item in argv if item.startswith('--')}
    words = [item for item in argv if not item.startswith('--')]

    patches = [os.path.join(HERE, 'cordis.patch.yml')] if '--patch' in flags else []
    ctx = await start_app(path=os.path.join(HERE, 'cordis.yml'), patches=patches)

    if '--dump-config' in flags:
        print(ctx.loader.dump_config())
        return
    if '--status' in flags:
        for info in ctx.loader.status():
            print(f"  {info['id']:<14} {info['state']:<9} {info.get('name')}")
        return

    try:
        if words:
            reply = await ctx.cli.run_once(' '.join(words))
            print(f'agent> {reply}')
        else:
            await ctx.cli.repl()
    finally:
        await ctx.loader.dispose()


if __name__ == '__main__':
    asyncio.run(main())
