# 从零构建你的 Python 版 Cordis：教程总览

这套教程面向**要为自己写一个智能体核心（agent harness）的工程师**。
读完之后你应该能做到三件事：

1. 说清"插件框架"到底解决了什么问题，以及它的五个原语各管什么；
2. 从头写出一个能用的迷你框架（约 600 行），并理解每一行为什么在那里；
3. 用这套框架搭出一个可替换模型、可插策略、可回放会话的智能体核心。

## 怎么用这套教程

| 你想要的 | 走这条线 |
|----------|----------|
| 先跑起来看看 | `python examples/01_hello.py` → `examples/agent/run.py "读取 pyproject.toml"` |
| 按顺序学原理 | 下面 01→07 章节；每章先跑 `tutorial/chNN_*.py`，再读正文 |
| 直接看成品 | `cordis/` 包 + `tests/`（测试就是语义规格）+ `examples/agent/` |
| 想抄架构 | [06-agent-core.md](06-agent-core.md) 的能力 seam 与轮次流程 |

每一章的配套代码都是**自包含、可直接运行**的：

```bash
python tutorial/ch01_context.py     # 上下文与插件
python tutorial/ch02_lifecycle.py   # fiber 状态机与 effect
python tutorial/ch03_services.py    # 服务与依赖注入
python tutorial/ch04_events.py      # 事件五种分发模式
python tutorial/ch05_config.py      # 配置与 Schema 校验
python tutorial/ch06_loader.py      # YAML 装载器 + patch + 热重载
python tutorial/ch07_agent.py       # 微型智能体核心（用真实框架）
python examples/agent/run.py "列一下目录"   # 完整智能体示例
```

## 章节地图

| 章 | 主题 | 你会亲手实现 | 对应成品代码 |
|----|------|--------------|--------------|
| 01 | [为什么需要 Cordis](01-why-and-context.md) | Context 的作用域、插件挂载、注册账本 | `cordis/context.py` `cordis/registry.py` |
| 02 | [生命周期与 effect](02-lifecycle.md) | fiber 状态机、逆序释放、递归卸载、失败隔离 | `cordis/fiber.py` |
| 03 | [服务与依赖注入](03-services.md) | provide/get、`inject`、PENDING→ACTIVE、提供方替换 | `cordis/reflect.py` `cordis/service.py` |
| 04 | [事件与策略](04-events.md) | 五种分发模式、waterfall 短路、监听器即 effect | `cordis/events.py` |
| 05 | [配置与装载器](05-config-and-loader.md) | Schema 校验、`cordis.yml`、patch 叠加层、热重载 | `cordis/schema.py` `cordis/loader/` |
| 06 | [智能体核心设计](06-agent-core.md) | 工具流水线、会话日志、agent 循环、能力 seam | `examples/agent/` |
| 07 | [Python 适配与坑位](07-python-notes.md) | 与 TS 版逐条对照、常见坑、练习清单 | — |

## 五个原语速查

```python
# ① 插件：一个接受 ctx 的函数（或 Service 子类、带 apply 的对象/字典）
def my_plugin(ctx, config): ...

# ② 上下文：服务与资源的容器；extend/isolate/intercept 派生作用域
ctx = Context()
child = ctx.extend(agent=agent_obj)

# ③ 服务与注入：按名字共享能力；依赖未就绪时插件保持 PENDING
class Tools(Service):
    def __init__(self, ctx):
        super().__init__(ctx, 'tools')
my_plugin.inject = ['tools']

# ④ 事件：五种分发模式；waterfall 是"拦截/策略"的形状
ctx.on('tools/pre-execute', policy)          # 不调用 next() 即否决
await ctx.waterfall('agent/request', req, default)

# ⑤ 作用（effect）：注册即可逆副作用，卸载时逆序撤销
ctx.cleanup(disposer)
ctx.effect(lambda: contextmanager_instance)
```

## 三个"反直觉但重要"的设计决定

1. **依赖关系决定加载顺序，而不是书写顺序。** `ctx.plugin()` 会立刻返回一个 fiber，
   它可能停在 PENDING 等待服务；服务出现的那一刻它才真正加载。这就是"配置替换"
   （换一个工具的提供方，所有消费方自动重启）能成立的原因。

2. **一切注册都绑定到"调用方 fiber"。** `ctx.on(...)`、`ctx.tools.register(...)`、
   `ctx.systemPrompt.section(...)` 注册的东西，随**调用方插件**卸载而消失。
   这条性质让"插件是自包含的"从口号变成机制（Python 里靠 `ServiceBindingProxy` +
   `contextvars` 实现，见第 07 章）。

3. **事件不只是通知，更是扩展点。** 观察用 `emit`，投票用 `serial`/`bail`，
   拦截与审批用 `waterfall`。把"模型请求"、"工具执行"、"轮次结束"设计成事件，
   就把改造点从"改源码"变成了"挂插件"。

## 前置知识

- Python 3.10+；会写 `async/await`；
- 用过装饰器、上下文管理器（`with`）、生成器；
- 不要求读过 TypeScript 版 Cordis——所有 TS 概念都会对照解释。
