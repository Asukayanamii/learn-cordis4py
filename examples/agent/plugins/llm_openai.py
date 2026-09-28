"""OpenAI 兼容适配器（DeepSeek / OpenAI / 本地 vLLM 等），仅依赖标准库。

启用方式（cordis.patch.yml 或 cordis.yml）：

.. code-block:: yaml

    - id: llm
      config:
        provider: deepseek
        model: deepseek-chat
    - insert:
        - id: llm-deepseek
          name: ./plugins/llm_openai.py
          config:
            base_url: https://api.deepseek.com
            api_key_env: DEEPSEEK_API_KEY
"""

import asyncio
import json
import os
import urllib.error
import urllib.request

inject = ['llm']

name = 'llm-openai'


def apply(ctx, config=None):
    config = config or {}
    base_url = config.get('base_url', 'https://api.deepseek.com').rstrip('/')
    api_key_env = config.get('api_key_env', 'DEEPSEEK_API_KEY')
    timeout = float(config.get('timeout', 120))

    @ctx.llm.adapter('deepseek')
    async def deepseek(request):
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(f'环境变量 {api_key_env} 未设置，无法调用真实模型')
        payload = {
            'model': request.get('model', 'deepseek-chat'),
            'messages': request['messages'],
            **{key: value for key, value in request.items()
               if key in ('temperature', 'max_tokens', 'top_p')},
        }
        if request.get('tools'):
            payload['tools'] = [
                {'type': 'function', 'function': tool} for tool in request['tools']
            ]
            payload['tool_choice'] = 'auto'

        def post():
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            http_request = urllib.request.Request(
                f'{base_url}/chat/completions',
                data=data,
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
            )
            with urllib.request.urlopen(http_request, timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))

        try:
            body = await asyncio.to_thread(post)
        except urllib.error.HTTPError as error:
            detail = error.read().decode('utf-8', 'replace')[:500]
            raise RuntimeError(f'模型请求失败 HTTP {error.code}: {detail}') from error

        message = body['choices'][0]['message']
        return {
            'role': 'assistant',
            'content': message.get('content'),
            'tool_calls': [
                {
                    'id': call['id'],
                    'name': call['function']['name'],
                    'arguments': json.loads(call['function']['arguments'] or '{}'),
                }
                for call in message.get('tool_calls') or []
            ],
        }
