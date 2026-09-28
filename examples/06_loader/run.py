"""06 · Loader 示例：用 YAML 组装插件树。

运行：
    python examples/06_loader/run.py
    python examples/06_loader/run.py --patch     # 叠加 cordis.patch.yml
    python examples/06_loader/run.py --dump      # 打印生效后的配置树
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from cordis.loader import start_app  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


async def main():
    patches = ['cordis.patch.yml'] if '--patch' in sys.argv else []
    ctx = await start_app(
        path=os.path.join(HERE, 'cordis.yml'),
        patches=[os.path.join(HERE, name) for name in patches],
    )

    print('\n--- 条目状态 ---')
    for info in ctx.loader.status():
        print(f"  {info['id']:<10} {info['state']:<8} {info.get('name')}")

    if '--dump' in sys.argv:
        print('\n--- 生效后的配置树 ---')
        print(ctx.loader.dump_config())

    print('\n--- 服务可见性 ---')
    print('  ctx.get("greeter") ->', ctx.get('greeter'))
    print('  greeting           ->', ctx.greeter.greeting)

    # 插件树挂在 loader 插件之下：卸载 loader 会卸载整棵树
    print('\n--- 卸载 loader（整棵树一起卸载）---')
    await ctx.loader.dispose()
    print('  greeter 已随树卸载 ->', ctx.get('greeter'))


asyncio.run(main())
