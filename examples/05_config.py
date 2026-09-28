"""05 · 配置与 Schema：让插件接受用户配置，并在输入错误时明确报错。

- 插件用类属性 ``Config`` 声明配置结构（``class Config(Schema): ...``）；
- 框架在插件启动**之前**校验配置，错误会聚合全部问题后抛出 ValidationError；
- 校验通过后，归一化后的配置通过 ``config`` 参数传入，也可以在 ``fiber.config`` 读到；
- ``fiber.update(new_config)`` 可以在运行时改配置并自动重启插件（内部走
  ``internal/update`` waterfall，装载器/HMR 可以借此持久化配置）。

运行：python examples/05_config.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cordis import Context, Schema, ValidationError  # noqa: E402


class Config(Schema):
    api_key = Schema.string().required().description('API 密钥')
    model = Schema.string().default('deepseek-chat')
    max_tokens = Schema.integer().default(1024)
    retries = Schema.number().default(2)
    tags = Schema.array(Schema.string()).default([])


def plugin(ctx, config):
    """config 就是校验、归一化之后的配置。"""
    ctx.logger.info('启动：model=%s max_tokens=%s tags=%s',
                    config['model'], config['max_tokens'], config['tags'])


plugin.Config = Config


async def main():
    ctx = Context()

    print('--- 正确配置：缺省值被填上 ---')
    fiber = ctx.plugin(plugin, {'api_key': 'sk-demo', 'model': 'deepseek-reasoner'})
    await fiber
    print('  fiber.config =', fiber.config)

    print('--- 错误配置：聚合报错（不是只报第一条）---')
    broken = ctx.plugin(plugin, {'api_key': 123, 'max_tokens': 'many'})
    try:
        await broken
    except ValidationError as error:
        print('  捕获 ValidationError:')
        for line in str(error).splitlines():
            print('   ', line)
    print('  失败插件状态:', broken.state.name)

    print('--- 运行时改配置：校验后自动重启 ---')
    fiber.update({'api_key': 'sk-demo', 'max_tokens': 4096, 'tags': ['prod']})
    await fiber
    print('  新配置 =', fiber.config)


asyncio.run(main())
