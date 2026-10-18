"""Local validation for the JSON Schema subset emitted by pipeline contracts.

Unsupported keywords fail closed. Stage-specific Pydantic validation still
checks domain constraints and references after this structural check.
"""
from __future__ import annotations

import re
import json


def parse_json(content: str):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError('chave JSON repetida')
            result[name] = value
        return result
    def constant(_):
        raise ValueError('constante JSON inválida')
    return json.loads(content, object_pairs_hook=pairs, parse_constant=constant)


def validate_json(value, schema: dict, *, root=None):
    root = schema if root is None else root
    known = {'type', 'properties', 'required', 'additionalProperties', 'items',
        'enum', 'const', 'anyOf', 'oneOf', 'allOf', '$ref', '$defs',
        'title', 'description', 'default', 'minItems', 'maxItems', 'minLength',
        'maxLength', 'minimum', 'maximum', 'pattern', 'uniqueItems'}
    if schema.keys() - known:
        raise ValueError('schema contém restrições não suportadas localmente')
    if '$ref' in schema:
        if not schema['$ref'].startswith('#/'):
            raise ValueError('referência externa de schema não suportada')
        target = root
        for part in schema['$ref'][2:].split('/'):
            target = target[part.replace('~1', '/').replace('~0', '~')]
        validate_json(value, target, root=root)
    for operator in ('anyOf', 'oneOf', 'allOf'):
        if operator in schema:
            passed = 0
            for candidate in schema[operator]:
                try:
                    validate_json(value, candidate, root=root)
                    passed += 1
                except ValueError:
                    pass
            if (operator == 'anyOf' and not passed or operator == 'oneOf' and passed != 1
                or operator == 'allOf' and passed != len(schema[operator])):
                raise ValueError('alternativa de schema inválida')
    types = {'object': type(value) is dict, 'array': type(value) is list,
        'string': type(value) is str, 'integer': type(value) is int,
        'number': type(value) in (int, float), 'boolean': type(value) is bool,
        'null': value is None}
    if 'type' in schema:
        expected = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        if not any(types.get(kind, False) for kind in expected):
            raise ValueError('tipo JSON inválido')
    if 'enum' in schema and not any(type(value) is type(item) and value == item for item in schema['enum']):
        raise ValueError('enum inválido')
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        raise ValueError('constante inválida')
    if type(value) is dict:
        properties = schema.get('properties', {})
        if not set(schema.get('required', ())) <= value.keys():
            raise ValueError('campos obrigatórios ausentes')
        for name, item in value.items():
            child = properties.get(name, schema.get('additionalProperties', True))
            if child is False:
                raise ValueError('campo adicional inválido')
            if isinstance(child, dict):
                validate_json(item, child, root=root)
    if type(value) is list:
        for item in value:
            validate_json(item, schema.get('items', {}), root=root)
        if schema.get('uniqueItems') and any(value[i] == value[j] for i in range(len(value)) for j in range(i)):
            raise ValueError('itens repetidos')
    for low, high in (('minItems', 'maxItems'), ('minLength', 'maxLength')):
        if low in schema and len(value) < schema[low] or high in schema and len(value) > schema[high]:
            raise ValueError('tamanho inválido')
    if ('minimum' in schema and value < schema['minimum'] or
        'maximum' in schema and value > schema['maximum']):
        raise ValueError('valor fora dos limites')
    if 'pattern' in schema and not re.search(schema['pattern'], value):
        raise ValueError('padrão inválido')
