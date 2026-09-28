# 02 · 生命周期与 effect：让"卸载"成为一等公民

> 配套代码：`python tutorial/ch02_lifecycle.py`（约 290 行）

## fiber：一个插件实例的运行时句柄

`ctx.plugin(plugin)` 返回的不是插件本身，而是一个 **fiber**——
"这个插件这一次加载"的运行时句柄。

```text
PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED
               ↘ FAILED
```

| 状态 | 含义 | 你会遇到的场景 |
|------|------|----------------|
| `PENDING` | 已声明，但 `inject` 的依赖还没就绪 | "我的插件怎么没输出？" ← 十有八九是它 |
| `LOADING` | 插件主体正在执行 | 断点/日志落在这里 |
| `ACTIVE` | 已加载完成，服务可用 | 正常状态 |
| `FAILED` | 插件主体或配置校验抛错 | 错误**不会被静默吞掉** |
| `UNLOADING` | 清理函数正在运行 | 此时禁止再注册 |
| `DISPOSED` | 已移除，不能复活 | `fiber.uid is None` |

```python
fiber = ctx.plugin(demo)
print(fiber.state)      # PENDING（还没轮到它执行）
await fiber             # 等它稳定；启动失败会在这里抛出
print(fiber.state)      # ACTIVE
await fiber.dispose()   # 卸载；等待所有异步清理完成
```

`await fiber` 是重要的约定：**加载是异步的，而错误必须能在调用点被看到**。

## effect：三种写法，一个语义

"注册即副作用"的实际载体是 `ctx.effect()`。它接受三种形态：

```python
# ① 直接登记清理函数（最直白）
ctx.cleanup(lambda: conn.close())

# ② 立即执行 setup，返回 disposer（对应 TS 的 ctx.effect(() => () => ...)）
ctx.effect(lambda: (start_timer(), lambda: stop_timer())[1])

# ③ 上下文管理器（Python 最自然的 setup/teardown）
@contextmanager
def timer(interval):
    handle = start(interval)
    try:
        yield
    finally:
        stop(handle)

ctx.effect(lambda: timer(0.5))
```

三种写法背后是同一条规则：

> **effect 主体立即执行（setup），它产生的 disposer 被收集，
> 在"句柄被调用"或"fiber 卸载"时按逆序释放（teardown），先到先得、幂等。**

```python
order = []
def plugin(ctx):
    ctx.cleanup(lambda: order.append('first'))
    ctx.cleanup(lambda: order.append('second'))

fiber = ctx.plugin(plugin)
await fiber
await fiber.dispose()
assert order == ['second', 'first']      # 逆序
```

## 递归卸载：子插件是父插件的一个 effect

这是 Cordis 里最优雅的一处设计。`ctx.plugin(child)` 在内部做的事是：

```python
# 摘自上文的 Fiber.__init__
self._handle = parent.effect(lambda: lambda: self._dispose(), label='ctx.plugin()')
```

也就是说，**"卸载子插件"被登记成了父插件的一个 effect**。于是：

```python
def parent(ctx):
    ctx.plugin(child)          # 子插件
    ctx.cleanup(lambda: log('parent cleanup'))

await parent_fiber.dispose()
# 输出：parent cleanup → child cleanup
# 父插件自己的 effect 先按逆序释放，随后才轮到子插件的卸载 effect
```

不需要写"遍历子节点"的代码，卸载天然是递归的、有序的。

## 失败隔离：一个插件坏了，不该拖垮整个应用

```python
def broken(ctx):
    raise RuntimeError('apply 爆炸了')

fiber = ctx.plugin(broken)
try:
    await fiber
except RuntimeError as error:
    print(error)              # apply 爆炸了
print(fiber.state)            # FAILED
```

要点：

- 错误发生在 `_reload()` 里，被记录到 `fiber._error`，**由 `await fiber` 抛出**；
- fiber 进入 `FAILED`，它的 effect 会被完整回收（不会泄漏半个插件）；
- 其他插件不受影响（装载器逐条启动、逐条报告，见第 5 章）。

> dsh 的教程特意指出：插件加载失败会**明确报错**，不会静默跳过。
> 静默跳过是线上最难查的一类问题。

## 异步清理：Python 与 TS 的关键差异

TS 里 `async` 函数体在执行到第一个 `await` 之前是**同步运行**的；
Python 的协程在被 await 之前**一行都不执行**。这个差异会直接影响清理函数的写法：

```python
# ❌ 直觉写法：整个函数都是 async → dispose() 调用后什么都不会发生
async def uninstall():
    services.pop(name)          # ← 这一行要等到被 await 才执行
    await wait_dependents()

# ✅ 框架采用的写法："同步前缀 + 异步尾部"
def uninstall():
    services.pop(name)          # 立即生效（调用方马上看到服务消失）
    return wait_dependents()    # 返回可等待对象，交给框架去等待
```

因此本框架的 disposer 语义是：

```python
disposer()          # 触发释放；同步部分立刻执行，返回 awaitable（或 None）
await disposer      # 触发并等待异步部分完成
```

框架内部（`Fiber._unload`、`Disposer._start`）会先按逆序**启动**所有清理函数，
再并发等待它们返回的 awaitable：

```text
清理按注册的逆序启动；返回 awaitable 的部分并发等待。
```

如果你在写自己的插件，只要遵守"同步前缀 + 异步尾部"，行为就和 TS 版一致。

## 动手练习

1. 在 `tutorial/ch02_lifecycle.py` 里给插件加一个定时器 effect，
   观察卸载时定时器确实被取消（没有多余输出）。
2. 把 `ctx.cleanup` 换成 `async def` 清理函数（`await asyncio.sleep(0.05)`），
   用 `await fiber.dispose()` 验证卸载会等待它完成。
3. 故意在清理函数里抛异常，观察框架记录日志但不打断其余清理。

## 小结

- fiber 是插件实例的句柄，状态机让"插件为什么没跑"变得可诊断；
- effect 是唯一的注册原语，三个写法一个语义：立即 setup、逆序 teardown、幂等；
- 子插件是父插件的 effect，所以卸载天生递归；
- 失败会进入 `FAILED` 并从 `await fiber` 抛出，绝不静默。

下一章：[服务与依赖注入](03-services.md) —— 让插件之间只认名字。
