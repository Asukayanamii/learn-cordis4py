"""第 5 章 · 配置与 Schema：在启动前把错误配置拦下来。

本章聚焦一件事：**插件声明配置结构，框架在插件启动前校验**。
没有校验的配置系统会有两种坏结局：要么带着错误跑、要么在深水区崩溃。

本章实现（约 120 行，自包含）：

- ``Schema``：声明式校验器（string/number/integer/boolean/array/object/union/const），
  支持 ``default`` / ``required`` / ``transform``；
- ``validate_config``：聚合**全部**问题后一次性报错（而不是只报第一条）；
- ``FiberLite``：加载前校验配置，支持 ``update()`` 运行时改配置并重启。

对照仓库里的完整实现：``cordis/schema.py``（约 450 行，多了类声明式 schema、
路径化错误、dict/union 嵌套等）；``cordis/fiber.py`` 的 ``_resolve_config``。

运行：python tutorial/ch05_config.py
"""

from __future__ import annotations

import asyncio


# ============================================================== Schema：声明 + 校验
class Issue:
    def __init__(self, message, path=()):
        self.message = message
        self.path = list(path)


MISSING = object()


class ValidationError(TypeError):
    """聚合了全部校验问题的错误。"""

    def __init__(self, issues):
        lines = []
        for issue in issues:
            where = f'（位于 {".".join(map(str, issue.path))}）' if issue.path else ''
            lines.append(f'  - {issue.message}{where}')
        self.issues = list(issues)
        super().__init__('invalid config:\n' + '\n'.join(lines))


class Schema:
    """一个校验节点。链式方法返回新实例，因此 schema 可以安全复用。"""

    kind = 'any'
    expected = '任意值'

    def __init__(self):
        self.default = MISSING
        self.required = False
        self.transform_fn = None

    # ---------------------------------------------------------- 构建器
    def _clone(self):
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        return clone

    def with_default(self, value):
        clone = self._clone()
        clone.default = value
        return clone

    def as_required(self):
        clone = self._clone()
        clone.required = True
        return clone

    def transform(self, fn):
        clone = self._clone()
        clone.transform_fn = fn
        return clone

    # ---------------------------------------------------------- 校验
    def validate(self, value):
        issues = []
        result = self._check(value, [], issues)
        if issues:
            raise ValidationError(issues)
        return result

    def _check(self, value, path, issues):
        if value is None or value is MISSING:
            if self.default is not MISSING:
                value = self.default
            elif self.required:
                issues.append(Issue('缺少必填项', path))
                return MISSING
            else:
                return MISSING
        result = self._type_check(value, path, issues)
        if result is MISSING:
            return MISSING
        return self.transform_fn(result) if self.transform_fn else result

    def _type_check(self, value, path, issues):
        return value

    # ---------------------------------------------------------- 工厂
    @staticmethod
    def string():
        return StringSchema()

    @staticmethod
    def number():
        return NumberSchema()

    @staticmethod
    def integer():
        return IntegerSchema()

    @staticmethod
    def boolean():
        return BooleanSchema()

    @staticmethod
    def array(inner=None):
        return ArraySchema(inner)

    @staticmethod
    def object(fields=None):
        return ObjectSchema(fields or {})

    @staticmethod
    def union(*options):
        return UnionSchema(list(options))


class StringSchema(Schema):
    kind, expected = 'string', '一个字符串'

    def _type_check(self, value, path, issues):
        if not isinstance(value, str):
            issues.append(Issue(f'期望字符串，实际是 {type(value).__name__}', path))
            return MISSING
        return value


class NumberSchema(Schema):
    kind, expected = 'number', '一个数字'

    def _type_check(self, value, path, issues):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(Issue(f'期望数字，实际是 {type(value).__name__}', path))
            return MISSING
        return value


class IntegerSchema(NumberSchema):
    kind, expected = 'integer', '一个整数'

    def _type_check(self, value, path, issues):
        result = super()._type_check(value, path, issues)
        if result is MISSING:
            return MISSING
        if not isinstance(result, int):
            issues.append(Issue(f'期望整数，实际是 {type(result).__name__}', path))
            return MISSING
        return result


class BooleanSchema(Schema):
    kind, expected = 'boolean', '一个布尔值'

    def _type_check(self, value, path, issues):
        if not isinstance(value, bool):
            issues.append(Issue(f'期望布尔值，实际是 {type(value).__name__}', path))
            return MISSING
        return value


class ArraySchema(Schema):
    kind, expected = 'array', '一个列表'

    def __init__(self, inner=None):
        super().__init__()
        self.inner = inner

    def _type_check(self, value, path, issues):
        if not isinstance(value, list):
            issues.append(Issue(f'期望列表，实际是 {type(value).__name__}', path))
            return MISSING
        if self.inner is None:
            return list(value)
        result = []
        for index, item in enumerate(value):
            checked = self.inner._check(item, path + [index], issues)
            if checked is not MISSING:
                result.append(checked)
        return result


