# 04 · 事件与策略：为什么拦截要用 waterfall

> 配套代码：`python tutorial/ch04_events.py`（约 530 行）

## 事件是"横向"的扩展点

服务解决"我要用某个能力"，事件解决"我要在某件事发生时插一脚"。
发事件的人不需要知道谁在听——这正是智能体核心需要的形状：

```text
模型请求发出前   → llm/request        （可以换模型、裁剪上下文、加缓存标记）
工具执行前       → tools/pre-execute  （可以改写参数、直接否决）
工具执行后       → tools/post-execute （可以脱敏、截断、审计）
每一步开始前     → agent/pre-step     （可以拒绝/改写用户输入）
轮次要结束时     → agent/turn-stopping（可以让循环继续或停下）
```

把改造点设计成事件，意味着"给工具加审批"不需要改工具，也不需要改 agent 循环。

## 五种分发模式

| 模式 | 调用 | 是否 await | 语义 |
|------|------|-----------|------|
| `emit` | `ctx.emit(name, ...)` | 否 | 同步广播；不等待、不收集返回值 |
| `parallel` | `await ctx.parallel(name, ...)` | 是 | 所有监听器并发运行，一起等待 |
| `serial` | `await ctx.serial(name, ...)` | 是 | 依次 await，**首个非假返回值胜出** |
| `bail` | `ctx.bail(name, ...)` | 否 | `serial` 的同步版本 |
| `waterfall` | `ctx.waterfall(name, ...,next)` | 看监听器 | 环绕中间件；不调用 `next()` 即短路 |

选择哪种模式，是事件**公开约定的一部分**——换个模式就可能改变监听器能否返回值、
并发与否、能否短路。

```python
# emit：广播（观察者）
ctx.emit('tool/call', call)

# serial：投票（谁先给出有效答案就听谁的）
decision = await ctx.serial('approval/request', request)

# parallel：并发（所有监听器都得跑完）
await ctx.parallel('telemetry/flush')
```

> **`emit` 的监听器必须是同步函数。** 如果你写成 `async def`，它返回的协程没有人 await
> （Python 会给出 `RuntimeWarning: coroutine was never awaited`）。需要异步就用
> `parallel`/`serial` 分发的事件。这是第 07 章"坑位清单"的第一条。

## waterfall：拦截与策略的正确形状

`waterfall` 把监听器串成一条"洋葱"：

```text
监听器 1  →  监听器 2  →  ...  →  最内层默认实现
   ↑ 可以包装下游返回值            ↑ 调用 next() 才会走到这里
   ↑ 不调用 next() 直接返回 = 否决整条链
```

定义一个"可被拦截的决策"：

```python
async def default_accept(message, next_):
    return {'content': message['content']}

decision = await ctx.waterfall('agent/pre-step', {'content': text}, default_accept)
```

策略插件（审批/改写/审计）：

```python
# 改写：修改决策对象后继续委托
async def rewrite(message, next_):
    message['content'] = message['content'].strip()
    return await next_()

# 否决：不调用 next()，直接返回结果
async def policy(call, next_):
    if call['name'] in DENY:
        return {'ok': False, 'error': '策略拒绝了该工具'}
    return await next_()

# 包装：调用 next() 之后加工返回值
async def redact(call, result, next_):
    result = await next_()
    result['content'] = SECRET.sub('***', result['content'])
    return result
```

**两条纪律**（dsh 文档里作为"常设规则"强调过）：

1. **只观察或标注的监听器必须调用 `next()`**——忘了调用 = 静默吞掉下游和默认行为，
   这是最难查的一类 bug；
2. **拥有决策权的监听器可以不调用 `next()`**——直接返回就是有意短路。

### 一个小陷阱：next 可以返回非 awaitable

```python
async def listener(x, next):
    return await next()        # ← 如果最终解析到同步的默认实现就会炸
```

所以本框架的约定是：**在异步应用里，waterfall 的默认实现写成 `async def`**；
不确定时用 `cordis.utils.maybe_await(next_())` 兜底。

## 监听器即 effect

```python
def plugin(ctx):
    ctx.on('tool/call', handler)     # 注册绑定到本插件的 fiber
# plugin 卸载 → handler 自动移除
```

同一个事件可以有多个插件监听，各自独立；谁卸载都不影响别人——
这就是"插件"能自由组合的又一层保障。

## `internal/*`：框架自己的事件

除了业务事件，框架还广播一批内省事件，用来写"元插件"（装载器、HMR、诊断工具都靠它）：

```python
ctx.on('internal/status', lambda fiber, old: print(f'{fiber.name}: {old.name} -> {fiber.state.name}'))
ctx.on('internal/plugin', lambda fiber: print('插件发布/回收', fiber))
ctx.on('internal/dispatch', lambda mode, name, args, this_ctx: ...)   # 所有事件分发都会经过
ctx.on('internal/service', lambda name, value: ...)                    # 服务注册/注销
```

内部事件还有两个被用作扩展点的 waterfall：

| 事件 | 作用 |
|------|------|
| `internal/config` | 插件加载前改写原始配置 |
| `internal/update` | fiber.update 时拦截/替换重启（装载器借此把新配置写回 YAML） |

## 动手练习

1. 给 `tutorial/ch04_events.py` 加一个 `tools/post-execute` 监听器，
   把工具结果里的手机号打码（regex 即可）。
2. 写一个"限流"监听器：同一工具一分钟内调用超过 2 次就直接否决（不调用 `next()`）。
3. 用 `serial` 实现"多策略投票"：任一策略返回"拒绝"即拒绝，
   任何策略都没意见时返回 None。

## 小结

- 事件的模式是它的公开约定：观察用 `emit`，投票用 `serial`/`bail`，拦截用 `waterfall`；
- waterfall 的短路语义正是"策略/审批"需要的形状，但只观察的监听器必须 `next()`；
- 监听器随插件卸载自动移除，**插件之间通过事件名耦合，而不是通过 import**；
- `internal/*` 事件让"元能力"（装载、诊断、热重载）也能写成插件。

下一章：[配置与装载器](05-config-and-loader.md) —— 让应用变成一份可 patch 的配置。
