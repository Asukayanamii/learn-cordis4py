"""Loader 子系统：``cordis.yml`` → 运行中的插件树。

.. code-block:: python

    import asyncio
    from cordis.loader import start_app, run_app

    # 方式一：异步入口（推荐，支持 watch / 后台任务）
    async def main():
        ctx = await start_app('cordis.yml', watch=True)
        await asyncio.Event().wait()

    # 方式二：同步入口（脚本内快速启动）
    ctx = run_app('cordis.yml')
    print(ctx.loader.dump_config())
"""

from .entry import Entry, EntryOptions
from .loader import EntryTree, Loader, LoaderError, run_app

__all__ = ['Loader', 'LoaderError', 'EntryTree', 'Entry', 'EntryOptions', 'run_app', 'start_app']


async def start_app(
    path: str = 'cordis.yml',
    patches: list[str] | None = None,
    watch: bool = False,
    config: dict | None = None,
):
    """异步入口：创建根上下文、挂载 Loader 并等待插件树就绪。"""
    from ..context import Context

    ctx = Context()
    options = dict(config or {})
    options.setdefault('path', path)
    if patches:
        options.setdefault('patches', list(patches))
    if watch:
        options.setdefault('watch', True)
    await ctx.plugin(Loader, options)
    return ctx
