"""配置校验 Schema（对标 dsh 使用的 schemastery）。

插件通过类属性 ``Config`` 声明自己的配置结构::

    class Config(Schema):
        api_key = Schema.string().required().description('API 密钥')
        model = Schema.string().default('deepseek-chat')

    class MyService(Service):
        Config = Config

框架在插件启动前调用 ``Config.validate(raw)``，校验失败会抛出
:class:`~cordis.utils.ValidationError`，并聚合全部问题（而不是只报第一条），
这正是 dsh 教程第 5 章强调的 "输入错误时明确报错"。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from .utils import ValidationError

__all__ = ['Schema', 'SchemaMeta', 'Issue', 'MISSING', 'as_schema', 'validate_config']

MISSING = object()


class Issue:
    """一条校验问题。"""

    __slots__ = ('message', 'path')

    def __init__(self, message: str, path: Iterable[Any] = ()) -> None:
        self.message = message
        self.path = list(path)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'Issue({self.message!r}, path={self.path!r})'


def _type_name(value: Any) -> str:
    return type(value).__name__


class SchemaMeta(type):
    """让 schema 既可以函数式组合，也可以类声明式定义::

        class Config(Schema):
            api_key = Schema.string().required()
            model = Schema.string().default('deepseek-chat')
    """

    def __new__(mcls, name: str, bases: tuple, namespace: dict) -> Any:
        fields: dict[str, 'Schema'] = {}
        for base in bases:
            fields.update(getattr(base, '_schema_fields', None) or {})
        schema_cls = globals().get('Schema')
        if schema_cls is not None:
            for key, value in namespace.items():
                if isinstance(value, schema_cls) and not key.startswith('_'):
                    fields[key] = value
        cls = super().__new__(mcls, name, bases, namespace)
        cls._schema_fields = fields
        return cls


class _ValidateDescriptor:
    """让类声明式配置也能直接校验：``Config.validate(value)``。

    - 通过类访问（``Config.validate``）时，用类的 ``_schema_fields`` 组装
      ``ObjectSchema`` 校验；
    - 通过实例访问（``Schema.string().validate``）时，走实例自身的校验逻辑。
    """

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        if instance is None:
            def validate_class(value: Any) -> Any:
                schema = ObjectSchema(dict(getattr(owner, '_schema_fields', None) or {}))
                return schema._validate_instance(value)
            return validate_class
        return instance._validate_instance


class Schema(metaclass=SchemaMeta):
    """配置校验节点。所有链式方法都会返回 **新的** 实例，因此 schema 可安全复用。"""

    kind = 'any'
    expected = 'any'

    def __init__(self, **meta: Any) -> None:
        self._default: Any = MISSING
        self._required = False
        self._description: str | None = None
        self._transform: Callable[[Any], Any] | None = None
        for key, value in meta.items():
            setattr(self, f'_{key}', value)

    # ------------------------------------------------------------------ 构建器
    def _clone(self, **changes: Any) -> 'Schema':
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone.__dict__.update(changes)
        return clone

    def default(self, value: Any) -> 'Schema':
        return self._clone(_default=value)

    def required(self) -> 'Schema':
        return self._clone(_required=True)

    def description(self, text: str) -> 'Schema':
        return self._clone(_description=text)

    def transform(self, fn: Callable[[Any], Any]) -> 'Schema':
        return self._clone(_transform=fn)

    @property
    def description_text(self) -> str | None:
        return self._description

    # ------------------------------------------------------------------ 校验
    def _validate_instance(self, value: Any) -> Any:
        """校验并归一化配置；失败时抛出聚合了全部问题的 ``ValidationError``。"""
        issues: list[Issue] = []
        result = self._validate_field(value, [], issues)
        if issues:
            raise ValidationError(issues)
        return result

    def _validate_field(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        """校验单个字段并应用 ``transform``（容器 schema 用它递归校验子字段）。"""
        result = self._validate(value, path, issues)
        if result is MISSING:
            return MISSING
        if self._transform is not None:
            return self._transform(result)
        return result

    def _resolve_missing(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        """处理 ``None`` / 缺省值：返回 ``MISSING`` 表示该字段不进入结果。"""
        if value is None or value is MISSING:
            if self._default is not MISSING:
                return self._default
            if self._required:
                issues.append(Issue('missing required value', path))
            return MISSING
        return value

    def _validate(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        value = self._resolve_missing(value, path, issues)
        if value is MISSING:
            return MISSING
        return value

    # ------------------------------------------------------------------ 工具
    #: 校验入口：``Config.validate(value)``（类）与 ``schema.validate(value)``（实例）
    validate = _ValidateDescriptor()

    @staticmethod
    def merge(*configs: Any) -> Any:
        """浅合并多个配置对象（服务拦截配置会用到）。"""
        result: dict[Any, Any] = {}
        for config in configs:
            if config is None:
                continue
            if isinstance(config, dict):
                result.update(config)
            else:
                result = config
        return result

    @staticmethod
    def any() -> 'Schema':
        return AnySchema()

    @staticmethod
    def string() -> 'Schema':
        return StringSchema()

    @staticmethod
    def number() -> 'Schema':
        return NumberSchema()

    @staticmethod
    def integer() -> 'Schema':
        return IntegerSchema()

    @staticmethod
    def boolean() -> 'Schema':
        return BooleanSchema()

    @staticmethod
    def array(inner: Schema | None = None) -> 'Schema':
        return ArraySchema(inner=inner)

    @staticmethod
    def object(fields: dict[str, Schema] | None = None) -> 'Schema':
        return ObjectSchema(fields=fields or {})

    @staticmethod
    def dict_(values: Schema | None = None) -> 'Schema':
        return DictSchema(inner=values)

    @staticmethod
    def const(value: Any) -> 'Schema':
        return ConstSchema(value=value)

    @staticmethod
    def union(*options: Schema) -> 'Schema':
        return UnionSchema(options=list(options))

    @staticmethod
    def callable_() -> 'Schema':
        return CallableSchema()


class AnySchema(Schema):
    kind = 'any'


class StringSchema(Schema):
    kind = 'string'
    expected = 'a string'

    def __init__(self, **meta: Any) -> None:
        super().__init__(**meta)
        self._min: int | None = meta.get('min')
        self._max: int | None = meta.get('max')

    def min(self, value: int) -> 'StringSchema':
        return self._clone(_min=value)

    def max(self, value: int) -> 'StringSchema':
        return self._clone(_max=value)

    def _validate(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        value = self._resolve_missing(value, path, issues)
        if value is MISSING:
            return MISSING
        if not isinstance(value, str):
            issues.append(Issue(f'expected a string but got {_type_name(value)}', path))
            return MISSING
        if self._min is not None and len(value) < self._min:
            issues.append(Issue(f'expected a string of length >= {self._min}', path))
        if self._max is not None and len(value) > self._max:
            issues.append(Issue(f'expected a string of length <= {self._max}', path))
        return value


class NumberSchema(Schema):
    kind = 'number'
    expected = 'a number'

    def __init__(self, **meta: Any) -> None:
        super().__init__(**meta)
        self._min: float | None = meta.get('min')
        self._max: float | None = meta.get('max')

    def min(self, value: float) -> 'NumberSchema':
        return self._clone(_min=value)

    def max(self, value: float) -> 'NumberSchema':
        return self._clone(_max=value)

    def _validate(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        value = self._resolve_missing(value, path, issues)
        if value is MISSING:
            return MISSING
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(Issue(f'expected a number but got {_type_name(value)}', path))
            return MISSING
        if self._min is not None and value < self._min:
            issues.append(Issue(f'expected a number >= {self._min}', path))
        if self._max is not None and value > self._max:
            issues.append(Issue(f'expected a number <= {self._max}', path))
        return value


class IntegerSchema(NumberSchema):
    kind = 'integer'
    expected = 'an integer'

    def _validate(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        result = super()._validate(value, path, issues)
        if result is MISSING:
            return MISSING
        if not isinstance(result, int):
            issues.append(Issue(f'expected an integer but got {_type_name(result)}', path))
            return MISSING
        return result


class BooleanSchema(Schema):
    kind = 'boolean'
    expected = 'a boolean'

    def _validate(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        value = self._resolve_missing(value, path, issues)
        if value is MISSING:
            return MISSING
        if not isinstance(value, bool):
            issues.append(Issue(f'expected a boolean but got {_type_name(value)}', path))
            return MISSING
        return value


class CallableSchema(Schema):
    kind = 'callable'
    expected = 'a callable'

    def _validate(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        value = self._resolve_missing(value, path, issues)
        if value is MISSING:
            return MISSING
        if not callable(value):
            issues.append(Issue(f'expected a callable but got {_type_name(value)}', path))
            return MISSING
        return value


class ArraySchema(Schema):
    kind = 'array'
    expected = 'an array'

    def __init__(self, inner: Schema | None = None, **meta: Any) -> None:
        super().__init__(**meta)
        self._inner = inner

    def _validate(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        value = self._resolve_missing(value, path, issues)
        if value is MISSING:
            return MISSING
        if not isinstance(value, list):
            issues.append(Issue(f'expected an array but got {_type_name(value)}', path))
            return MISSING
        if self._inner is None:
            return list(value)
        result = []
        for index, item in enumerate(value):
            validated = self._inner._validate_field(item, path + [index], issues)
            if validated is not MISSING:
                result.append(validated)
        return result


class ObjectSchema(Schema):
    kind = 'object'
    expected = 'an object'

    def __init__(self, fields: dict[str, Schema] | None = None, **meta: Any) -> None:
        super().__init__(**meta)
        self._fields = dict(fields or {})

    @property
    def fields(self) -> dict[str, Schema]:
        return self._fields

    def _validate(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        value = self._resolve_missing(value, path, issues)
        if value is MISSING:
            return MISSING
        if not isinstance(value, dict):
            issues.append(Issue(f'expected an object but got {_type_name(value)}', path))
            return MISSING
        result: dict[str, Any] = {key: item for key, item in value.items() if key not in self._fields}
        for key, field in self._fields.items():
            validated = field._validate_field(value.get(key, MISSING), path + [key], issues)
            if validated is not MISSING:
                result[key] = validated
        return result


class DictSchema(Schema):
    kind = 'dict'
    expected = 'an object'

    def __init__(self, inner: Schema | None = None, **meta: Any) -> None:
        super().__init__(**meta)
        self._inner = inner

    def _validate(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        value = self._resolve_missing(value, path, issues)
        if value is MISSING:
            return MISSING
        if not isinstance(value, dict):
            issues.append(Issue(f'expected an object but got {_type_name(value)}', path))
            return MISSING
        if self._inner is None:
            return dict(value)
        result = {}
        for key, item in value.items():
            validated = self._inner._validate_field(item, path + [key], issues)
            if validated is not MISSING:
                result[key] = validated
        return result


class ConstSchema(Schema):
    kind = 'const'

    def __init__(self, value: Any = None, **meta: Any) -> None:
        super().__init__(**meta)
        self._value = value

    def _validate(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        value = self._resolve_missing(value, path, issues)
        if value is MISSING:
            return MISSING
        if value != self._value:
            issues.append(Issue(f'expected {self._value!r} but got {value!r}', path))
            return MISSING
        return value


class UnionSchema(Schema):
    kind = 'union'

    def __init__(self, options: list[Schema] | None = None, **meta: Any) -> None:
        super().__init__(**meta)
        self._options = list(options or [])

    def _validate(self, value: Any, path: list[Any], issues: list[Issue]) -> Any:
        value = self._resolve_missing(value, path, issues)
        if value is MISSING:
            return MISSING
        if not self._options:
            return value
        collected: list[Issue] = []
        for option in self._options:
            option_issues: list[Issue] = []
            result = option._validate_field(value, list(path), option_issues)
            if not option_issues:
                return result
            collected.extend(option_issues)
        expected = ' or '.join(option.expected for option in self._options)
        issues.append(Issue(f'expected {expected}', path))
        return MISSING


def as_schema(schema: Any) -> Schema:
    """把“类声明式”的配置转换为可校验的 schema 实例。"""
    if isinstance(schema, type) and issubclass(schema, Schema):
        return ObjectSchema(dict(getattr(schema, '_schema_fields', None) or {}))
    return schema


def validate_config(schema: Any, value: Any) -> Any:
    """校验一份插件配置，兼容实例 schema 与类声明式 schema。"""
    schema = as_schema(schema)
    validate = getattr(schema, 'validate', None)
    if validate is None:
        return value
    return validate(value)
