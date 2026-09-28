# 07 · Python 适配要点、坑位清单与练习

> 本章不引入新代码，专门讲"从 TS 版 Cordis 到 Python 版"的每一处适配，
> 以及你在自己实现时会踩的坑。

## 一、逐条对照：TS 版怎么做的，Python 版怎么做

| 主题 | TS 版（Cordis / dsh） | Python 版（本仓库） | 为什么 |
|------|----------------------|--------------------|--------|
| 上下文属性读取 | `Proxy` 的 `get` 陷阱 + `internal/get` waterfall | `Context.__getattr__` + `ReflectService.resolve` | Python 没有 Proxy，属性缺失时才走 `__getattr__` |
| 上下文属性写入 | `Proxy` 的 `set` 陷阱（未 provide 即抛错） | `Context.__setattr__` + 白名单内部属性 | 同上；内部属性用 `_` 前缀与白名单区分 |
| 作用域派生 | 原型链（`Object.create`） | `extend()` 复制内部引用 + `_own` 元数据字典 | Python 没有原型链，用字典模拟"子级遮蔽父级" |
| 隔离 / 拦截 | `isolate` / `intercept` 映射的原型链 | `_isolate_own` 字典 + 根上的默认标签表 | 语义一致，实现更直白 |
| 服务方法里的 `this.ctx` | 可追踪代理（traceable proxy） | `ServiceBindingProxy` + `contextvars` | 目标一致：让服务内部的注册归属**调用方** fiber |
| effect 的返回值形态 | 函数 / Promise / 迭代器 / 生成器 | 函数 / awaitable / 可迭代 / **上下文管理器** | 上下文管理器是 Python 的 setup-teardown 惯用法 |
| `async` 函数的执行时机 | 同步执行到第一个 `await` | 被 await 才执行 | **最大的差异**：清理函数要写"同步前缀 + 异步尾部" |
| 插件形态 | 函数 / 类 / `{apply}` | 函数 / 类 / 带 `apply` 的对象 / 带 `apply` 的字典 | YAML 装载器需要字典形态 |
| 插件配置参数 | 多传一个参数没有代价 | 按签名判断是否传 `config` | Python 多传参数会 TypeError |
| 事件 `this` 参数 | `ctx.emit(thisArg, name, ...)` | 过滤函数显式传参（`emit_filtered`） | Python 没有隐式 `this` |
| 并发 | Promise / 微任务队列 | `asyncio`；无循环时用临时循环同步驱动 | 让脚本式示例也能直接 `ctx.plugin(...)` |
| 类型系统 | 声明合并（`declare module`）扩 `Context`/`Events` | 无（动态）；用文档与测试保证 | Python 没有编译期声明合并 |
| 模块热替换 | 动态 `import()` + 缓存击穿 | `importlib` 每次以唯一模块名重新加载文件 | 等价效果 |

### 关于 `contextvars` 的那一处，值得单独说

TS 版里 `ctx.tools.register(t)` 之所以能"归属调用方"，靠的是代理在调用瞬间创建
一个 shadow 上下文。Python 没有这种"调用瞬间劫持 `this`"的能力，替代方案是
**在调用期间设置一个 contextvar**：

```python
def _bind_callable(service, method, ctx):
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        token = current_caller.set(ctx)      # 调用期间：我是被谁调的
        try:
            result = method(*args, **kwargs)
        finally:
            current_caller.reset(token)
        if is_awaitable(result):
            return _await_with_caller(result, ctx)   # 协程里也要带上
        return result
    return wrapper
```

三个注意点：

1. **异步方法要单独包一层**：`async def` 的方法体在 await 时才执行，
   调用点的 `set` 早已 reset；
2. **`contextvars` 会随任务传播**：服务内部 `create_task(...)` 创建的后台任务会继承
   调用方的上下文，这通常正是你想要的（工作归属于发起它的插件）；
3. **因此服务自己读依赖要用 `self.owner_ctx`**（见第 3 章）。

## 二、坑位清单（按踩到的概率排序）

### 1. `emit` 的监听器写成 `async def`

```python
ctx.on('tool/result', async_callback)   # ❌ RuntimeWarning: coroutine never awaited
```

`emit` 是同步广播，不收集返回值。要么写同步监听器，要么用 `parallel`/`serial`。

### 2. 忘了调用 `next()`（waterfall 里静默吞掉下游）

```python
async def observer(x, next):
    log(x)              # ❌ 忘了 return await next()
    return 'ok'         # 下游和默认实现全被吞掉
```

只观察的监听器**必须** `next()`；只有拥有决策权时才短路。

### 3. 在插件卸载后仍然触碰 `ctx`

```python
def plugin(ctx):
    handle = asyncio.ensure_future(loop_forever(ctx))   # ❌ 没有随插件卸载取消
```

后果是"卸载后还在跑的协程"——典型症状是日志里出现莫名报错。
正确做法：把后台任务放进 effect（`ctx.effect`/`ctx.cleanup`），或用
`ctx.fiber.state` 做守卫。

### 4. 异步清理写成 `async def`

```python
async def uninstall():        # ❌ 调用 disposer 后什么都不会发生（Python 协程惰性）
    services.pop(name)

def uninstall():              # ✅ 同步前缀立即生效，异步尾部返回 awaitable
    services.pop(name)
    return maybe_async_tail()
```

### 5. 服务名冲突

`events` / `logger` / `reflect` / `registry` / `fiber` / `root` 被框架占用；
同名服务**不会**覆盖，而是抛 `service "x" has been registered at <...>`。
给服务加前缀，例如 `myapp.storage`。

