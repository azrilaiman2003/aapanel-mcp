# coding: utf-8
"""Helpers shared by the tool modules: schema shorthand, result checking, lookups."""

from ..panel_client import result_failed, result_message
from ..registry import ToolError


# ------------------------------------------------------------------ schema DSL

def obj(properties=None, required=(), additional=False):
    schema = {'type': 'object', 'properties': properties or {}}
    if required:
        schema['required'] = list(required)
    if not additional:
        schema['additionalProperties'] = False
    return schema


def string(description, **extra):
    schema = {'type': 'string', 'description': description}
    schema.update(extra)
    return schema


def integer(description, **extra):
    schema = {'type': 'integer', 'description': description}
    schema.update(extra)
    return schema


def boolean(description, **extra):
    schema = {'type': 'boolean', 'description': description}
    schema.update(extra)
    return schema


def array(description, items, **extra):
    schema = {'type': 'array', 'description': description, 'items': items}
    schema.update(extra)
    return schema


NO_ARGS = obj()

PAGE_ARGS = {
    'search': string('Filter by name. Leave empty for everything.', default=''),
    'page': integer('Page number, 1-based.', default=1, minimum=1),
    'limit': integer('Rows per page.', default=100, minimum=1, maximum=1000),
}


# --------------------------------------------------------------- result helpers

def expect(result, what):
    """Turn the panel's `{status: false, msg: ...}` into a ToolError the model can read."""
    if result_failed(result):
        raise ToolError('%s failed: %s' % (what, result_message(result, 'the panel gave no reason')))
    return result


def ok(message, **extra):
    payload = {'status': 'ok', 'message': message}
    payload.update(extra)
    return payload


def rows_of(result):
    """Normalise the panel's list endpoints, which return either a list or {data: [...]}."""
    if isinstance(result, dict):
        for key in ('data', 'list', 'rows'):
            if isinstance(result.get(key), list):
                return result[key]
        return []
    if isinstance(result, list):
        return result
    return []


def pick(row, *fields):
    return {field: row.get(field) for field in fields if field in row}


def paged(panel, table, args, fields=None, type_=-1):
    """A /data?action=getData listing, trimmed to the fields worth showing a model."""
    result = panel.get_data(table,
                            page=args.get('page', 1),
                            limit=args.get('limit', 100),
                            search=args.get('search', ''),
                            type_=type_)
    rows = rows_of(result)
    if fields:
        rows = [pick(row, *fields) for row in rows]
    payload = {'count': len(rows), table: rows}
    if isinstance(result, dict) and result.get('page'):
        payload['pagination'] = _strip_html(result['page'])
    return payload


def _strip_html(page_html):
    """The panel returns pagination as an HTML blob; keep only the totals line."""
    import re
    text = re.sub(r'<[^>]+>', ' ', str(page_html))
    return ' '.join(text.split())[:200]


# ------------------------------------------------------------------- resolvers

def _find_row(panel, table, needle, name_fields):
    needle = str(needle).strip()
    result = panel.get_data(table, page=1, limit=500, search=needle)
    rows = rows_of(result)
    if not rows:
        result = panel.get_data(table, page=1, limit=1000)
        rows = rows_of(result)
    for row in rows:
        if str(row.get('id')) == needle:
            return row
        for field in name_fields:
            if str(row.get(field, '')).lower() == needle.lower():
                return row
    return None


def resolve_site(panel, site):
    """Accept a site id, its panel name, or one of its domains."""
    row = _find_row(panel, 'sites', site, ('name', 'ps'))
    if row:
        return row
    # Not the primary name: look through the bound domains.
    domains = rows_of(panel.get_data('domain', page=1, limit=1000, search=str(site)))
    for domain in domains:
        if str(domain.get('name', '')).lower() == str(site).lower():
            parent = _find_row(panel, 'sites', str(domain.get('pid')), ('name',))
            if parent:
                return parent
    raise ToolError('No website matches "%s". Use site_list to see what exists.' % site)


def resolve_database(panel, database):
    row = _find_row(panel, 'databases', database, ('name', 'username'))
    if row:
        return row
    raise ToolError('No database matches "%s". Use database_list to see what exists.' % database)


def resolve_ftp(panel, user):
    row = _find_row(panel, 'ftps', user, ('name', 'username'))
    if row:
        return row
    raise ToolError('No FTP user matches "%s". Use ftp_list to see what exists.' % user)


def resolve_cron(panel, task):
    rows = rows_of(panel.call('/crontab', 'GetCrontab'))
    needle = str(task).strip()
    for row in rows:
        if str(row.get('id')) == needle or str(row.get('name', '')).lower() == needle.lower():
            return row
    raise ToolError('No cron task matches "%s". Use cron_list to see what exists.' % task)
