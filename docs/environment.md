# 依赖与环境：conda / venv / IDE

这份文档说明本项目**需要什么**、**有哪几种装法**、**国内网络下怎么装得快**。
一句话版本：**运行时只有 PyYAML，开发只有 pytest**，没有编译依赖。

---

## 一、依赖清单

| 用途 | 包 | 版本 | 用在哪 / 不加会怎样 |
|------|----|------|---------------------|
| 运行时 | **PyYAML** | `>=6.0` | 只有 `cordis/loader/` 读 `cordis.yml` 时用。不加：框架本体、`examples/01`–`05`、`tutorial/ch01`–`ch05`、`ch07` 都能跑，只是 `06_loader` 与 `examples/agent` 起不来 |
| 开发 | **pytest** | `>=8.0` | 75 个用例。不需要 pytest-asyncio / anyio——异步用例由 `tests/conftest.py` 里 15 行钩子跑在 asyncio 上 |
| 标准库 | — | — | 其余全部只用标准库（`asyncio` / `contextvars` / `importlib` / `urllib` / `subprocess`…），无编译工具链、无 C 扩展 |
| 项目本体 | **cordis** | 本仓库 | `pip install -e .` 会把它装成可编辑包；不装也能在仓库根目录跑示例与测试（仓库根会进 `sys.path`） |

**Python 版本：>= 3.11**（`pyproject.toml` 的 `requires-python`）。
3.11 起才有的 `ExceptionGroup` 用在 `parallel` 事件分发里，因此 3.10 及更低不行。
> 注意：conda 的 `base` 环境在不少机器上是 3.9，不能直接跑本项目，请另建环境。

依赖声明只有三处，**`pyproject.toml` 是权威来源**：

| 文件 | 给谁用 | 内容 |
|------|--------|------|
| `pyproject.toml` | pip / 打包 | `[project].dependencies`、`[project.optional-dependencies].dev` |
| `requirements.txt`、`requirements-dev.txt` | IDE、习惯 pip 的人 | 与上面保持一致 |
| `environment.yml` | conda 用户 | `python=3.13 + pyyaml + pytest + pip` |

改依赖时请同步这三处（或只改 `pyproject.toml`，另两处保持"薄"清单）。

---

## 二、四种装法

### 方式 1：conda（仓库自带 `environment.yml`）

```bash
conda env create -f environment.yml     # 创建名为 learn-cordis 的环境
conda activate learn-cordis
python -m pytest -q                     # 75 passed 即就绪
python examples/agent/run.py "列出目录"
```

`environment.yml` 用的是 **defaults 渠道 + `python=3.13`**，这是刻意选的：

> **实测（本机 conda 4.9.2）**：指定 `conda-forge` 渠道 + 宽版本范围（`python>=3.10`）时，
> 经典求解器要先拉取并解析 conda-forge 的元数据，**几分钟都不结束**，看起来像卡死；
> 换成 `defaults` 且把 Python 固定到 3.13 后，**求解只需约 7 秒**。
> 原因是 conda 4.9 没有 libmamba 求解器，渠道越大、约束越松就越慢——不是机器慢。

想删掉环境：`conda env remove -n learn-cordis`
想让已有环境按文件更新：`conda env update -n learn-cordis -f environment.yml`

### 方式 2：venv + pip

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1            # Windows PowerShell
# source .venv/bin/activate            # Linux / macOS
python -m pip install -e ".[dev]"     # 装本体 + 开发依赖（pytest）
python -m pytest -q
```

只想要运行时：`python -m pip install -e .`（只装 PyYAML）。

### 方式 3：什么都不装，直接跑

在仓库根目录下，示例与教程靠"仓库根进 `sys.path`"即可运行：

```bash
python examples/01_hello.py            # 零第三方依赖
python tutorial/ch07_agent.py "读取 pyproject.toml"
python -m pip install pyyaml           # 只有用到 loader 的示例才需要
python examples/agent/run.py "列出目录"
```

测试同理：在仓库根目录 `python -m pytest -q` 即可（pytest 会把仓库根加入 `sys.path`）。
只有"想在任何目录 `import cordis`"时才需要 `pip install -e .`。

### 方式 4：IDE 里直接配

**PyCharm**

1. `Settings → Project → Python Interpreter → Add Interpreter`；
2. 选 **Conda**，解释器路径指向 `...\miniconda3\Scripts\conda.exe`：
   - 已有环境：选 `Existing`，挑 `learn-cordis`；
   - 新建环境：选 `New`，Python 选 3.11 及以上；
3. 在项目终端执行 `python -m pip install -e ".[dev]"`；
4. 打开 `requirements-dev.txt` 时 PyCharm 通常也会弹 **Install requirements** 横幅，一键等价。

**VS Code**

1. `Ctrl+Shift+P → Python: Select Interpreter`，选择或创建 conda 环境；
2. 集成终端里 `python -m pip install -e ".[dev]"`；
3. 测试面板会自动发现 `tests/`（`pyproject.toml` 里已配置 `testpaths = ["tests"]`）。

> `.venv/`、`venv/`、conda 环境目录都已写进 `.gitignore`，不会污染仓库。

---

## 三、国内网络：镜像配置

conda（写入 `%USERPROFILE%\.condarc`，Linux 是 `~/.condarc`）：

```yaml
channels:
  - defaults
show_channel_urls: true
default_channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r
custom_channels:
  conda-forge: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud
```

pip（一次性或写进 `pip.ini` / `pip.conf`）：

```bash
python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
# 需要时再加：python -m pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn
```

镜像站会变，命令以镜像站首页说明为准（清华 TUNA、阿里云、中科大都有 anaconda / pypi 镜像）。

---

## 四、常见问题

**Q：`conda env create` 卡在 "Solving environment" 或 "Downloading and Extracting" 很久？**
先看上面"实测"那段：换成 `defaults` + 固定 `python=3.13`；仍慢就是网络，配镜像。
实在不想等，用最快的两步走：

```bash
conda create -n learn-cordis python=3.13 -y     # 只建 python（本地缓存里通常已有）
conda activate learn-cordis
python -m pip install pyyaml pytest             # 两个纯 Python 包，走 pip 往往更快
```

**Q：报 `ModuleNotFoundError: No module named 'cordis'`？**
两种解法：在仓库根目录运行（自动进 `sys.path`），或 `python -m pip install -e .`。

**Q：测试里出现 "coroutine was never awaited" 警告、或用例被跳过？**
`tests/conftest.py` 负责把 `@pytest.mark.anyio` 的异步用例跑起来，别删它。
若你更喜欢标准插件：`pip install anyio`（仓库不依赖它，只是兼容——装了也不会重复执行）。

**Q：Python 3.9 / 3.10 报错？**
项目要求 >= 3.11（`ExceptionGroup`）。`conda create -n learn-cordis python=3.13` 即可。

**Q：PyYAML 能不能不装？**
能——只要不用 `cordis.loader`（即不跑 `06_loader` 与 `examples/agent`）。`cordis/` 核心与 `tests/test_loader.py` 之外的所有测试都不需要它。
