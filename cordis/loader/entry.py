"""配置项（Entry）：cordis.yml 里的一个插件条目。

一个条目对应一条配置：

.. code-block:: yaml

    - id: llm                 # 可选；缺省自动生成
      name: ./plugins/llm.py  # 模块说明符（相对路径 / 包路径，可带 :attr）
      config: {...}           # 传给插件的配置
      inject: [tools]         # 追加依赖（可选）
      disabled: false         # 可选
      group: true             # 若为 true，config 是子条目列表

条目是"声明"；:class:`~cordis.loader.loader.Loader` 负责把它变成运行中的
插件（fiber），并在配置变化/文件变化时卸载重装。
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ['Entry', 'EntryOptions']


class EntryOptions(dict):
    """条目配置（dict 包装，便于诊断输出）。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    @property
    def id(self) -> str:
        return self['id']

    @property
    def name(self) -> Optional[str]:
        return self.get('name')

    @property
    def is_group(self) -> bool:
        return bool(self.get('group'))

    @property
    def disabled(self) -> bool:
        return bool(self.get('disabled'))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'EntryOptions(id={self.get("id")!r}, name={self.get("name")!r})'


class Entry:
    """一个配置项的运行时句柄。"""

    def __init__(self, tree: Any, options: dict, parent: Optional['Entry'] = None) -> None:
        self.tree = tree
        self.options = options
        self.parent = parent
        self.id: str = options['id']
        self.fiber: Any = None
        self.children: list['Entry'] = []
        self.state = 'inactive'  # inactive | disabled | pending | active | failed
        self.error: Any = None
        self.file: Optional[str] = None
        self.module_name: Optional[str] = None

    # ================================================================ 生命周期
    async def start(self, loader: Any) -> None:
        if self.options.get('disabled'):
            self.state = 'disabled'
            return
        if self.options.get('group'):
            raw = self.options.get('config') or []
            self.children = [self.tree.build(item, parent=self) for item in raw]
            for child in self.children:
                await child.start(loader)
            self.state = 'active'
            return

        try:
            plugin, self.file = self.tree.import_plugin(self.options.get('name'))
        except Exception as error:
            self.state = 'failed'
            self.error = error
            loader.report_error(self, error)
            return

        try:
            self.fiber = self.tree.ctx.plugin(self.tree.wrap_plugin(plugin, self.options), self.options.get('config'))
            await self.fiber
            self.state = 'active'
        except Exception as error:
            self.state = 'failed'
            self.error = error
            loader.report_error(self, error)

    async def stop(self) -> None:
        for child in self.children:
            await child.stop()
        self.children = []
        if self.fiber is not None:
            await maybe_dispose(self.fiber)
            self.fiber = None
        if self.state != 'failed':
            self.state = 'inactive'

    async def reload(self, loader: Any) -> None:
        await self.stop()
        await self.start(loader)

    # ================================================================ 展示
    def file_candidates(self) -> list[str]:
        """用于热重载监视的文件路径（普通插件条目返回模块文件）。"""
        files: list[str] = []
        if self.file:
            files.append(self.file)
        for child in self.children:
            files.extend(child.file_candidates())
        return files

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'<Entry {self.id} {self.state}>'


async def maybe_dispose(fiber: Any) -> None:
    import asyncio
    result = fiber.dispose()
    if asyncio.iscoroutine(result) or hasattr(result, '__await__'):
        await result
