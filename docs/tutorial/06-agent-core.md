# 06 · 智能体核心设计：把五个原语组装成 agent

> 配套代码：`python tutorial/ch07_agent.py`（约 330 行，单文件可跑）
> 完整成品：`examples/agent/`（配置驱动、多插件、真实模型适配器、REPL）

## 架构一眼看全

```text
                       ┌──────────────────────────────┐
   cordis.yml ───────► │  Loader（插件树）             │
                       └──────────────┬───────────────┘
                                      │ 挂载
     ┌────────────────┬───────────────┼────────────────┬─────────────────┐
     ▼                ▼               ▼                ▼                 ▼
┌─────────┐    ┌────────────┐   ┌───────────┐   ┌────────────┐   ┌────────────┐
│ctx.system│   │ctx.sessions│   │ ctx.tools │   │  ctx.llm   │   │ ctx.agents │
│ Prompt  │    │ 会话日志    │   │ 工具流水线 │   │ 模型 seam  │   │ agent 循环 │
└─────────┘    └────────────┘   └───────────┘   └────────────┘   └────────────┘
     ▲               ▲               ▲                ▲                 ▲
     │               │               │                │                 │
  提示词片段      持久事实        工具即插件       适配器即插件       驱动 + 事件
  （插件贡献）    （可回放）      （可被策略拦截） （mock/真实模型）  （pre-step/request…）
```

每个方块都是**一个服务 + 一组事件**，也就是一个**能力 seam**（接缝）。

## 什么是 seam：三个角色

一个可替换能力由三种角色组成，缺一不可：

| 角色 | 职责 | 例子 |
|------|------|------|
| **Service Definition** | 声明接口（怎么调用） | `LLMService.chat(messages, tools) -> message` |
| **Service Provider** | 实现它（可多个、可替换） | `llm_mock`、`llm_openai` |
| **Consumer** | 使用它（通常面向模型） | agent 循环、`tools-builtin` |

看清这三种角色，就看清了整个系统"哪里可以换"：

```text
换模型：注册新的 llm 适配器（不动 agent）
换工具集：卸载 tools-builtin，换一个提供工具集的插件（不动 agent）
换审批策略：挂一个 tools/pre-execute 监听器（不动工具、不动 agent）
换 UI：换 cli 插件为 Web/headless/SDK（不动 agent）
```

## 一、工具流水线：让策略与工具解耦

工具的**执行**被设计成三个 waterfall 事件，而不是一个函数调用：

```python
# plugins/tools.py（简化）
async def execute(self, name, arguments, ctx=None, call_id=None):
    call = ToolCall(name, arguments, ctx, call_id)
    self.ctx.emit('tool/call', call)                       # 观察点（同步广播）

    result = await maybe_await(self.ctx.waterfall('tools/pre-execute', call, execute_tool))
    result = await maybe_await(self.ctx.waterfall('tools/post-execute', call, result, keep))
    call.result = result
    self.ctx.emit('tool/result', call)                     # 观察点
    return result
```

于是策略插件可以做到这些，而工具本身**一行都不用改**：

```python
# plugins/approval.py（节选）
async def policy(call, next_):
    if call.name in DENY:
        return {'ok': False, 'error': f'策略拒绝了工具 {call.name}'}   # 短路 = 否决
    tool = ctx.tools.store.get(call.name)
    if tool and tool.dangerous and not allow_dangerous:
        return {'ok': False, 'error': f'{call.name} 是危险工具，当前策略未放行'}
    return await next_()

async def redact(call, result, next_):
    result = await next_()
    result['content'] = SECRET.sub('***', result['content'])     # 结果脱敏
    return result

ctx.on('tools/pre-execute', policy)
ctx.on('tools/post-execute', redact)
```

**注册工具也体现"注册即副作用"**：

```python
@ctx.tools.tool('read_file', '读取文件', {...})
async def read_file(agent_ctx, path): ...
```

装饰器内部做的是 `self.ctx.cleanup(...)`——而 `self.ctx` 是**调用方上下文**（第 3 章的调用方绑定），
所以工具随 `tools-builtin` 插件卸载而消失。

## 二、会话日志：模型可见即已记录

智能体最容易出 bug 的地方是"模型看到了什么"。本设计的答案是**只从日志投影**：

```python
class Session:
    def append(self, type_, **payload):        # 仅追加
        ...

    def derive_messages(self):                 # 投影出模型历史
        for event in self.events:
            if event.type == 'user/message':   → {'role': 'user', ...}
            if event.type == 'assistant/message': → {'role': 'assistant', ...}
            if event.type == 'tool/result':    → {'role': 'tool', ...}
```

好处：

- **恢复/续跑**：重新投影即可，不需要重建内存状态；
- **可回放**：出问题时把 JSONL 日志拿出来就是完整现场；
- **可观测**：任何插件监听 `session/event` 就能做 UI/遥测/审计；
- **上下文工程**（裁剪、压缩、注入）变成"对日志的操作"，可测试。

事件类型（本示例的词汇表）：

```text
turn/start   user/message   step/start   assistant/message
tool/result  step/end       turn/end     （持久事件）

agent/created  tool/call  session/event  agent/assistant-stream  （实时事件）
```

## 三、agent 循环：一个轮次的生命周期

```text
turn/start
  领取输入
  agent/pre-step      ← waterfall：可以拒绝或改写输入
  写入 user/message（持久事实）
  ┌─ 每个 step：
  │   组装提示词片段 + 工具 schema
  │   agent/request        ← waterfall：可以换模型/裁剪上下文
  │   模型响应 → assistant/message
  │   有工具调用 → tools/* 流水线 → tool/result（每条都持久化）
  │   还有工具调用 → 下一个 step
  └─ 否则结束
  agent/turn-stopping ← serial：可以让循环停下
  turn/end（保存 JSONL）
```

