"""插件：消费 greeter 服务（模块导出 apply + inject 元数据）。"""

inject = ['greeter']


def apply(ctx):
    ctx.logger.info(ctx.greeter.greet('cordis'))
    # 通过 ctx 注册的资源会随插件卸载自动撤销
    ctx.cleanup(lambda: ctx.logger.debug('consumer unloaded'))
