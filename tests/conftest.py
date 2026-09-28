"""pytest 公共设施。

- ``anyio_backend``：让 ``@pytest.mark.anyio`` 的异步测试跑在 asyncio 上；
- 默认把日志级别压到 error，避免测试输出噪声。
"""

import os

import pytest

os.environ.setdefault('CORDIS_LOG_LEVEL', 'error')


@pytest.fixture
def anyio_backend():
    return 'asyncio'
