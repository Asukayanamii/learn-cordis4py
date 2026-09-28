# 01 · 为什么需要 Cordis：从"智能体核心"的难题说起

> 配套代码：`python tutorial/ch01_context.py`（约 130 行）

## 先看一个真实的痛点

你要写一个智能体核心。第一版通常长这样：

```python
class Agent:
    def __init__(self):
        self.tools = ToolsModule()
        self.llm = OpenAIClient()
        self.sessions = SessionStore('./sessions')

    async def run(self, task):
        ...
```

跑得挺好。然后需求来了：

- 要把 OpenAI 换成公司内网的模型 → 改 `Agent.__init__`；
- 危险工具需要人工审批 → 在 `ToolsModule.execute` 里插 `if`；
- 不同会话要用不同的工具集 → `ToolsModule` 长出 `if session_id in ...`；
- 要在不重启的情况下换模型 / 改策略 / 加载新工具 → 做不到；
- 想给每次工具调用加审计日志 → 又改一遍 `ToolsModule`。

**问题不在代码质量，在架构形状**：所有能力被"写死"在一个对象里，
彼此直接 import、直接调用，导致任何一处变化都要改核心。

Cordis 的答案是：把"能力"和"能力的组合方式"分开。

> **每个能力都是插件；插件只描述自己的贡献；应用 = 一棵插件树。**

## 五个原语：一次说清

Cordis 全篇只有五个概念（dsh 的文档也是这样开篇的）：

| 原语 | 解决的问题 | 一句话 |
|------|-----------|--------|
| **插件 Plugin** | 谁在贡献能力？ | 一个接受 `ctx` 的函数/类；描述贡献，不关心别人 |
| **上下文 Context** | 能力放在哪里？ | 服务与资源的容器；`extend()` 派生作用域 |
| **服务 Service / 注入 inject** | 能力怎么被找到？ | 按**名字**共享；`inject` 声明依赖，未就绪就 PENDING |
| **事件 Events** | 不相关的插件怎么协作？ | 广播/投票/拦截；发事件的人不需要知道谁在听 |
| **作用 Effect** | 贡献怎么撤销？ | 一切注册都是可逆副作用，卸载时逆序释放 |

论文里的说法是**时空可组合性**（spatiotemporal composability）：

- **空间**维度：把能力拆成插件，通过上下文组合（谁和谁在一起）；
- **时间**维度：插件的加载/卸载/替换发生在运行期间，且**可逆**（什么时候在、什么时候走）。

"时空可组合"落到工程上就是两句话：

```python
ctx.plugin(ModelAdapter)      # 空间：能力由插件提供，通过名字被消费
await adapter_fiber.dispose()  # 时间：卸载即撤销它带来的一切
```

## 动手：最小上下文

第 1 章的代码只做三件事。第一，**上下文是一个带作用域链的容器**：

```python
class Context:
    def __init__(self, parent=None, **meta):
        self.parent = parent
        self.meta = dict(meta)     # 本作用域的元数据（fiber、agent……）
        self.cleanups = []         # 清理函数账本

    def extend(self, **meta) -> 'Context':
        """派生一个子上下文：继承父级，附带自己的元数据（父级不受影响）。"""
        return Context(self, **meta)

    def __getattr__(self, name):
        """属性找不到时，顺着作用域链向上找。"""
        ...
```

`extend()` 是 Cordis 里"空间"的基本操作。后面你会看到它的三种用法：

- `ctx.extend({'fiber': fiber})` —— 插件运行在自己的子上下文里；
- `ctx.isolate('llm')` —— 让 `llm` 这个名字在子树里解析到另一个实现（多租户/多会话）；
- `ctx.intercept('llm', config)` —— 给子树里的插件追加服务配置。

第二，**插件就是一个函数**，通过上下文注册贡献：

```python
def hello(ctx):
    print('hello, 我的第一个插件')
    ctx.cleanup(lambda: print('清理'))

ctx = Context()
ctx.plugin(hello)
```

第三，**注册是可逆副作用**——`ctx.cleanup(fn)` 记在账本上，插件卸载时**逆序**执行：

```python
ctx.plugin(demo)          # demo 里注册了 A、B 两个清理
handle.dispose()          # 打印顺序是 B、A（后注册的先清理）
```

> 为什么强调"逆序"？因为后注册的东西往往依赖先注册的东西（先建连接、再建会话；
> 先装插件、再注册它的处理器）。逆序释放才是安全的默认值——这也是所有资源管理
> 框架（如 `contextlib.ExitStack`）的共同选择。

## 完整实现里多了什么

第 1 章的 `Context` 只有 60 行；成品 `cordis/context.py` 在同样的骨架上补了：

| 能力 | 为什么需要 |
|------|-----------|
| `__setattr__` 守卫 | 插件上下文里**必须先 `provide` 才能赋值**，防止状态躲在属性里 |
| `isolate()` / `intercept()` | 多租户、多会话、按作用域改写配置 |
| `get/set/provide/accessor/mixin` | 服务表与计算属性；`mixin` 让 `ctx.on` 这类门面存在 |
| `root` / `baseUrl` | 应用级共享引用与模块解析基准 |
| `Context.is_()` | 跨副本的类型判断（对应 TS 版的全局 symbol 品牌） |

## 常见坑（第 1 章）

1. **`extend()` 不是"复制整个上下文"**，而是"派生一层作用域"——
   父级后来的变化子级能看到（服务表在根上），子级的元数据不会污染父级。
2. **不要在插件顶层直接 import 别的插件实现**。要共享能力就 `@provide` 服务、
   要通知就用事件；直接 import 等于把两个插件粘死，替换其中一个就成了改代码。
3. **注册就要有撤销**。第 1 章的账本在第 2 章会变成真正的 fiber effect；
   从第一天起就用"注册即副作用"的心智写插件。

## 小结

- 智能体核心的组合性难题 → 用插件 + 上下文把能力与组合方式分离；
- 五个原语：插件、上下文、服务/注入、事件、作用；
- 记住两句话：**依赖关系决定加载顺序**、**一切注册都是可逆副作用**。

下一章：[生命周期与 effect](02-lifecycle.md) —— 把账本变成真正的 fiber 状态机。
