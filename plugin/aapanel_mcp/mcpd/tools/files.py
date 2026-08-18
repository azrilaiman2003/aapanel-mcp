# coding: utf-8
"""File manager tools, and the shell escape hatch: the /files route."""

import threading
import time

from ..panel_client import result_message
from ..registry import TIER_DESTRUCTIVE, TIER_READ, TIER_SHELL, TIER_WRITE, ToolError
from .common import boolean, expect, integer, obj, ok, string

DOMAIN = 'files'

# The panel runs shell commands through a single fixed pair of files in /tmp, so two
# concurrent calls would read each other's output. Serialise them.
_SHELL_LOCK = threading.Lock()


def register(registry):
    tool = registry.tool

    # ------------------------------------------------------------------- read

    @tool('file_list', 'List a directory: names, sizes, permissions, owners, modified times.',
          TIER_READ, schema=obj({
              'path': string('Absolute directory path, e.g. /www/wwwroot.'),
              'search': string('Only show entries containing this text.', default=''),
          }, required=['path']), domain=DOMAIN, title='List directory')
    def file_list(ctx, args):
        params = {'path': args['path'], 'p': 1, 'showRow': 500}
        if args['search']:
            params['search'] = args['search']
        result = ctx.panel.call('/files', 'GetDir', **params)
        if not isinstance(result, dict):
            return {'result': result}
        return {
            'path': result.get('PATH', args['path']),
            'directories': result.get('DIR', []),
            'files': result.get('FILES', []),
            'count': result.get('count'),
        }

    @tool('file_read', 'Read a text file from the server.', TIER_READ,
          schema=obj({'path': string('Absolute file path.')}, required=['path']),
          domain=DOMAIN, title='Read file')
    def file_read(ctx, args):
        result = expect(ctx.panel.call('/files', 'GetFileBody', path=args['path']),
                        'Reading the file')
        if isinstance(result, dict):
            return {'path': args['path'], 'encoding': result.get('encoding'),
                    'content': result.get('data', '')}
        return {'path': args['path'], 'content': result}

    @tool('file_search', 'Find files under a directory whose name contains some text.',
          TIER_READ, schema=obj({
              'path': string('Directory to search under.'),
              'keyword': string('Text the file name must contain.'),
          }, required=['path', 'keyword']), domain=DOMAIN, title='Search files')
    def file_search(ctx, args):
        result = ctx.panel.call('/files', 'GetSearch', path=args['path'], search=args['keyword'])
        if isinstance(result, list):
            text = result[0] if result else ''
        else:
            text = result if isinstance(result, str) else result_message(result, '')
        matches = [line for line in str(text).splitlines() if line.strip()]
        return {'count': len(matches), 'matches': matches[:500]}

    @tool('file_size', 'Total size of a directory or file on disk.', TIER_READ,
          schema=obj({'path': string('Absolute path.')}, required=['path']),
          domain=DOMAIN, title='Path size')
    def file_size(ctx, args):
        return {'path': args['path'],
                'size': ctx.panel.call('/files', 'GetDirSize', path=args['path'])}

    # ------------------------------------------------------------------ write

    @tool('file_write', 'Write a text file, creating or replacing it. The whole content is '
                        'replaced, so read the file first if you mean to edit it.',
          TIER_WRITE, schema=obj({
              'path': string('Absolute file path.'),
              'content': string('The complete new content.'),
              'encoding': string('File encoding.', default='utf-8'),
          }, required=['path', 'content']), domain=DOMAIN, title='Write file')
    def file_write(ctx, args):
        expect(ctx.panel.call('/files', 'SaveFileBody', path=args['path'],
                              data=args['content'], encoding=args['encoding']),
               'Writing the file')
        return ok('Wrote %d bytes to %s.' % (len(args['content'].encode('utf-8')), args['path']))

    @tool('file_create_directory', 'Create a directory, including parents.', TIER_WRITE,
          schema=obj({'path': string('Absolute directory path.')}, required=['path']),
          domain=DOMAIN, title='Create directory')
    def file_create_directory(ctx, args):
        expect(ctx.panel.call('/files', 'CreateDir', path=args['path']),
               'Creating the directory')
        return ok('Directory %s created.' % args['path'])

    @tool('file_copy', 'Copy a file or directory.', TIER_WRITE,
          schema=obj({
              'source': string('Absolute path to copy from.'),
              'target': string('Absolute path to copy to.'),
              'is_directory': boolean('True when the source is a directory.', default=False),
          }, required=['source', 'target']), domain=DOMAIN, title='Copy file')
    def file_copy(ctx, args):
        action = 'CopyDir' if args['is_directory'] else 'CopyFile'
        expect(ctx.panel.call('/files', action, sfile=args['source'], dfile=args['target']),
               'Copying')
        return ok('Copied %s to %s.' % (args['source'], args['target']))

    @tool('file_move', 'Move or rename a file or directory.', TIER_WRITE,
          schema=obj({
              'source': string('Absolute path to move from.'),
              'target': string('Absolute path to move to.'),
          }, required=['source', 'target']), domain=DOMAIN, title='Move file')
    def file_move(ctx, args):
        expect(ctx.panel.call('/files', 'MvFile', sfile=args['source'], dfile=args['target']),
               'Moving')
        return ok('Moved %s to %s.' % (args['source'], args['target']))

    @tool('file_set_permissions', 'Change the mode and owner of a path.', TIER_WRITE,
          schema=obj({
              'path': string('Absolute path.'),
              'mode': string('Octal permission bits, e.g. 644 or 755.',
                             pattern=r'^[0-7]{3,4}$'),
              'owner': string('User and group to own it. The panel sets both to this name.',
                              default='www'),
              'recursive': boolean('Apply to everything underneath as well.', default=False),
          }, required=['path', 'mode']), domain=DOMAIN, title='Set permissions')
    def file_set_permissions(ctx, args):
        expect(ctx.panel.call('/files', 'SetFileAccess', filename=args['path'],
                              access=args['mode'], user=args['owner'],
                              all='True' if args['recursive'] else 'False'),
               'Setting permissions')
        return ok('%s is now %s owned by %s%s.'
                  % (args['path'], args['mode'], args['owner'],
                     ' (recursively)' if args['recursive'] else ''))

    @tool('file_compress', 'Compress files or directories into an archive.', TIER_WRITE,
          schema=obj({
              'path': string('Directory the entries below are relative to.'),
              'entries': string('Comma-separated names inside that directory to include.'),
              'target': string('Absolute path of the archive to create, e.g. /tmp/backup.zip.'),
              'format': string('Archive format.', enum=['zip', 'tar.gz'], default='zip'),
          }, required=['path', 'entries', 'target']), domain=DOMAIN, title='Create archive')
    def file_compress(ctx, args):
        result = expect(ctx.panel.call('/files', 'Zip', path=args['path'], sfile=args['entries'],
                                       dfile=args['target'], z_type=args['format']),
                        'Creating the archive')
        return ok('Archive %s created.' % args['target'], panel_result=result)

    @tool('file_extract', 'Extract an archive into a directory.', TIER_WRITE,
          schema=obj({
              'source': string('Absolute path of the archive.'),
              'target': string('Directory to extract into.'),
              'password': string('Archive password, if it has one.', default=''),
          }, required=['source', 'target']), domain=DOMAIN, title='Extract archive')
    def file_extract(ctx, args):
        result = expect(ctx.panel.call('/files', 'UnZip', sfile=args['source'],
                                       dfile=args['target'], password=args['password'],
                                       z_type='zip'), 'Extracting the archive')
        return ok('Extracted %s into %s.' % (args['source'], args['target']),
                  panel_result=result)

    # ------------------------------------------------------------- destructive

    @tool('file_delete', 'Delete a file or directory. Directories are removed with '
                         'everything inside them.',
          TIER_DESTRUCTIVE, schema=obj({
              'path': string('Absolute path to delete.'),
              'is_directory': boolean('True when the path is a directory.', default=False),
          }, required=['path']), domain=DOMAIN, title='Delete file')
    def file_delete(ctx, args):
        action = 'DeleteDir' if args['is_directory'] else 'DeleteFile'
        expect(ctx.panel.call('/files', action, path=args['path']), 'Deleting')
        return ok('Deleted %s.' % args['path'])

    # -------------------------------------------------------------------- shell

    @tool('run_shell',
          'Run a shell command on the server as root and return its output. The panel '
          'refuses interactive commands (vi, vim, top, passwd, su). Only one shell command '
          'runs at a time; a command still running when the timeout expires keeps running '
          'in the background.',
          TIER_SHELL, schema=obj({
              'command': string('The command line to run.'),
              'cwd': string('Working directory to run it in.', default='/root'),
              'timeout': integer('Seconds to wait for the command to finish.',
                                 default=60, minimum=1, maximum=600),
          }, required=['command']), domain='shell', title='Run shell command')
    def run_shell(ctx, args):
        command = args['command'].strip()
        if not command:
            raise ToolError('command is empty.')
        with _SHELL_LOCK:
            expect(ctx.panel.call('/files', 'ExecShell', shell=command, path=args['cwd']),
                   'Starting the command')
            deadline = time.time() + args['timeout']
            output, finished = '', False
            while time.time() < deadline:
                time.sleep(0.7)
                result = ctx.panel.call('/files', 'GetExecShellMsg')
                if isinstance(result, dict):
                    output = result.get('msg', '') or ''
                    finished = bool(result.get('status'))
                else:
                    output = str(result)
                    finished = output != 'FILE_SHELL_EMPTY'
                if finished:
                    break
        return {
            'command': command,
            'cwd': args['cwd'],
            'finished': finished,
            'output': output,
            'note': None if finished else
                    'Still running after %ss. It keeps going in the background — call '
                    'shell_last_output to check on it rather than running it again.'
                    % args['timeout'],
        }

    @tool('shell_last_output',
          'Output of the most recent shell command, and whether it has finished. Use this '
          'to follow a command that outlived its run_shell timeout.',
          TIER_SHELL, schema=obj(), domain='shell', title='Last shell output')
    def shell_last_output(ctx, args):
        result = ctx.panel.call('/files', 'GetExecShellMsg')
        if isinstance(result, dict):
            return {'finished': bool(result.get('status')), 'output': result.get('msg', '')}
        text = str(result)
        return {'finished': text != 'FILE_SHELL_EMPTY', 'output': text}
