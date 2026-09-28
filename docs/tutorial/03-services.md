# 03 · 服务与依赖注入：插件之间只认名字

> 配套代码：`python tutorial/ch03_services.py`（约 390 行）

## 服务：一个有名字的能力

```python
class Storage(Service):
    def __init__(self, ctx):
        super().__init__(ctx, 'storage')     # ← 注册到 ctx.storage
        self.data = {}

    def put(self, key, value): ...
```

- `super().__init__(ctx, name)` 立即完成注册，归属**当前 fiber**；
- 任何插件都可以通过 `ctx.storage` 消费它，**不需要 import 这个类**；
- 提供方卸载时服务自动注销，依赖方随之卸载。

`Service` 子类本身就是插件（类形态），所以 `ctx.plugin(Storage)` 会像挂载函数一样挂载它。

## inject：声明依赖，而不是手动编排启动顺序

```python
def consumer(ctx):
    ctx.storage.put('answer', 42)     # 依赖已就绪，直接用

consumer.inject = ['storage']
```

框架做的事：

```text
扫描 inject → 逐个检查服务是否"可用"（提供方 fiber 处于 ACTIVE）
  全部就绪 → 启动插件（LOADING → ACTIVE）
  有缺失   → 保持 PENDING，什么都不做
```

这带来三个直接好处：

1. **加载顺序不用你操心。** `cordis.yml` 里的条目顺序无关紧要（装载器甚至并发启动）。
2. **替换提供方是零成本的。** 卸载 `OpenAIAdapter`、挂载 `DeepSeekAdapter`，
   所有 `inject = ['llm']` 的插件自动卸载并重新加载——这就是"配置替换"。
3. **诊断有据可依。** 插件没输出？先看 `fiber.state`：`PENDING` 说明依赖没就绪。

```python
provider = ctx.plugin(StorageA)
await provider
consumer_fiber = ctx.plugin(consumer)
await consumer_fiber            # ACTIVE

await provider.dispose()        # 提供方走了
await consumer_fiber
print(consumer_fiber.state)     # PENDING（自动卸载）
```

## 三种依赖写法

```python
consumer.inject = ['storage']                       # 数组：只声明名字
consumer.inject = {'storage': {'timeout': 5}}       # 映射：附带"拦截配置"（见下）
consumer.inject = None                              # 什么都不声明 = 可选依赖
```

可选依赖用探测写法，缺了也不影响插件运行：

```python
def plugin(ctx):
    storage = ctx.get('storage')          # 未提供返回 None
    if storage is not None:
        ...
```

> 注意区分 `ctx.get(name)` 与 `ctx.name`：
> `get` 是**原始值查找**（找不到返回 None，可在任意时候调用）；
> 属性访问 `ctx.name` 走**作用域链解析**，未声明 inject 时可能直接报错。
> 这正是"依赖必须声明"的强制点。

## 隔离与拦截：同一份代码，不同的能力集

`ctx.isolate(name)` 让某个**名字**在子树里解析到独立作用域：

```python
ctx.provide('llm', default_model)

tenant_a = ctx.isolate('llm')
tenant_a.provide('llm', model_a)      # 只影响 tenant_a 子树

tenant_b = ctx.isolate('llm')
tenant_b.provide('llm', model_b)
```

- 两个 `isolate('llm')` 传同一个 `label` 会让它们的**作用域合并**；
- 隔离边界之外的实现不可见（子树里的插件不会"意外"拿到外层实现）；
- 这是多租户/多会话的基础：dsh 用它给每个 agent 组装不同的能力集合。

`ctx.intercept(name, config)` 则不改实现、只改配置：

```python
scoped = ctx.intercept('llm', {'temperature': 0.1})
scoped.plugin(SomePlugin)     # 这个插件启动时，服务能读到合并后的配置

# 服务侧读取合并结果：
class LLMService(Service):
    def __init__(self, ctx):
        super().__init__(ctx, 'llm')
        self.config = self.resolve_config()
```

## 调用方绑定：一个容易忽略但极其重要的细节

先看一段代码，猜猜工具注册能不能被自动清理：

```python
class Tools(Service):
    def register(self, tool):
        self.store[tool.name] = tool
        self.ctx.cleanup(lambda: self.store.pop(tool.name, None))   # ← 挂到哪个 fiber 上？
        return tool

def my_plugin(ctx):
    ctx.tools.register(my_tool)     # 谁会负责撤销这次注册？
```

答案是：**挂到 `my_plugin` 的 fiber 上**。因为访问 `ctx.tools` 返回的不是服务本身，
而是一个"绑定到调用方"的轻量代理：

```python
# cordis/reflect.py（简化）
class ServiceBindingProxy:
    def __getattr__(self, name):
        value = getattr(self._target, name)
        if callable(value):
            return bind_callable(value, self._ctx)     # 调用期间设置 current_caller
        return value

def bind_callable(service, method, ctx):
    def wrapper(*args, **kwargs):
        token = current_caller.set(ctx)      # contextvars
        try:
            return method(*args, **kwargs)
        finally:
            current_caller.reset(token)
    return wrapper

class CallerBound:                        # Service 的基类之一
    @property
    def ctx(self):
        return current_caller.get() or self._service_ctx
```

于是服务方法里的 `self.ctx` 就是**调用方上下文**——服务内部的注册自然挂到调用方 fiber 上。
这是 TS 版可追踪代理（traceable proxy）在 Python 里的等价物，效果一致：

```text
插件 A 调用 ctx.tools.register(t)
  → 代理把 current_caller 设为 A 的上下文
  → 服务内部 self.ctx.cleanup(...) 注册到 A 的 fiber
  → A 卸载 → 工具自动消失
```

这就回答了第 1 章埋的问题："插件是自包含的"不是靠约定，而是靠机制。

### 什么时候要用 `owner_ctx`

如果你的服务需要读取**自己的**依赖（而不是替调用方注册），用 `self.owner_ctx`：

```python
class Agents(Service):
    def create(self):
        owner = self.owner_ctx
        session = owner.sessions.create()      # 读自己的依赖
        owner.cleanup(...)                     # 登记服务自身的资源
        scope = owner.plugin(child)            # 子作用域的父级是本服务
        return Agent(owner, session, scope)
```

一句话记忆：

> **替调用方登记用 `self.ctx`；读自己的依赖、管自己的资源用 `self.owner_ctx`。**

## 常见坑（第 3 章）

1. **服务名是扁平命名空间。** `events`/`logger`/`reflect`/`registry`/`fiber`/`root`
   已被框架占用；给你的服务加前缀（如 `myApp.storage`）。
2. **`inject` 是持续生效的，不是一次性检查。** 依赖消失 → 插件卸载；依赖回来 → 插件重载。
   所以插件必须能在"被卸载后重新加载"的情形下正确工作（把状态放在服务里，不要放全局变量）。
3. **未声明 inject 就访问 `ctx.name` 可能报错**——这是故意的，用来暴露隐藏耦合。
4. **`isinstance(ctx.service, MyService)` 成立，但 `type(ctx.service)` 是代理类型**；
   需要原始对象时用 `ctx.get('myService')`。

## 小结

- 服务 = 具名能力，`inject` = 声明依赖，加载顺序由依赖关系决定；
- 提供方替换会让消费方自动重启，这是"可配置产品"的物理基础；
- `isolate` 划作用域、`intercept` 改配置，同一个插件可以有不同的能力集；
- 调用方绑定让"服务内部的注册"归属调用方，这是自包含插件的关键。

下一章：[事件与策略](04-events.md) —— 插件之间的横向通信。
