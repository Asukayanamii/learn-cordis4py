"""第 1 章 · 上下文与插件：一切的起点。

本章只做一件事：把"插件"和"上下文"这两个最基础的概念用 Python 写出来。

- **上下文（Context）** = 服务与资源的容器，用 ``extend()`` 派生作用域；
- **插件** = 接受 ``ctx`` 的函数（或类），通过 ``ctx`` 注册自己的贡献；
- **注册是可逆副作用**：``ctx.cleanup(fn)`` 登记清理函数，
  插件卸载时按逆序执行 —— 本章先用 print 演示，第 2 章把它做实。

对照仓库里的完整实现：``cordis/context.py``、``cordis/registry.py``。

运行：python tutorial/ch01_context.py
"""

from __future__ import annotations


class Context:
    """最小上下文：保存父子关系、元数据与清理函数的账本。"""

    def __init__(self, parent: 'Context | None' = None, **meta):
        self.parent = parent
        self.meta = dict(meta)          # 本作用域上的元数据（如 fiber、agent）
        self.cleanups: list = []        # 本作用域的清理函数账本

    # ---------------------------------------------------------------- 作用域
    def extend(self, **meta) -> 'Context':
        """派生一个子上下文：继承父级，附带自己的元数据（父级不受影响）。"""
        child = Context(self, **meta)
        child.__dict__.update(meta)      # 元数据同时作为属性，可顺着作用域链读取
        return child

    def __getattr__(self, name):
        """属性找不到时，依次向上级作用域查找（这就是"作用域链"）。"""
        if name.startswith('_'):
            raise AttributeError(name)
        parent = self.__dict__.get('parent')
        if parent is not None and hasattr(parent, name):
            return getattr(parent, name)
        raise AttributeError(name)

    # ---------------------------------------------------------------- 生命周期
    def cleanup(self, disposer, label: str = 'cleanup'):
        """登记一个清理函数；返回可撤销句柄（调用它可提前撤销）。"""
        entry = {'label': label, 'fn': disposer, 'done': False}

        def undo():
            if entry['done']:
                return False
            entry['done'] = True
            if entry in self.cleanups:
                self.cleanups.remove(entry)
            entry['fn']()
            return True

        self.cleanups.append(entry)
        return undo

    def effect(self, execute, label: str = 'effect'):
        """运行 ``execute``（setup），把它返回的清理函数登记下来。"""
        result = execute()
        if callable(result):
            return self.cleanup(result, label)
        return None

    def dispose(self):
        """卸载本作用域：按注册的逆序执行全部清理函数。"""
        while self.cleanups:
            entry = self.cleanups.pop()     # 后注册的先清理
            if entry['done']:
                continue
            entry['done'] = True
            entry['fn']()

    # ---------------------------------------------------------------- 插件
    def plugin(self, plugin, config=None) -> 'PluginHandle':
        """挂载一个插件，返回句柄（第 2 章它会变成带状态机的 Fiber）。"""
        # 插件可以有 name / inject / Config 元数据（本章只用到 name）
        name = getattr(plugin, 'name', None) or getattr(plugin, '__name__', 'anonymous')
        child = self.extend(fiber=name)     # 插件运行在自己的子作用域里
        handle = PluginHandle(name, child)
        child.meta['handle'] = handle
        print(f'  [plugin] {name} 加载')
        callback = plugin.apply if hasattr(plugin, 'apply') else plugin
        callback(child)                     # 调用插件主体，传入它的上下文
        return handle


class PluginHandle:
    """插件句柄：本章只有名字与上下文，第 2 章会补上状态机。"""

    def __init__(self, name: str, ctx: Context):
        self.name = name
        self.ctx = ctx

    def dispose(self):
        self.ctx.dispose()

    def __repr__(self):
        return f'<Plugin {self.name}>'


# ---------------------------------------------------------------------- 演示
if __name__ == '__main__':
    print('--- 1) 挂载一个最简单的插件 ---')

    def hello(ctx):
        print('  hello 插件收到上下文，meta =', ctx.meta)
        ctx.cleanup(lambda: print('  hello 清理'), label='hello')

    ctx = Context()
    handle = ctx.plugin(hello)
    ctx.plugin(hello)          # 同一个插件可以挂载多次，互不影响

    print('--- 2) 卸载其中一个 ---')
    handle.dispose()

    print('--- 3) 作用域链：子上下文能读到父级的元数据 ---')
    captured = {}

    def capture(ctx):
        captured['ctx'] = ctx       # 把插件自己的上下文留出来

    ctx.plugin(capture)
    inner = captured['ctx'].extend(agent='demo')
    print('  inner.meta =', inner.meta)
    print('  inner.fiber 顺着作用域链读到 =', inner.fiber)
    print('  inner.agent（自己的元数据） =', inner.agent)

    print('--- 4) 主动撤销一个注册 ---')
    undo = ctx.cleanup(lambda: print('  这条清理已执行'))
    undo()          # 立即撤销
    undo()          # 幂等：第二次什么都不做
    ctx.dispose()   # 卸载根作用域

    print('\n本章完。下一章：让 fiber 拥有状态机，并让清理真正"逆序"生效。')
