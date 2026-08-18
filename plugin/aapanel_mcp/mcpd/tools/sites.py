# coding: utf-8
"""Website tools: /site plus the sites table."""

import json

from ..registry import TIER_DESTRUCTIVE, TIER_READ, TIER_WRITE
from .common import (NO_ARGS, PAGE_ARGS, array, boolean, expect, integer, obj, ok, paged,
                     resolve_site, rows_of, string)

DOMAIN = 'sites'
SITE_ARG = string('Website: its domain, its panel name, or its numeric id.')
SITE_FIELDS = ('id', 'name', 'path', 'status', 'ps', 'addtime', 'edate', 'php_version', 'type_id')


def register(registry):
    tool = registry.tool

    # ------------------------------------------------------------------- read

    @tool('site_list', 'List the websites on this server.', TIER_READ,
          schema=obj(dict(PAGE_ARGS)), domain=DOMAIN, title='List websites')
    def site_list(ctx, args):
        return paged(ctx.panel, 'sites', args, SITE_FIELDS)

    @tool('site_info',
          'Everything about one website: paths, bound domains, PHP version, TLS state.',
          TIER_READ, schema=obj({'site': SITE_ARG}, required=['site']),
          domain=DOMAIN, title='Website details')
    def site_info(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        site_id, name = row['id'], row['name']
        info = {'site': {k: row.get(k) for k in SITE_FIELDS if k in row}}

        info['domains'] = _domains(ctx.panel, site_id)
        info['php_version'] = _quiet(ctx.panel, '/site', 'GetSitePHPVersion', siteName=name)
        info['run_path'] = _quiet(ctx.panel, '/site', 'GetSiteRunPath', id=site_id)
        ssl = _quiet(ctx.panel, '/site', 'GetSSL', siteName=name)
        if isinstance(ssl, dict):
            info['ssl'] = {
                'enabled': bool(ssl.get('status')),
                'issuer': ssl.get('issuer'),
                'not_after': ssl.get('notAfter'),
                'subject': ssl.get('subject'),
                'dns': ssl.get('dns'),
                'force_https': bool(ssl.get('httpTohttps')),
            }
        return info

    @tool('site_domains', 'List the domains and subdirectory bindings of a website.',
          TIER_READ, schema=obj({'site': SITE_ARG}, required=['site']),
          domain=DOMAIN, title='List domains')
    def site_domains(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        return {'site': row['name'], 'domains': _domains(ctx.panel, row['id'])}

    @tool('site_php_versions', 'PHP versions installed on this server, for site_create '
                               'and site_set_php_version.',
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='List PHP versions')
    def site_php_versions(ctx, args):
        versions = ctx.panel.call('/site', 'GetPHPVersion')
        return {'versions': versions}

    @tool('site_logs', 'Recent access and error log lines for a website.', TIER_READ,
          schema=obj({
              'site': SITE_ARG,
              'lines': integer('How many lines to return.', default=100, minimum=1, maximum=1000),
          }, required=['site']), domain=DOMAIN, title='Website logs')
    def site_logs(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        access = ctx.panel.call('/site', 'GetSiteLogs', siteName=row['name'])
        errors = _quiet(ctx.panel, '/site', 'get_site_err_log', siteName=row['name'],
                        num=args['lines'])
        return {
            'site': row['name'],
            'access_log': _tail(access, args['lines']),
            'error_log': _tail(errors, args['lines']),
        }

    @tool('site_rewrite_get', 'Read the URL rewrite rules of a website.', TIER_READ,
          schema=obj({'site': SITE_ARG}, required=['site']), domain=DOMAIN,
          title='Read rewrite rules')
    def site_rewrite_get(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        path = _rewrite_path(ctx.panel, row['name'])
        body = ctx.panel.call('/files', 'GetFileBody', path=path)
        return {'site': row['name'], 'path': path,
                'content': body.get('data') if isinstance(body, dict) else body}

    @tool('site_rewrite_templates', 'Rewrite rule templates shipped with the panel '
                                    '(WordPress, Laravel, ThinkPHP and so on).',
          TIER_READ, schema=obj({'site': SITE_ARG}, required=['site']), domain=DOMAIN,
          title='List rewrite templates')
    def site_rewrite_templates(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        return ctx.panel.call('/site', 'GetRewriteList', siteName=row['name'])

    @tool('site_proxy_list', 'Reverse proxies configured on a website.', TIER_READ,
          schema=obj({'site': SITE_ARG}, required=['site']), domain=DOMAIN,
          title='List reverse proxies')
    def site_proxy_list(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        return {'site': row['name'],
                'proxies': rows_of(ctx.panel.call('/site', 'GetProxyList', sitename=row['name']))}

    # ------------------------------------------------------------------ write

    @tool('site_create', 'Create a website. Optionally creates its database and FTP user '
                         'at the same time; both passwords are returned once, so pass them '
                         'on to the user.',
          TIER_WRITE, schema=obj({
              'domain': string('Primary domain, e.g. example.com.'),
              'extra_domains': array('Additional domains bound to the same site.',
                                     {'type': 'string'}, default=[]),
              'path': string('Document root. Defaults to /www/wwwroot/<domain>.', default=''),
              'php_version': string('PHP version id from site_php_versions, or "00" for a '
                                    'static site.', default='00'),
              'port': integer('Listening port.', default=80, minimum=1, maximum=65535),
              'remark': string('Note shown in the panel.', default=''),
              'create_database': boolean('Also create a MySQL database for the site.',
                                         default=False),
              'database_user': string('Database user and name. Defaults to the domain with '
                                      'dots replaced by underscores.', default=''),
              'database_password': string('Database password. Generated when omitted.', default=''),
              'create_ftp': boolean('Also create an FTP account rooted at the site.',
                                    default=False),
              'ftp_user': string('FTP username. Defaults to the domain with dots replaced by '
                                 'underscores.', default=''),
              'ftp_password': string('FTP password. Generated when omitted.', default=''),
          }, required=['domain']), domain=DOMAIN, title='Create website')
    def site_create(ctx, args):
        domain = args['domain'].strip().lower()
        safe_name = domain.replace('.', '_')[:16]
        path = args['path'] or '/www/wwwroot/%s' % domain
        webname = {'domain': domain, 'domainlist': list(args['extra_domains']), 'count': 0}

        params = {
            'webname': json.dumps(webname),
            'path': path,
            'type_id': 0,
            'type': 'PHP',
            'version': args['php_version'],
            'port': args['port'],
            'ps': args['remark'] or domain,
        }
        secrets = {}
        if args['create_database']:
            password = args['database_password'] or _password()
            params.update({'sql': 'MySQL', 'codeing': 'utf8mb4',
                           'datauser': args['database_user'] or safe_name,
                           'datapassword': password})
            secrets['database_password'] = password
        if args['create_ftp']:
            password = args['ftp_password'] or _password()
            params.update({'ftp': 'true',
                           'ftp_username': args['ftp_user'] or safe_name,
                           'ftp_password': password})
            secrets['ftp_password'] = password

        result = expect(ctx.panel.call('/site', 'AddSite', **params), 'Creating the website')
        payload = ok('Website %s created at %s.' % (domain, path), site=domain, path=path,
                     panel_result=result)
        payload.update(secrets)
        return payload

    @tool('site_add_domain', 'Bind another domain to a website.', TIER_WRITE,
          schema=obj({
              'site': SITE_ARG,
              'domain': string('Domain to add. Append ":port" for a non-standard port.'),
          }, required=['site', 'domain']), domain=DOMAIN, title='Add domain')
    def site_add_domain(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        expect(ctx.panel.call('/site', 'AddDomain', id=row['id'], webname=row['name'],
                              domain=args['domain']), 'Adding the domain')
        return ok('%s now serves %s.' % (row['name'], args['domain']))

    @tool('site_set_php_version', 'Switch a website to a different PHP version.', TIER_WRITE,
          schema=obj({
              'site': SITE_ARG,
              'php_version': string('Version id from site_php_versions, e.g. "82".'),
          }, required=['site', 'php_version']), domain=DOMAIN, title='Set PHP version')
    def site_set_php_version(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        expect(ctx.panel.call('/site', 'SetPHPVersion', siteName=row['name'],
                              version=args['php_version']), 'Changing the PHP version')
        return ok('%s now runs PHP %s.' % (row['name'], args['php_version']))

    @tool('site_set_run_path', 'Set the run directory of a website, for frameworks that '
                               'serve from a subfolder such as /public.',
          TIER_WRITE, schema=obj({
              'site': SITE_ARG,
              'run_path': string('Path relative to the document root, e.g. "/public". Use "/" '
                                 'for the document root itself.'),
          }, required=['site', 'run_path']), domain=DOMAIN, title='Set run directory')
    def site_set_run_path(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        run_path = args['run_path']
        if run_path == '/':
            run_path = ''
        expect(ctx.panel.call('/site', 'SetSiteRunPath', id=row['id'], runPath=run_path),
               'Setting the run directory')
        return ok('%s now runs from %s.' % (row['name'], run_path or 'the document root'))

    @tool('site_set_status', 'Start or stop a website.', TIER_WRITE,
          schema=obj({
              'site': SITE_ARG,
              'running': boolean('True to start serving, false to stop.'),
          }, required=['site', 'running']), domain=DOMAIN, title='Start/stop website')
    def site_set_status(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        action = 'SiteStart' if args['running'] else 'SiteStop'
        expect(ctx.panel.call('/site', action, id=row['id'], name=row['name']),
               'Changing the website state')
        return ok('%s is now %s.' % (row['name'], 'running' if args['running'] else 'stopped'))

    @tool('site_rewrite_set', 'Replace the URL rewrite rules of a website.', TIER_WRITE,
          schema=obj({
              'site': SITE_ARG,
              'content': string('The complete rewrite configuration to write.'),
          }, required=['site', 'content']), domain=DOMAIN, title='Write rewrite rules')
    def site_rewrite_set(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        path = _rewrite_path(ctx.panel, row['name'])
        expect(ctx.panel.call('/files', 'SaveFileBody', path=path, data=args['content'],
                              encoding='utf-8'), 'Saving the rewrite rules')
        return ok('Rewrite rules for %s updated. Reload the web server for them to take '
                  'effect (service_restart).' % row['name'], path=path)

    @tool('site_create_proxy', 'Add a reverse proxy to a website, so a path is served by '
                               'another URL.',
          TIER_WRITE, schema=obj({
              'site': SITE_ARG,
              'name': string('Name for this proxy rule.'),
              'target_url': string('Where to proxy to, e.g. http://127.0.0.1:3000.'),
              'proxy_dir': string('Path on the website to proxy.', default='/'),
              'send_host': string('Host header to send upstream. Defaults to the target host.',
                                  default=''),
              'cache': boolean('Enable proxy caching.', default=False),
          }, required=['site', 'name', 'target_url']), domain=DOMAIN, title='Add reverse proxy')
    def site_create_proxy(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        to_domain = args['send_host'] or args['target_url'].split('//')[-1].split('/')[0]
        expect(ctx.panel.call('/site', 'CreateProxy',
                              sitename=row['name'], proxyname=args['name'],
                              proxydir=args['proxy_dir'], proxysite=args['target_url'],
                              todomain=to_domain, type=1, cache=1 if args['cache'] else 0,
                              subfilter=json.dumps([{'sub1': '', 'sub2': ''}]),
                              advanced=0, cachetime=1), 'Creating the reverse proxy')
        return ok('%s%s now proxies to %s.' % (row['name'], args['proxy_dir'], args['target_url']))

    @tool('site_remove_proxy', 'Remove a reverse proxy rule from a website.', TIER_WRITE,
          schema=obj({'site': SITE_ARG, 'name': string('The proxy rule name.')},
                     required=['site', 'name']), domain=DOMAIN, title='Remove reverse proxy')
    def site_remove_proxy(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        expect(ctx.panel.call('/site', 'RemoveProxy', sitename=row['name'],
                              proxyname=args['name']), 'Removing the reverse proxy')
        return ok('Proxy %s removed from %s.' % (args['name'], row['name']))

    @tool('site_set_expiry', 'Set or clear the expiry date of a website.', TIER_WRITE,
          schema=obj({
              'site': SITE_ARG,
              'date': string('Expiry date as YYYY-MM-DD, or "0000-00-00" for never.',
                             pattern=r'^\d{4}-\d{2}-\d{2}$'),
          }, required=['site', 'date']), domain=DOMAIN, title='Set expiry date')
    def site_set_expiry(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        expect(ctx.panel.call('/site', 'SetEdate', id=row['id'], edate=args['date']),
               'Setting the expiry date')
        return ok('%s expires on %s.' % (row['name'], args['date']))

    # ------------------------------------------------------------- destructive

    @tool('site_delete', 'Delete a website. Optionally also deletes its files, database '
                         'and FTP account. This cannot be undone.',
          TIER_DESTRUCTIVE, schema=obj({
              'site': SITE_ARG,
              'delete_files': boolean('Also delete the document root.', default=False),
              'delete_database': boolean('Also delete the attached database.', default=False),
              'delete_ftp': boolean('Also delete the attached FTP account.', default=False),
          }, required=['site']), domain=DOMAIN, title='Delete website')
    def site_delete(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        params = {'id': row['id'], 'webname': row['name']}
        if args['delete_files']:
            params['path'] = '1'
        if args['delete_database']:
            params['database'] = '1'
        if args['delete_ftp']:
            params['ftp'] = '1'
        expect(ctx.panel.call('/site', 'DeleteSite', **params), 'Deleting the website')
        return ok('Website %s deleted.' % row['name'], deleted=params)

    @tool('site_remove_domain', 'Unbind a domain from a website.', TIER_DESTRUCTIVE,
          schema=obj({
              'site': SITE_ARG,
              'domain': string('The domain to unbind.'),
              'port': integer('Port the domain is bound on.', default=80),
          }, required=['site', 'domain']), domain=DOMAIN, title='Remove domain')
    def site_remove_domain(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        expect(ctx.panel.call('/site', 'DelDomain', id=row['id'], webname=row['name'],
                              domain=args['domain'], port=args['port']), 'Removing the domain')
        return ok('%s no longer serves %s.' % (row['name'], args['domain']))


# ----------------------------------------------------------------- internals

def _domains(panel, site_id):
    result = panel.call('/site', 'GetSiteDomains', id=site_id)
    if isinstance(result, dict):
        return {'domains': result.get('domains', []), 'bindings': result.get('binding', [])}
    return {'domains': rows_of(result), 'bindings': []}


def _rewrite_path(panel, site_name):
    from ..config import panel_home
    return '%s/vhost/rewrite/%s.conf' % (panel_home(), site_name)


def _quiet(panel, route, action, **params):
    """Best-effort call: a missing sub-feature should not fail the whole overview."""
    try:
        return panel.call(route, action, **params)
    except Exception as exc:
        return {'unavailable': str(exc)}


def _tail(payload, lines):
    if isinstance(payload, dict):
        payload = payload.get('msg') or payload.get('data') or ''
    if not isinstance(payload, str):
        return payload
    return '\n'.join(payload.splitlines()[-lines:])


def _password(length=18):
    import secrets
    import string as _string
    alphabet = _string.ascii_letters + _string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))
