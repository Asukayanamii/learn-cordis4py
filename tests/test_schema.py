"""schema 校验测试。"""

import pytest

from cordis import Schema, ValidationError


def test_string_required_and_default():
    schema = Schema.object({
        'name': Schema.string().required(),
        'model': Schema.string().default('deepseek-chat'),
    })
    assert schema.validate({'name': 'x'}) == {'name': 'x', 'model': 'deepseek-chat'}

    with pytest.raises(ValidationError) as info:
        schema.validate({})
    assert 'missing required value' in str(info.value)
    assert 'name' in str(info.value)


def test_type_errors_aggregate_all_issues():
    schema = Schema.object({
        'a': Schema.string(),
        'b': Schema.number(),
    })
    with pytest.raises(ValidationError) as info:
        schema.validate({'a': 1, 'b': 'x'})
    message = str(info.value)
    assert 'expected a string' in message
    assert 'expected a number' in message


def test_nested_path_reported():
    schema = Schema.object({'items': Schema.array(Schema.object({'id': Schema.number()}))})
    with pytest.raises(ValidationError) as info:
        schema.validate({'items': [{'id': 1}, {'id': 'x'}]})
    assert 'items.1.id' in str(info.value)


def test_union_and_const_and_boolean():
    schema = Schema.object({
        'mode': Schema.union(Schema.const('auto'), Schema.const('manual')),
        'flag': Schema.boolean().default(False),
    })
    assert schema.validate({'mode': 'auto'}) == {'mode': 'auto', 'flag': False}
    with pytest.raises(ValidationError):
        schema.validate({'mode': 'other'})


def test_transform_and_merge():
    schema = Schema.object({'port': Schema.string().transform(int)})
    assert schema.validate({'port': '8080'}) == {'port': 8080}
    assert Schema.merge({'a': 1}, {'b': 2}) == {'a': 1, 'b': 2}


def test_class_based_config():
    class Config(Schema):
        api_key = Schema.string().required().description('API 密钥')
        model = Schema.string().default('deepseek-reasoner')

    validated = Config.validate({'api_key': 'sk-1'})
    assert validated == {'api_key': 'sk-1', 'model': 'deepseek-reasoner'}

    with pytest.raises(ValidationError):
        Config.validate({})


def test_unknown_keys_are_preserved():
    schema = Schema.object({'known': Schema.number()})
    assert schema.validate({'known': 1, 'extra': 'kept'}) == {'known': 1, 'extra': 'kept'}


def test_optional_fields_are_omitted():
    schema = Schema.object({'a': Schema.string(), 'b': Schema.string().required()})
    assert schema.validate({'b': 'x'}) == {'b': 'x'}
