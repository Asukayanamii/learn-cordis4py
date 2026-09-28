"""遥测插件：只观察，不干预。

演示"事件即观察点"：不 import agent/tools 的实现，只在事件上挂监听器，
统计工具调用与轮次情况，并往提示词里加一段"当前会话统计"（可选）。
"""

inject = ['tools']


def apply(ctx, config=None):
    config = config or {}
    log = ctx.logger('telemetry')
    stats = {'calls': 0, 'failed': 0, 'turns': 0}
    print_stats = bool(config.get('print', True))

    def on_call(call):
        stats['calls'] += 1
        log.debug('工具调用 #%d: %s', stats['calls'], call.name)

    def on_result(call):
        if not call.result or not call.result.get('ok'):
            stats['failed'] += 1
            log.warn('工具 %s 失败: %s', call.name, (call.result or {}).get('error'))
        elif print_stats:
            preview = (call.result.get('content') or '').replace('\n', ' ')[:80]
            log.info('工具 %s 完成: %s', call.name, preview)

    def on_session_event(session, event):
        if event.type == 'turn/end':
            stats['turns'] += 1
            log.info('轮次 #%d 结束（累计工具调用 %d，失败 %d）', stats['turns'], stats['calls'], stats['failed'])

    ctx.on('tool/call', on_call)
    ctx.on('tool/result', on_result)
    ctx.on('session/event', on_session_event)
