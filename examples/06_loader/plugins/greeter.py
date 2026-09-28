"""插件：提供 greeter 服务（模块导出 plugin = 类）。"""

from cordis import Service


class GreeterService(Service):
    """一个最小的服务：``ctx.greeter``。"""

    def __init__(self, ctx, config=None):
        super().__init__(ctx, 'greeter')
        self.greeting = (config or {}).get('greeting', 'Hello')

    def greet(self, who):
        return f'{self.greeting}, {who}!'


#: loader 会优先读取模块的 ``plugin`` 导出
plugin = GreeterService
