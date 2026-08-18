# coding: utf-8
"""Docker tools: the panel's Docker manager at /v2/btdocker/<module>/<method>."""

import os

from ..config import panel_home
from ..registry import TIER_DESTRUCTIVE, TIER_RAW, TIER_READ, TIER_WRITE
from .common import NO_ARGS, expect, obj, ok, rows_of, string

DOMAIN = 'docker'

MODULES = ('container', 'image', 'compose', 'network', 'volume', 'project', 'registry',
           'status', 'app', 'backup', 'dkgroup', 'proxy', 'setup', 'site')


def _docker_available(panel):
    return os.path.exists(os.path.join(panel_home(), 'class_v2', 'btdockerModelV2'))


def _call(panel, module, method, **params):
    return panel.request('/btdocker/%s/%s' % (module, method), params)


def register(registry):
    tool = registry.tool
    available = _docker_available

    @tool('docker_containers', 'List Docker containers with their state, image and ports.',
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='List containers', available=available)
    def docker_containers(ctx, args):
        result = _call(ctx.panel, 'container', 'get_list', p=1, limit=200)
        return {'containers': rows_of(result) or result}

    @tool('docker_container_info', 'Full inspection of one container.', TIER_READ,
          schema=obj({'container': string('Container id or name.')}, required=['container']),
          domain=DOMAIN, title='Container details', available=available)
    def docker_container_info(ctx, args):
        return _call(ctx.panel, 'container', 'get_container_info', id=args['container'])

    @tool('docker_container_logs', 'Recent log output from a container.', TIER_READ,
          schema=obj({
              'container': string('Container id or name.'),
              'lines': string('How many lines to return.', default='200'),
          }, required=['container']), domain=DOMAIN, title='Container logs', available=available)
    def docker_container_logs(ctx, args):
        return _call(ctx.panel, 'container', 'get_logs', id=args['container'],
                     line=args['lines'])

    @tool('docker_images', 'List Docker images on this host.', TIER_READ, schema=NO_ARGS,
          domain=DOMAIN, title='List images', available=available)
    def docker_images(ctx, args):
        result = _call(ctx.panel, 'image', 'image_list', p=1, limit=200)
        return {'images': rows_of(result) or result}

    @tool('docker_networks', 'List Docker networks.', TIER_READ, schema=NO_ARGS,
          domain=DOMAIN, title='List networks', available=available)
    def docker_networks(ctx, args):
        result = _call(ctx.panel, 'network', 'get_host_network')
        return {'networks': rows_of(result) or result}

    @tool('docker_volumes', 'List Docker volumes.', TIER_READ, schema=NO_ARGS,
          domain=DOMAIN, title='List volumes', available=available)
    def docker_volumes(ctx, args):
        result = _call(ctx.panel, 'volume', 'get_volume_list', p=1, limit=200)
        return {'volumes': rows_of(result) or result}

    @tool('docker_system_info', 'Docker daemon version, storage driver and resource usage.',
          TIER_READ, schema=NO_ARGS, domain=DOMAIN, title='Docker system info',
          available=available)
    def docker_system_info(ctx, args):
        return _call(ctx.panel, 'status', 'get_docker_system_info')

    @tool('docker_container_control', 'Start, stop, restart, pause or unpause a container.',
          TIER_WRITE, schema=obj({
              'container': string('Container id or name.'),
              'action': string('What to do.',
                               enum=['start', 'stop', 'restart', 'pause', 'unpause', 'reload']),
          }, required=['container', 'action']), domain=DOMAIN, title='Control container',
          available=available)
    def docker_container_control(ctx, args):
        result = expect(_call(ctx.panel, 'container', args['action'], id=args['container']),
                        '%sing the container' % args['action'].title())
        return ok('Container %s: %s.' % (args['container'], args['action']), panel_result=result)

    @tool('docker_image_pull', 'Pull an image from a registry.', TIER_WRITE,
          schema=obj({
              'image': string('Image name with tag, e.g. nginx:latest.'),
          }, required=['image']), domain=DOMAIN, title='Pull image', available=available)
    def docker_image_pull(ctx, args):
        result = expect(_call(ctx.panel, 'image', 'pull', name=args['image']),
                        'Pulling the image')
        return ok('Pull of %s started.' % args['image'], panel_result=result)

    @tool('docker_container_delete', 'Remove a container. Anything not on a volume is lost.',
          TIER_DESTRUCTIVE, schema=obj({
              'container': string('Container id or name.'),
          }, required=['container']), domain=DOMAIN, title='Delete container',
          available=available)
    def docker_container_delete(ctx, args):
        result = expect(_call(ctx.panel, 'container', 'del_container', id=args['container']),
                        'Deleting the container')
        return ok('Container %s removed.' % args['container'], panel_result=result)

    @tool('docker_image_delete', 'Remove an image from this host.', TIER_DESTRUCTIVE,
          schema=obj({'image': string('Image id or name.')}, required=['image']),
          domain=DOMAIN, title='Delete image', available=available)
    def docker_image_delete(ctx, args):
        result = expect(_call(ctx.panel, 'image', 'remove', id=args['image']),
                        'Removing the image')
        return ok('Image %s removed.' % args['image'], panel_result=result)

    @tool('docker_call',
          'Call any method of the panel Docker manager, for anything the tools above do not '
          'cover: compose projects, registries, backups. The module is one of %s.'
          % ', '.join(MODULES),
          TIER_RAW, schema=obj({
              'module': string('Docker manager module.', enum=list(MODULES)),
              'method': string('Method name, e.g. get_list, create_async, prune.'),
              'params': {'type': 'object', 'description': 'Parameters for the method.',
                         'default': {}},
          }, required=['module', 'method']), domain=DOMAIN, title='Raw Docker call',
          available=available)
    def docker_call(ctx, args):
        params = args.get('params') or {}
        return {'module': args['module'], 'method': args['method'],
                'result': _call(ctx.panel, args['module'], args['method'], **params)}