### 6. 把可变状态放在模块级

```python
cache = {}                     # ❌ 热重载后状态还在，但 listener 已被重建
def apply(ctx): ...
```

热重载 = 卸载 + 重新导入 + 重新挂载。跨重载的状态请放进服务或外部存储。

### 7. 用 `except Exception` 吞掉插件启动错误

```python
try:
    await ctx.plugin(Broken)
except Exception:
    pass                       # ❌ 你会得到"什么都没发生"的现场
```

fiber 的 `FAILED` 状态与 `await fiber` 的异常是诊断的主要线索，别丢。

### 8. 认为 `ctx.service` 就是服务本身

`ctx.service` 是**绑定到调用方的代理**：`isinstance` 成立、方法可用，
但 `type()` 是代理类型；需要原始对象用 `ctx.get('service')`。

### 9. 依赖循环

A 注入 b、B 注入 a → 两者都停在 PENDING（dsh 同样是这种死锁形态）。
用事件打破循环，或把公共部分抽成第三个服务。

### 10. 在 `apply` 顶层做重活

插件加载是"启动路径"：拉模型、扫盘、建连都会拖慢启动，且失败会让整个条目 FAILED。
把这些放进服务内部或 `agent` 首次使用时惰性初始化。

## 三、与 dsh 概念对照表

| 本仓库 | dsh / Cordis | 说明 |
|--------|--------------|------|
| `cordis/context.py` | `vendor/cordis/src/context.ts` | 上下文与作用域 |
| `cordis/fiber.py` | `vendor/cordis/src/fiber.ts` | 生命周期与 effect |
| `cordis/events.py` | `vendor/cordis/src/events.ts` | 五种分发模式 |
| `cordis/reflect.py` | `vendor/cordis/src/reflect.ts` | 服务存储与属性声明 |
| `cordis/registry.py` | `vendor/cordis/src/registry.ts` | 插件归一化与启动 |
| `cordis/service.py` | `vendor/cordis/src/service.ts` | Service 基类与拦截配置 |
| `cordis/loader/` | `@cordisjs/plugin-loader` | `cordis.yml` + patch + HMR |
| `examples/agent/plugins/tools.py` | `packages/core/tools` | 工具注册表与执行流水线 |
| `examples/agent/plugins/agent.py` | `packages/core/agent-loop` | 轮次/步骤循环 |
| `examples/agent/plugins/sessions.py` | `packages/core/session` | 仅追加会话日志 |
| `examples/agent/plugins/llm.py` | `packages/llm/llm` | 模型 seam |
| `examples/agent/plugins/approval.py` | `packages/.../approval` | 审批策略（挂在 `tools/pre-execute`） |
| `examples/agent/plugins/telemetry.py` | `telemetry` 子系统 | 只观察的遥测插件 |

想深入哪一块，就照着这张表去读 dsh 的对应子系统（dsh 文档的 reference 章节有
每个子系统的生成式 API 说明）。

## 四、练习清单（从易到难）

1. **给事件加一个新模式**：实现 `first-wins`（并发发出，取最先返回者的值）。
   提示：`asyncio.wait(..., return_when=FIRST_COMPLETED)`。
2. **给 loader 加 `--dump-config` 之外的诊断**：`loader.tree()` 打印插件树缩进，
   标注每个条目的服务提供情况（`reflect.store` 反查）。
3. **实现 `ctx.isolate` 的实战用法**：写一个"多租户"示例——
   同一个 `llm` 名字，两个会话各自解析到不同的 mock 适配器。
4. **给 agent 加"人工审批"**：`tools/pre-execute` 监听器遇到危险工具时，
   通过 `asyncio.Queue` 等待外部（CLI）输入 yes/no，再决定放行或否决。
5. **实现工具并行调用**：一轮里有多个工具调用时并发执行
   （提示：`asyncio.gather` + 保持会话日志事件有序）。
6. **实现会话压缩**：当 `derive_messages()` 超过 N 条时，注册一个
   `agent/request` 监听器把早期消息替换成摘要（并写入 `session/event`）。
7. **换掉自研框架**：把 `tutorial/ch07_agent.py` 改为使用真实 `cordis` 包
   （其实就是把 `from tutorial.…` 换成 `from cordis import …`），跑通后对比差异。

## 五、想继续深入时读什么

1. Cordis 论文 *A Programming Paradigm for Spatiotemporal Composability*
   （[arXiv:2608.25512](https://arxiv.org/abs/2608.25512)）——理解"时空可组合性"的理论框架；
2. [Cordis 入门](https://deepseek-harness.github.io/deepseek-harness/reference/cordis-primer)——
   五个核心概念与 waterfall 语义的权威表述；
3. [Cordis 教程](https://deepseek-harness.github.io/deepseek-harness/develop/cordis-tutorial/)——
   官方七章动手教程（TS 版），与本教程互为镜像；
4. [dsh 架构文档](https://deepseek-harness.github.io/deepseek-harness/reference/)——
   轮次流程、会话日志、能力 seam、新行为的归属位置表，本教程第 06 章即是对它的微缩。

## 结语

Cordis 的复杂度不在代码量（核心五个文件加起来约 2000 行），而在**约束自己**：

- 只用一个注册原语（effect）；
- 只用一种依赖表达（我声明我需要什么，而不是我去找谁）；
- 只用一条通信路径（事件）；
- 让"卸载"与"加载"同样彻底。

做到这四条，你就得到了一棵可以随时改配置、换实现、热重载的插件树——
而你的智能体核心，也从"一个会成长的上帝对象"变成了"一组可组合的能力"。
