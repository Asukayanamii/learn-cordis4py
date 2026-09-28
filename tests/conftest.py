"""pytest 公共设施。

两个目的：

1. **让异步用例只依赖 pytest**：仓库不引入 pytest-asyncio / anyio，
   而是用下面 ~15 行实现 ``@pytest.mark.anyio``（把 async 用例跑在 asyncio 事件循环里）。
   这样"开发依赖只有 pytest"这句话是真的，克隆下来装一个包就能跑测试。
2. 默认把日志级别压到 error，避免测试输出噪声。

如果你的项目里的异步用例需要更复杂的能力（fixture 生命周期、trio 后端、超时控制），
再引入 anyio 或 pytest-asyncio 即可——本仓库不需要。
"""

import asyncio
import inspect
import os

import pytest

os.environ.setdefault('CORDIS_LOG_LEVEL', 'error')


def pytest_configure(config):
    config.addinivalue_line('markers', 'anyio: 在 asyncio 事件循环中运行这个异步用例')


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """``@pytest.mark.anyio`` 标记的异步用例：用 ``asyncio.run`` 执行。

    作为 firstresult 钩子，本函数返回 True 表示"已处理"，
    因此即使环境里装了 anyio / pytest-asyncio，也不会重复执行同一个用例。
    """
    if 'anyio' not in pyfuncitem.keywords:
        return None
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(func(**kwargs))
    return True
