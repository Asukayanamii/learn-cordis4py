# learn-cordis · 用 Python 复刻 Cordis 插件框架，并搭一个智能体核心

这个仓库做三件事：

1. **`cordis/`** —— 把 [Cordis](https://github.com/cordiverse/cordis)（[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 底层的插件框架）用 Python **复刻**了一遍：
   上下文、服务依赖注入、类型化事件、fiber 生命周期、可逆副作用、配置校验、YAML 装载器——一个不少。
2. **`tutorial/` + `docs/tutorial/`** —— 一套**从零构建**的教程：每一章都是可直接运行的代码，
   从 60 行的"上下文 + 插件"一路长到完整的智能体核心，讲清每一步"为什么这样设计"。
3. **`examples/agent/`** —— 用这套框架实现的**配置驱动智能体核心**：工具流水线、策略审批、
   会话日志、agent 循环、可替换模型适配器、CLI。默认用 mock 模型，**不需要 API Key 就能跑通完整回合**。

> 框架设计参考：Cordis 论文 *A Programming Paradigm for Spatiotemporal Composability*
> ([arXiv:2608.25512](https://arxiv.org/abs/2608.25512))；概念术语与
> [DeepSeek Harness 文档](https://deepseek-harness.github.io/deepseek-harness/)保持一致。

---

## 60 秒上手

```bash
git clone <this-repo> && cd learn-cordis
python -m pip install -e .          # 运行时只依赖 PyYAML
python -m pytest -q                 # 75 个测试

python examples/01_hello.py                     # 第一个插件
python examples/04_events.py                    # 五种事件分发模式
python examples/06_loader/run.py --dump         # YAML 组装插件树 + patch
python examples/agent/run.py "读取 pyproject.toml 并总结"   # 智能体跑一个完整回合
python examples/agent/run.py                    # 交互式 REPL
```

智能体示例的输出（mock 模型 → 工具调用 → 汇总，全程确定性）：

```text
$ python examples/agent/run.py "读取 pyproject.toml 并总结"
agent> 任务「读取 pyproject.toml 并总结」已完成。我调用了 1 次工具，结果如下：
- 第 1 条工具结果：[build-system] requires = ["setuptools>=68"] ...

$ python examples/agent/run.py "运行 echo hello"       # shell 是危险工具
agent> ... - 第 1 条工具结果：shell 是危险工具，当前策略未放行      # 策略插件否决了它

$ python examples/agent/run.py --patch "运行 echo hello"          # 叠加层放行危险工具
agent> ... - 第 1 条工具结果：exit=0 hello
```

---

## 五个核心概念

| 概念 | 一句话 | 代码 |
|------|--------|------|
| **插件（Plugin）** | 接受 `ctx` 的函数/类/对象；它描述自己的贡献 | `ctx.plugin(apply)` · `ctx.plugin(MyService)` |
| **上下文（Context）** | 服务与资源的容器，可派生作用域 | `ctx.tools` · `ctx.extend()` · `ctx.isolate('llm')` |
| **服务 / 注入（Service · inject）** | 能力按**名字**共享；依赖没就绪就 PENDING | `class S(Service)` · `inject = ['tools']` |
| **事件（Events）** | 五种分发：`emit` / `parallel` / `serial` / `bail` / `waterfall` | `ctx.on(...)` · `await ctx.waterfall(...)` |
| **作用（Effect）** | 一切注册都是可逆副作用，卸载时逆序撤销 | `ctx.cleanup(fn)` · `ctx.effect(cm)` |

```python
from cordis import Context, Service

class Greeter(Service):                       # ① 提供服务
    def __init__(self, ctx):
        super().__init__(ctx, 'greeter')

    def greet(self, who):
        return f'Hello, {who}!'

def consumer(ctx, config):                    # ② 消费服务（依赖就绪后才加载）
    ctx.logger.info(ctx.greeter.greet(config['who']))
    ctx.cleanup(lambda: ctx.logger.info('consumer 卸载'))   # ③ 注册即副作用

consumer.inject = ['greeter']

ctx = Context()
ctx.plugin(Greeter)
ctx.plugin(consumer, {'who': 'cordis'})
```

```text
[INFO ] greeter  Hello, cordis!
[INFO ] consumer consumer 卸载     ← 卸载插件时自动撤销
```

---

## 目录结构

```text
cordis/                  Python 版框架（本仓库的"成品"）
├── context.py           上下文：作用域派生、服务解析、事件/插件门面
├── fiber.py             fiber：生命周期状态机、effect 收集与逆序释放
├── events.py            事件总线：五种分发模式 + internal/* 事件
├── reflect.py           服务存储、属性声明、调用方绑定代理
├── registry.py          插件归一化（函数/类/对象/字典）与 fiber 启动
├── service.py           Service 基类（含拦截配置合并）
├── schema.py            配置校验（类声明式 + 函数式）
├── logger.py            日志服务（默认控制台出口，级别可用 CORDIS_LOG_LEVEL 控制）
└── loader/              cordis.yml 插件树：条目、patch 叠加层、热重载

tutorial/                从零构建教程（每章一个可直接运行的文件）
├── ch01_context.py      上下文与插件
├── ch02_lifecycle.py    fiber 状态机与 effect
├── ch03_services.py     服务与依赖注入
├── ch04_events.py       事件五种分发模式
├── ch05_config.py       配置与 Schema 校验
├── ch06_loader.py       YAML 装载器 + patch + 热重载
└── ch07_agent.py        用框架搭一个微型智能体核心

docs/tutorial/           教程正文（配套上面每一章）
examples/                示例：01–05 基础、06 装载器、agent 智能体核心
tests/                   75 个 pytest 用例（语义即规格）
```

---

## 从零到智能体：教程导航

1. [**为什么需要 Cordis**](docs/tutorial/01-why-and-context.md) —— 智能体的组合性难题，五个原语登场
2. [**生命周期与 effect**](docs/tutorial/02-lifecycle.md) —— 状态机、逆序释放、热重载的物理基础
3. [**服务与依赖注入**](docs/tutorial/03-services.md) —— 按名字共享能力、PENDING、隔离作用域
4. [**事件与策略**](docs/tutorial/04-events.md) —— waterfall 为什么是拦截与审批的正确形状
5. [**配置与装载器**](docs/tutorial/05-config-and-loader.md) —— 让"应用"变成一份可 patch 的配置
6. [**智能体核心设计**](docs/tutorial/06-agent-core.md) —— 工具流水线、会话日志、agent 循环、能力 seam
7. [**Python 适配与坑位清单**](docs/tutorial/07-python-notes.md) —— 与 TS 版逐条对照、常见坑、练习
8. [教程总览与学习路径](docs/tutorial/README.md)

---

## Python 版与 TypeScript 版的差异（要点）

| 主题 | TS 版（Cordis / dsh） | Python 版（本仓库） |
|------|----------------------|--------------------|
| 上下文属性 | `Proxy` 拦截 | `__getattr__` / `__setattr__` |
| 作用域继承 | 原型链 | `extend()` 复制内部引用 + `_own` 元数据 |
| 服务方法内的 `self.ctx` | 可追踪代理（traceable proxy） | `ServiceBindingProxy` + `contextvars`（效果一致） |
| "立即执行到第一个 await" | 原生 async 语义 | 清理函数写成"同步前缀 + 异步尾部"（`dispose()` 立即生效，`await dispose()` 等待完成） |
| 并发 | 事件循环 + Promise | `asyncio`（无事件循环时用临时循环同步驱动，脚本可直接用） |

细节与更多坑位见 [Python 适配要点](docs/tutorial/07-python-notes.md)。

---

## 许可

MIT。
