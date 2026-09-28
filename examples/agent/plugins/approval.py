"""策略插件：审批、输入把关与结果审查。

这是"事件即扩展点"的实战：

- ``tools/pre-execute``（waterfall）：危险工具的把关——**不调用 next() 直接返回
  即否决**，工具根本不会被执行；
- ``tools/post-execute``（waterfall）：审查/脱敏工具结果；
- ``agent/pre-step``（waterfall）：拒绝不该进入模型的输入。

注意策略插件不 import 任何工具实现，工具也不 import 策略——两边只认事件名。
"""

import re

inject = ['tools']

name = 'approval'

_SECRET = re.compile(r'(sk-[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._-]{10,})')


def apply(ctx, config=None):
    config = config or {}
    deny = set(config.get('deny') or [])
    allow = set(config.get('allow') or [])
    allow_dangerous = bool(config.get('allow_dangerous'))
    forbid = list(config.get('forbid') or [])
    log = ctx.logger('approval')

    async def policy(call, next_):
        if call.name in deny:
            log.warn('拒绝工具调用 %s（deny 列表）', call.name)
            return {'ok': False, 'error': f'策略拒绝了工具 {call.name}'}
        tool = ctx.tools.store.get(call.name)
        if tool is not None and tool.dangerous and not allow_dangerous and call.name not in allow:
            log.warn('拒绝危险工具 %s（allow_dangerous=false）', call.name)
            return {'ok': False, 'error': f'{call.name} 是危险工具，当前策略未放行'}
        if tool is not None and call.name == 'read_file':
            # 参数改写示例：把路径限制在工作区内（拒绝越界访问）
            path = str(call.arguments.get('path', ''))
            if '..' in path:
                log.warn('拒绝越界路径 %s', path)
                return {'ok': False, 'error': '路径不被允许（包含 ..）'}
        return await next_()

    async def redact(call, result, next_):
        result = await next_()
        if isinstance(result.get('content'), str):
            cleaned, count = _SECRET.subn('***', result['content'])
            if count:
                log.info('工具 %s 的结果中有 %d 处敏感信息，已脱敏', call.name, count)
            result['content'] = cleaned
        return result

    async def pre_step(message, next_):
        for word in forbid:
            if word in message['content']:
                log.warn('输入包含禁用词 %r，被拒绝', word)
                return None
        return await next_()

    ctx.on('tools/pre-execute', policy)
    ctx.on('tools/post-execute', redact)
    ctx.on('agent/pre-step', pre_step)
    log.info('策略已启用：deny=%s allow_dangerous=%s forbid=%s',
             sorted(deny) or '[]', allow_dangerous, forbid or '[]')