对应代码（`plugins/agent.py` 节选）：

```python
async def run_turn(self):
    sessions.append(self.session, 'turn/start')
    while self.inbox:
        text = self.inbox.pop(0)
        decision = await maybe_await(self.ctx.waterfall('agent/pre-step', {'content': text}, accept))
        if not decision:
            continue
        sessions.append(self.session, 'user/message', content=decision['content'])
        await self.run_steps()

async def run_steps(self):
    for step in range(max_steps):
        request = self._assemble_request()                       # 提示词 + 工具 schema
        request = await maybe_await(self.ctx.waterfall('agent/request', request, use_request))
        reply = await self.ctx.llm.chat(**request)
        sessions.append(self.session, 'assistant/message', message=reply)
        if not reply.get('tool_calls'):
            break
        for call in reply['tool_calls']:
            result = await self.ctx.tools.execute(call['name'], call['arguments'], ctx=self.ctx)
            sessions.append(self.session, 'tool/result', call=call, result=result)
```

### 每个 agent 有自己的 fiber

```python
scope = owner.plugin({'name': f'agent/{session.id}', 'apply': lambda ctx, config: None})
agent.ctx = scope.ctx.extend({'agent': self})
```

这不是装饰，而是"按 agent 作用域"的落点：任何插件都可以用 `agent.ctx` 给**某一个** agent
注册专属能力（专属工具、专属提示词片段、专属策略），agent 结束时一起撤销。

> dsh 里对应的说法是"给某个会话组装 agent preset，其中的服务行需要 `isolate` realm"。
> 本示例用"每个 agent 一个子 fiber"实现同一目标，更简单也够用；
> 需要真正的服务替换时，用第 3 章的 `ctx.isolate()`。

## 四、能力 seam 清单：你的 agent 应该有哪些

| seam | 本示例 | 换成别的实现会得到什么 |
|------|--------|----------------------|
| `ctx.llm` | `llm_mock` / `llm_openai` | 换任何 OpenAI 兼容端点、本地模型、路由网关 |
| `ctx.tools` | `tools` + `tools_builtin` | 换成沙箱执行、远程执行、只读文件系统 |
| `ctx.sessions` | 内存 + JSONL | 换成数据库、对象存储、加密日志 |
| `ctx.systemPrompt` | 片段注册表 | 换成模板引擎、按角色拼装 |
| `ctx.agents` | 单步循环 | 换成多步规划、子 agent 委派、并行工具调用 |
| `ctx.cli` | REPL / 一次性任务 | 换成 Web UI、headless runner、SDK server |

### 加一个能力要让三者一起设计

以"给 agent 加**记忆**（memory）"为例：

1. **Definition**：`class MemoryService(Service)`，接口 `recall(query) -> list[str]` / `remember(text)`；
2. **Provider**：`memory_sqlite.py`（本地）/ `memory_remote.py`（服务化）；
3. **Consumer**：一个插件监听 `agent/pre-step`，把召回的记忆作为 `agent.inject()` 注入
   （注入的上下文会落到下一次模型请求，且不打断会话日志的投影规则）。

顺手就得到了"可替换"与"可观测"两个性质。

## 五、从单文件到产品：示例的分层

| 层次 | 文件 | 说明 |
|------|------|------|
| 单文件教学版 | `tutorial/ch07_agent.py` | 约 330 行：工具 + mock 模型 + 循环 + 策略 |
| 配置驱动版 | `examples/agent/` | 11 个插件条目、`cordis.yml` 组装、`--patch` 换模型、REPL |
| 框架 | `cordis/` | 把前面五章的能力做完整（隔离、拦截、装载器、诊断） |

`examples/agent/cordis.yml` 就是"应用即配置"的样子：

```yaml
- id: tools
  name: ./plugins/tools.py
- id: tools-builtin
  name: ./plugins/tools_builtin.py
  config:
    workspace: .
- id: approval
  name: ./plugins/approval.py
  config:
    allow_dangerous: false
    forbid: ['rm -rf /']
- id: agents
  name: ./plugins/agent.py
  config:
    max_steps: 4
```

而 `cordis.patch.yml` 演示"换模型 + 放行危险工具 + 关遥测"三个动作都不改代码：

```bash
python examples/agent/run.py "运行 echo hello"            # 被策略拒绝
python examples/agent/run.py --patch "运行 echo hello"     # 放行后执行成功
```

## 六、设计检查表（写你自己的 agent 时对照）

1. **每个能力都有 seam 吗？** 模型、工具、存储、提示词、循环、UI——都能替换吗？
2. **注册都可撤销吗？** 有没有在插件外面保存监听器/连接/定时器？
3. **模型看到的东西都能从日志重建吗？**（"模型可见即已记录"）
4. **策略和机制分离了吗？** 审批/脱敏/限流应该挂在事件上，而不是写进工具实现。
5. **失败会静默吗？** 插件加载失败、工具执行失败、模型调用失败——都有明确状态吗？
6. **能否只 reload 一个插件而不用重启进程？**

## 小结

- 智能体核心 = 一组能力 seam（服务）+ 一组扩展点（事件）+ 一棵插件树（配置）；
- 工具流水线用 waterfall 把"策略"从"工具"里剥离出来；
- 会话日志是模型上下文的唯一来源，让恢复、回放、观测都变成同一条路径；
- 每个 agent 一个子 fiber，使"按 agent 作用域的注册"成为机制。

下一章：[Python 适配与坑位清单](07-python-notes.md)。
