# 示例索引

| 文件 | 主题 | 运行 |
|------|------|------|
| `01_hello.py` | 第一个插件：函数/对象/类三种形态、配置、服务 | `python examples/01_hello.py` |
| `02_effects.py` | 生命周期与 effect：状态机、逆序释放、失败处理 | `python examples/02_effects.py` |
| `03_service.py` | 服务与依赖注入：PENDING 等待、替换提供方 | `python examples/03_service.py` |
| `04_events.py` | 事件五种分发模式：emit/parallel/serial/bail/waterfall | `python examples/04_events.py` |
| `05_config.py` | 配置与 Schema 校验、运行时更新 | `python examples/05_config.py` |
| `06_loader/` | 用 `cordis.yml` 组装插件树、patch 叠加层、热重载 | `python examples/06_loader/run.py --dump` |
| `agent/` | **完整智能体核心**：工具流水线、策略审批、会话日志、agent 循环、CLI | `python examples/agent/run.py "读取 pyproject.toml"` |

推荐的阅读顺序就是上面的顺序；`agent/` 把前面所有概念组合成一个可用的智能体。
