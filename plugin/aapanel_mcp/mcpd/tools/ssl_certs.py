# coding: utf-8
"""TLS tools: /site SSL actions plus the /ssl and /acme routes."""

import json

from ..registry import TIER_DESTRUCTIVE, TIER_READ, TIER_WRITE
from .common import (NO_ARGS, array, boolean, expect, obj, ok, resolve_site, rows_of, string)

DOMAIN = 'ssl'
SITE_ARG = string('Website: its domain, its panel name, or its numeric id.')


def register(registry):
    tool = registry.tool

    @tool('ssl_list_certificates', 'Certificates stored in the panel, with their expiry dates.',
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='List certificates')
    def ssl_list_certificates(ctx, args):
        return {'certificates': rows_of(ctx.panel.call('/ssl', 'GetCertList'))}

    @tool('ssl_site_status', 'TLS state of one website: issuer, expiry, forced HTTPS.',
          TIER_READ, schema=obj({'site': SITE_ARG}, required=['site']),
          domain=DOMAIN, title='Website TLS status')
    def ssl_site_status(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        return ctx.panel.call('/site', 'GetSSL', siteName=row['name'])

    @tool('ssl_acme_orders', "Let's Encrypt orders the panel is tracking, with their renewal state.",
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='List ACME orders')
    def ssl_acme_orders(ctx, args):
        return {'orders': ctx.panel.call('/acme', 'get_orders')}

    @tool('ssl_issue_letsencrypt',
          "Request a Let's Encrypt certificate for a website and install it. File validation "
          "is used by default, which needs the domains already pointing at this server and no "
          "301 redirect or reverse proxy in the way. Pass dns_api for a wildcard.",
          TIER_WRITE, schema=obj({
              'site': SITE_ARG,
              'domains': array('Domains to include. Defaults to every domain bound to the site.',
                               {'type': 'string'}, default=[]),
              'email': string('Contact address for the ACME account.', default=''),
              'dns_api': string('Name of a DNS API configured in the panel, for DNS-01 '
                                'validation. Required for wildcard certificates.', default=''),
          }, required=['site']), domain=DOMAIN, title="Issue Let's Encrypt certificate")
    def ssl_issue_letsencrypt(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        domains = list(args['domains'])
        if not domains:
            result = ctx.panel.call('/site', 'GetSiteDomains', id=row['id'])
            listed = result.get('domains', []) if isinstance(result, dict) else []
            domains = [d.get('name') for d in listed if d.get('name')]
        if not domains:
            domains = [row['name']]

        params = {'siteName': row['name'], 'domains': json.dumps(domains)}
        if args['email']:
            params['email'] = args['email']
        if args['dns_api']:
            params['dnsapi'] = args['dns_api']
            params['dnssleep'] = 10
        result = expect(ctx.panel.call('/site', 'CreateLet', **params),
                        'Requesting the certificate')
        return ok('Certificate issued for %s.' % ', '.join(domains), domains=domains,
                  panel_result=result)

    @tool('ssl_upload_certificate', 'Install a certificate you already have on a website.',
          TIER_WRITE, schema=obj({
              'site': SITE_ARG,
              'certificate': string('The full certificate chain, PEM encoded.'),
              'private_key': string('The private key, PEM encoded.'),
          }, required=['site', 'certificate', 'private_key']), domain=DOMAIN,
          title='Upload certificate')
    def ssl_upload_certificate(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        expect(ctx.panel.call('/site', 'SetSSL', siteName=row['name'],
                              key=args['private_key'], csr=args['certificate']),
               'Installing the certificate')
        return ok('Certificate installed on %s.' % row['name'])

    @tool('ssl_set_force_https', 'Turn the HTTP to HTTPS redirect on or off for a website.',
          TIER_WRITE, schema=obj({
              'site': SITE_ARG,
              'enabled': boolean('True to force HTTPS, false to allow plain HTTP.'),
          }, required=['site', 'enabled']), domain=DOMAIN, title='Force HTTPS')
    def ssl_set_force_https(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        action = 'HttpToHttps' if args['enabled'] else 'CloseToHttps'
        expect(ctx.panel.call('/site', action, siteName=row['name']),
               'Changing the HTTPS redirect')
        return ok('%s %s force HTTPS.' % (row['name'], 'now forces' if args['enabled'] else 'no longer forces'))

    @tool('ssl_renew', "Renew a Let's Encrypt order early.", TIER_WRITE,
          schema=obj({'order_index': string('The order index from ssl_acme_orders.')},
                     required=['order_index']), domain=DOMAIN, title='Renew certificate')
    def ssl_renew(ctx, args):
        result = expect(ctx.panel.call('/acme', 'renew_cert', index=args['order_index']),
                        'Renewing the certificate')
        return ok('Renewal requested.', panel_result=result)

    @tool('ssl_disable', 'Turn TLS off for a website. Visitors on https:// will stop being '
                         'served until a certificate is installed again.',
          TIER_DESTRUCTIVE, schema=obj({'site': SITE_ARG}, required=['site']),
          domain=DOMAIN, title='Disable TLS')
    def ssl_disable(ctx, args):
        row = resolve_site(ctx.panel, args['site'])
        expect(ctx.panel.call('/site', 'CloseSSLConf', siteName=row['name']), 'Disabling TLS')
        return ok('TLS disabled on %s.' % row['name'])
