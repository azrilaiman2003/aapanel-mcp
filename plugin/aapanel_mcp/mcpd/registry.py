# coding: utf-8
"""Tool definitions, argument validation, and the call context.

A tool is a name, a JSON Schema, a permission tier and a handler. Handlers get a
`ToolContext` (panel client + config + progress callback) and the validated
arguments, and return plain Python data; the protocol layer turns that into MCP
content blocks.

The schema validator here covers the JSON Schema subset these tools actually use
(type/required/properties/enum/default/min/max/pattern/items/additionalProperties).
Pulling in `jsonschema` would mean a pip install inside the panel's pyenv, which is
exactly what this plugin avoids.
"""

import re

# Every handler declares one of these. See mcpd/permissions.py for how they gate.
TIER_READ = 'read'
TIER_WRITE = 'write'
TIER_DESTRUCTIVE = 'destructive'
TIER_SHELL = 'shell'
TIER_RAW = 'raw'


class ToolError(Exception):
    """An execution failure worth showing the model so it can retry differently."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(ToolError):
    pass


class ToolContext:
    """Everything a handler is allowed to touch."""

    def __init__(self, panel, config, registry, audit=None, client_info=None, progress=None):
        self.panel = panel
        self.config = config
        self.registry = registry
        self.audit = audit
        self.client_info = client_info or {}
        self._progress = progress

    def progress(self, message, current=None, total=None):
        """Emit a progress notification when the transport supports one."""
        if self._progress:
            self._progress(message, current, total)


class Tool:
    def __init__(self, name, handler, description, input_schema, tier,
                 title=None, domain='general', annotations=None, output_schema=None,
                 available=None):
        self.name = name
        self.handler = handler
        self.description = description
        self.input_schema = input_schema
        self.tier = tier
        self.title = title or name.replace('_', ' ').title()
        self.domain = domain
        self.output_schema = output_schema
        # Callable(panel) -> bool, for tools that only exist when some app is installed.
        self.available = available

        defaults = {
            'readOnlyHint': tier == TIER_READ,
            'destructiveHint': tier in (TIER_DESTRUCTIVE, TIER_SHELL),
            'idempotentHint': tier == TIER_READ,
            'openWorldHint': False,
        }
        defaults.update(annotations or {})
        self.annotations = defaults

    def definition(self):
        payload = {
            'name': self.name,
            'title': self.title,
            'description': self.description,
            'inputSchema': self.input_schema,
            'annotations': self.annotations,
        }
        if self.output_schema:
            payload['outputSchema'] = self.output_schema
        return payload

    def is_available(self, panel):
        if self.available is None:
            return True
        try:
            return bool(self.available(panel))
        except Exception:
            return False


class Registry:
    def __init__(self):
        self._tools = {}

    def add(self, tool):
        if tool.name in self._tools:
            raise ValueError('duplicate tool name: %s' % tool.name)
        self._tools[tool.name] = tool
        return tool

    def tool(self, name, description, tier, schema=None, title=None, domain='general',
             annotations=None, output_schema=None, available=None):
        """Decorator form: @registry.tool('site_list', 'List websites', TIER_READ)."""

        def decorator(handler):
            self.add(Tool(
                name=name,
                handler=handler,
                description=description,
                input_schema=schema or {'type': 'object', 'additionalProperties': False},
                tier=tier,
                title=title,
                domain=domain,
                annotations=annotations,
                output_schema=output_schema,
                available=available,
            ))
            return handler

        return decorator

    def get(self, name):
        return self._tools.get(name)

    def all(self):
        return [self._tools[name] for name in sorted(self._tools)]

    def domains(self):
        seen = []
        for tool in self.all():
            if tool.domain not in seen:
                seen.append(tool.domain)
        return seen

    def __len__(self):
        return len(self._tools)


# ------------------------------------------------------------------ validation

_TYPE_CHECKS = {
    'object': lambda v: isinstance(v, dict),
    'array': lambda v: isinstance(v, list),
    'string': lambda v: isinstance(v, str),
    'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
    'number': lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    'boolean': lambda v: isinstance(v, bool),
    'null': lambda v: v is None,
}


def _type_matches(value, expected):
    types = expected if isinstance(expected, list) else [expected]
    for name in types:
        check = _TYPE_CHECKS.get(name)
        if check and check(value):
            return True
        # JSON has one number type; accept an int where a number is asked for.
        if name == 'number' and isinstance(value, int) and not isinstance(value, bool):
            return True
    return False


def validate(schema, value, path='arguments'):
    """Validate `value` against `schema`, returning a copy with defaults applied.

    Raises ValidationError with a message aimed at the model, not at a developer.
    """
    if not isinstance(schema, dict) or not schema:
        return value

    expected = schema.get('type')
    if expected and not _type_matches(value, expected):
        raise ValidationError('%s must be of type %s, got %s'
                              % (path, expected, type(value).__name__))

    if 'enum' in schema and value not in schema['enum']:
        raise ValidationError('%s must be one of: %s'
                              % (path, ', '.join(repr(v) for v in schema['enum'])))

    if isinstance(value, str):
        if 'minLength' in schema and len(value) < schema['minLength']:
            raise ValidationError('%s must be at least %d characters'
                                  % (path, schema['minLength']))
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            raise ValidationError('%s must be at most %d characters'
                                  % (path, schema['maxLength']))
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            raise ValidationError('%s does not match the required format (%s)'
                                  % (path, schema['pattern']))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            raise ValidationError('%s must be >= %s' % (path, schema['minimum']))
        if 'maximum' in schema and value > schema['maximum']:
            raise ValidationError('%s must be <= %s' % (path, schema['maximum']))

    if isinstance(value, list) and 'items' in schema:
        return [validate(schema['items'], item, '%s[%d]' % (path, index))
                for index, item in enumerate(value)]

    if isinstance(value, dict) and schema.get('type') == 'object':
        properties = schema.get('properties') or {}
        result = {}

        for key in schema.get('required') or []:
            if key not in value or value[key] is None:
                raise ValidationError('%s.%s is required' % (path, key))

        if schema.get('additionalProperties') is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ValidationError(
                    '%s has unknown field(s): %s. Allowed: %s'
                    % (path, ', '.join(unknown), ', '.join(sorted(properties)) or 'none'))

        for key, sub_schema in properties.items():
            if key in value and value[key] is not None:
                result[key] = validate(sub_schema, value[key], '%s.%s' % (path, key))
            elif 'default' in sub_schema:
                result[key] = sub_schema['default']

        for key, item in value.items():
            if key not in properties and key not in result:
                result[key] = item
        return result

    return value
