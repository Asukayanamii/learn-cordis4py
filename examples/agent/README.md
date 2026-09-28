# 智能体核心示例（配置驱动）

这个目录把前面所有概念组装成一个**可用的智能体核心**：工具流水线、策略审批、
会话日志、agent 循环、可替换模型适配器、CLI —— 全部由 `cordis.yml` 组装。

```bash
python examples/agent/run.py "读取 pyproject.toml 并总结"   # 一次性任务（mock 模型）
python examples/agent/run.py                              # 交互式 REPL（/transcript 看记录）
python examples/agent/run.py --status                     # 插件树状态
python examples/agent/run.py --dump-config                # 生效后的配置树（patch 之后）
python examples/agent/run.py --patch "运行 echo hello"     # 换真实模型 + 放行危险工具
```

## 插件树（11 个条目）

| id | 文件 | 提供 | 依赖 | 说明 |
|----|------|------|------|------|
| `systemPrompt` | `system_prompt.py` | `ctx.systemPrompt` | — | 提示词片段注册表 |
| `persona` | `persona.py` | — | `systemPrompt` | 人设片段 + `agent/request` 调参 |
| `sessions` | `sessions.py` | `ctx.sessions` | — | 仅追加会话日志 + JSONL 持久化 |
| `tools` | `tools.py` | `ctx.tools` | — | 工具注册表 + 三段式执行流水线 |
| `tools-builtin` | `tools_builtin.py` | — | `tools` | read_file / write_file / list_dir / shell |
| `approval` | `approval.py` | — | `tools` | 审批、脱敏、输入把关（全在事件上） |
| `llm` | `llm.py` | `ctx.llm` | — | 模型 seam + 适配器注册 |
| `llm-mock` | `llm_mock.py` | — | `llm` | 确定性 mock 适配器（无需 API Key） |
| `agents` | `agent.py` | `ctx.agents` | llm/tools/sessions/systemPrompt | 轮次/步骤循环 |
| `telemetry` | `telemetry.py` | — | `tools` | 只观察：工具/轮次统计 |
| `cli` | `cli.py` | `ctx.cli` | `agents` | REPL 与一次性任务 |

## 用 patch 改造产品（不改代码）

`cordis.patch.yml` 演示三件事：

```yaml
- id: llm                       # ① 换模型：config 整体替换
  config: {provider: deepseek, model: deepseek-chat}
- insert:                       # ② 插入真实模型适配器
    - id: llm-deepseek
      name: ./plugins/llm_openai.py
- id: approval                  # ③ 改策略：放行危险工具
  config: {allow_dangerous: true}
- id: telemetry
  disabled: true                # ④ 关掉遥测
```

用真实模型需要设置环境变量（默认 `DEEPSEEK_API_KEY`，可在 patch 里改 `api_key_env`）：

```bash
# PowerShell
$env:DEEPSEEK_API_KEY = "sk-..."
python examples/agent/run.py --patch "运行 python --version"
```

## 会话日志

`config.directory` 指定 JSONL 输出目录（默认 `./sessions`，已 gitignore）。
每个事件一行，包含 `turn/start`、`user/message`、`assistant/message`、`tool/result`、
`step/start`、`step/end`、`turn/end`。把它喂给任何回放/评测脚本即可复现一次运行。

## 想改什么，看这里

| 想做的事 | 改哪里 |
|----------|--------|
| 换模型 | 注册新的 `llm` 适配器（照抄 `llm_openai.py`），在配置里切 `provider` |
| 加工具 | 在 `tools_builtin.py` 里加 `@ctx.tools.tool(...)`，或新建插件 |
| 加审批规则 | 改 `approval.py`（或新增一个 `tools/pre-execute` 监听插件） |
| 改提示词 | 改 `cordis.yml` 里 `systemPrompt.config.base` / `persona.config` |
| 换 UI | 改 `cli.py`，或新增一个 Web/headless 插件——agent 循环不用动 |
| 多会话用不同能力集 | 用 `ctx.isolate(...)` 给 agent 子作用域提供不同的服务实现 |