class ObjectSchema(Schema):
    kind, expected = 'object', '一个对象'

    def __init__(self, fields=None):
        super().__init__()
        self.fields = dict(fields or {})

    def _type_check(self, value, path, issues):
        if not isinstance(value, dict):
            issues.append(Issue(f'期望对象，实际是 {type(value).__name__}', path))
            return MISSING
        # 未声明的键原样保留（宽松策略；也可以选择报错）
        result = {key: item for key, item in value.items() if key not in self.fields}
        for key, field in self.fields.items():
            checked = field._check(value.get(key, MISSING), path + [key], issues)
            if checked is not MISSING:
                result[key] = checked
        return result


class UnionSchema(Schema):
    kind, expected = 'union', '联合类型'

    def __init__(self, options):
        super().__init__()
        self.options = options

    def _type_check(self, value, path, issues):
        for option in self.options:
            attempts = []
            result = option._check(value, list(path), attempts)
            if not attempts:
                return result
        issues.append(Issue('期望 ' + ' 或 '.join(o.expected for o in self.options), path))
        return MISSING


# ============================================================== 最小 Fiber：带配置校验
class State:
    PENDING = 'PENDING'
    LOADING = 'LOADING'
    ACTIVE = 'ACTIVE'
    DISPOSED = 'DISPOSED'
    FAILED = 'FAILED'


class FiberLite:
    """只保留与"配置"有关的部分：校验 → 加载 → 可更新。"""

    def __init__(self, name, plugin, config=None):
        self.name = name
        self.plugin = plugin            # 插件主体：callable(ctx, config)
        self.schema = getattr(plugin, 'Config', None)
        self.raw_config = config
        self.config = None
        self.state = State.PENDING
        self.error = None
        self.loaded = []

    def start(self):
        """校验配置并加载；失败则进入 FAILED 并把错误抛出。"""
        try:
            self.config = self.schema.validate(self.raw_config) if self.schema else self.raw_config
            result = self.plugin(self.config)
            if asyncio.iscoroutine(result):
                raise TypeError('本示例的插件主体应为同步函数')
            self.state = State.ACTIVE
        except Exception as error:
            self.error = error
            self.state = State.FAILED
        return self

    def update(self, config):
        """运行时改配置：先校验（失败保持原状），再重启插件。"""
        validated = self.schema.validate(config) if self.schema else config   # 先校验，再动任何东西
        self.raw_config = config
        print(f'  [update] 停止 {self.name}（当前配置 {self.config}）')
        self.state = State.PENDING
        self.config = validated
        self.plugin(self.config)        # 重新执行插件主体（真实框架里会先卸载再加载）
        self.state = State.ACTIVE
        print(f'  [update] 重启完成，新配置 {self.config}')


# ============================================================== 模拟插件的加载过程
def make_plugin(config):
    """插件主体：用配置做它该做的事。真实插件通过 ctx 注册、返回清理函数。"""
    print(f'  [plugin] 启动，model={config["model"]} max_tokens={config["max_tokens"]}'
          f' tags={config.get("tags", [])}')
    return lambda: print('  [plugin] 清理')


def accepts_config(plugin):
    import inspect
    return len([p for p in inspect.signature(plugin).parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) >= 1


# ============================================================== 配置声明
ApiConfig = Schema.object({
    'api_key': Schema.string().as_required(),
    'model': Schema.string().with_default('deepseek-chat'),
    'max_tokens': Schema.integer().with_default(1024),
    'retries': Schema.number().with_default(2),
    'tags': Schema.array(Schema.string()).with_default([]),
    'mode': Schema.union(Schema.string(), Schema.number()).with_default('auto'),
    # transform：先校验类型，再转换；union 让它同时接受 '8080' 与 8080
    'port': Schema.union(Schema.string().transform(int), Schema.integer()),
})


def demo_plugin(config):
    return make_plugin(config)


demo_plugin.Config = ApiConfig


# ---------------------------------------------------------------------- 演示
if __name__ == '__main__':
    print('--- 1) 正确配置：缺省值自动补全，transform 生效 ---')
    fiber = FiberLite('demo', demo_plugin, {'api_key': 'sk-demo', 'port': '8080'}).start()
    print('  校验后的配置 =', fiber.config)

    print('--- 2) 错误配置：一次报出全部问题 ---')
    broken = FiberLite('broken', demo_plugin, {'api_key': 123, 'max_tokens': '很多', 'tags': 'x'}).start()
    print('  ', type(broken.error).__name__)
    for line in str(broken.error).splitlines():
        print('  ', line)
    print('  状态 =', broken.state)

    print('--- 3) 运行时更新配置：先校验，失败则保持原状 ---')
    fiber.update({'api_key': 'sk-demo', 'model': 'deepseek-reasoner', 'max_tokens': 4096, 'port': 9090})
    print('  当前配置 =', fiber.config)

    print('--- 4) 更新失败：原配置不受影响 ---')
    try:
        fiber.update({'api_key': 'sk-demo', 'max_tokens': 'oops'})
    except ValidationError as error:
        print('  校验失败:', str(error).splitlines()[1].strip())
    print('  仍在运行的配置 =', fiber.config)

    print('\n本章完。下一章：把一堆插件用 YAML 组合成"应用"。')
