# 05 · 配置与装载器：应用就是一棵插件树

> 配套代码：`python tutorial/ch05_config.py`（配置校验）、`python tutorial/ch06_loader.py`（装载器与 patch）

## 一、配置必须在启动前校验

没有校验的配置系统只有两种结局：带着错误跑，或者在深水区崩溃。
本框架的做法和 dsh 一致：**插件声明配置结构，框架在插件启动之前校验，
错误一次性聚合报出**。

```python
from cordis import Context, Schema

class Config(Schema):                                  # 类声明式
    api_key = Schema.string().required()
    model = Schema.string().default('deepseek-chat')
    max_tokens = Schema.integer().default(1024)
    tags = Schema.array(Schema.string()).default([])
    port = Schema.union(Schema.string().transform(int), Schema.integer())

def plugin(ctx, config):
    ...                                                # config 已经过校验 + 补全缺省值

plugin.Config = Config
```

错误长这样（**全部问题一次说完**，并给出路径）：

```text
ValidationError: invalid config:
  - expected a string but got int (at api_key)
  - expected a number but got str (at max_tokens)
  - expected a list but got str (at tags)
```

校验失败时 fiber 进入 `FAILED`，`await fiber` 抛出异常——启动阶段就能发现，
而不是等某个字段在被使用时才炸。

### 运行时改配置

```python
fiber.update({'api_key': 'sk-1', 'max_tokens': 4096})   # 校验 → 重启插件
```

`update` 会先走 `internal/update` waterfall：装载器在这里把新配置写回 YAML（持久化），
HMR 在这里替换模块——**配置变更和模块热替换走同一条通道**。

## 二、把插件树写进 YAML

代码里挂插件适合写测试和示例；产品需要的是"配置即应用"：

```yaml
# cordis.yml
- id: llm                      # id 用于被 patch 定位
  name: ./plugins/llm.py       # 模块说明符（相对路径 / 包路径 / path.py:attr）
  config:
    provider: mock
    model: mock-1

- id: tools
  name: ./plugins/tools.py

- id: tools-builtin
  name: ./plugins/tools_builtin.py
  config:
    workspace: .
  inject: ['tools']            # 条目级依赖声明（与插件内声明等价，会合并）

- id: core
  group: true                  # 组：只用于组织与批量启停
  config:
    - id: telemetry
      name: ./plugins/telemetry.py
    - id: cli
      name: ./plugins/cli.py
      disabled: false          # 也可以置 true 临时禁用
```

装载器的职责：

1. 读取配置 → 规范化条目（补 id、校验字段）；
2. 解析模块：`./plugins/x.py` 用 `importlib` 按**唯一模块名**加载
   （这正是热重载能拿到新代码的关键：每次导入都是全新的模块对象）；
3. 逐条挂载插件，记录状态（`active` / `failed` / `disabled`）；
4. 提供诊断：`loader.status()`、`loader.dump_config()`。

> **路径锚定**：配置里的路径不应该受"从哪个目录启动"影响。装载器会把字符串里的
> `${baseDir}`（配置文件所在目录）与 `${cwd}`（进程工作目录）展开成绝对路径，
> 所以推荐写成 `directory: ${baseDir}/sessions`。

```python
from cordis.loader import start_app

ctx = await start_app('cordis.yml')
for info in ctx.loader.status():
    print(info)     # {'id': 'llm', 'state': 'active', 'name': './plugins/llm.py'}
```

**坏模块只影响自己**：`ghost.py` 不存在 → 该条目 `failed`，其余条目照常 `active`。
这是"应用是一棵树"的另一个好处：故障被限制在子树里。

## 三、patch 叠加层：不改原配置地改造产品

dsh 的组装模型是"在空条目列表上按序叠加若干层配置"。本仓库实现同样的语义：

```yaml
# cordis.patch.yml
- insert:                      # 插入新条目
    - id: llm-deepseek
      name: ./plugins/llm_openai.py
      config:
        base_url: https://api.deepseek.com

- id: llm                      # 按 id 定位，替换其【整个】config
  config:
    provider: deepseek
    model: deepseek-chat

- id: telemetry                # 禁用某个条目
  disabled: true

- id: legacy                   # 移除条目
  remove: true
```

启动时按序应用：

```python
ctx = await start_app('cordis.yml', patches=['company.patch.yml', 'local.patch.yml'])
```

这就是"同一份产品、不同部署"的实现方式：

| 层 | 谁写 | 例子 |
|----|------|------|
| 基础 `cordis.yml` | 产品作者 | 内置插件与默认配置 |
| `*.patch.yml` | 团队/部署者 | 换模型、接内网、加合规策略 |
| 运行参数 | 使用者 | `python run.py --patch` |

> 注意 patch 会**整体替换** config（与 dsh 一致），不是深合并。
> 想让"部分字段"可改，就在基础配置里给出完整默认值，patch 复制一份再改。

## 四、热重载

装载器的 `watch: true` 会轮询插件文件的 mtime，变化后：

```text
stop 该条目（卸载 fiber → 逆序释放它注册的一切）
  → 重新导入模块（全新模块对象）
  → 重新挂载（拿到新代码、新配置）
```

```python
ctx = await start_app('cordis.yml', watch=True)
```

手动触发同样简单：

```python
await ctx.loader.reload_entry('greeter')   # 只重载一条
await ctx.loader.reload_config()           # 重新读配置 + 重建整棵树
```

热重载之所以能成立，靠的是前四章的两条性质：

1. 卸载彻底（effect 逆序释放、服务注销、依赖方联动）——没有残留状态；
2. 加载可重复（插件不假设"只运行一次"，状态放在服务里）。

## 五、装载器自己也是插件

```python
ctx = await start_app('cordis.yml')
await ctx.loader.dispose()        # 卸载装载器 = 卸载整棵插件树
```

因为它用的就是 `ctx.plugin()` 那套机制（它挂在 `ctx.loader` 这个服务名上，
插件树是它的子 fiber）。这不是巧合，而是"万物皆插件"的自洽：
**连"加载插件"这件事也是插件。**

## 常见坑（第 5 章）

1. **配置文件里的相对路径以配置文件所在目录为基准**，不是进程 cwd；
   需要时用 `base_dir` 显式指定。
2. **patch 的 id 必须存在**，否则启动即报错（`cannot resolve entry`）——
   dsh 也是"非法 patch 在启动时失败"，而不是静默忽略。
3. **不要用 `python -c` 之类的方式启动**：装载器需要稳定的工作目录与配置文件路径。
4. **热重载会丢内存状态**：插件被卸载重装，模块级变量重置。需要跨重载保留的状态
   请放进服务或外部存储。

## 小结

- 配置在启动前校验，错误聚合、带路径、直接失败；
- `cordis.yml` 描述应用，`patch` 叠加层描述"这套部署的差异"；
- 装载器也是插件，卸载它就是卸载应用；
- 热重载 = 彻底卸载 + 全新导入 + 重新挂载，全靠前四章的性质支撑。

下一章：[智能体核心设计](06-agent-core.md) —— 把这些机制组装成一个真正的 agent。
