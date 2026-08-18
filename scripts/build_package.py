#!/usr/bin/env python3
# coding: utf-8
"""Build the importable aaPanel plugin archive.

aaPanel's importer (class/panelPlugin.py: update_zip/input_zip) unpacks the archive
into a temp dir and then walks it looking for a directory that holds BOTH info.json
and install.sh, with at least three files in it. That directory is what gets copied
to /www/server/panel/plugin/<name>/. So the archive must contain the plugin folder,
not the plugin's contents at the root.

Usage:
    python3 scripts/build_package.py [--out dist]
"""

import argparse
import json
import os
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_NAME = 'aapanel_mcp'
PLUGIN_SRC = os.path.join(REPO_ROOT, 'plugin', PLUGIN_NAME)

# Never ship these: runtime state, bytecode, editor droppings.
EXCLUDE_DIRS = {'__pycache__', '.git', '.idea', '.vscode'}
EXCLUDE_EXTS = {'.pyc', '.pyo', '.swp'}
EXCLUDE_NAMES = {'.DS_Store'}

# The importer needs these two; the rest are ours and their absence means a broken build.
REQUIRED_FILES = [
    'info.json',
    'install.sh',
    'index.html',
    'aapanel_mcp_main.py',
    'aapanel_mcp_service',
    'bin/aapanel-mcp-stdio',
    'mcpd/__init__.py',
    'mcpd/config.py',
    'mcpd/panel_client.py',
    'mcpd/protocol.py',
    'mcpd/http_transport.py',
    'mcpd/stdio_transport.py',
    'mcpd/registry.py',
    'mcpd/permissions.py',
    'mcpd/audit.py',
]

# Modes are rebuilt on install (aaPanel runs `chmod -R 600` over the whole plugin dir
# right after copying), but shipping sane modes keeps the archive usable by hand too.
EXECUTABLE = {
    'install.sh',
    'upgrade.sh',
    'repair.sh',
    'aapanel_mcp_service',
    'bin/aapanel-mcp-stdio',
}


def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for name in sorted(filenames):
            if name in EXCLUDE_NAMES:
                continue
            if os.path.splitext(name)[1] in EXCLUDE_EXTS:
                continue
            abs_path = os.path.join(dirpath, name)
            rel_path = os.path.relpath(abs_path, root).replace(os.sep, '/')
            # data/ holds runtime state (config, audit log); install.sh recreates it.
            if rel_path.startswith('data/') and rel_path != 'data/.gitkeep':
                continue
            yield abs_path, rel_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', default=os.path.join(REPO_ROOT, 'dist'))
    args = parser.parse_args()

    with open(os.path.join(PLUGIN_SRC, 'info.json'), encoding='utf-8') as fp:
        info = json.load(fp)
    version = info['versions']
    if info['name'] != PLUGIN_NAME:
        sys.exit('info.json name (%s) does not match plugin dir (%s)' % (info['name'], PLUGIN_NAME))

    files = list(iter_files(PLUGIN_SRC))
    present = {rel for _, rel in files}
    missing = [f for f in REQUIRED_FILES if f not in present]
    if missing:
        sys.exit('refusing to build, missing files: %s' % ', '.join(missing))
    empty = [rel for abs_path, rel in files if rel in REQUIRED_FILES and os.path.getsize(abs_path) == 0]
    if empty:
        sys.exit('refusing to build, empty files: %s' % ', '.join(empty))

    os.makedirs(args.out, exist_ok=True)
    target = os.path.join(args.out, '%s-%s.zip' % (PLUGIN_NAME, version))
    if os.path.exists(target):
        os.remove(target)

    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as zf:
        for abs_path, rel_path in files:
            arcname = '%s/%s' % (PLUGIN_NAME, rel_path)
            zinfo = zipfile.ZipInfo.from_file(abs_path, arcname)
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if rel_path in EXECUTABLE else 0o644
            zinfo.external_attr = mode << 16
            with open(abs_path, 'rb') as fp:
                zf.writestr(zinfo, fp.read())

    size_kb = os.path.getsize(target) / 1024.0
    print('built %s (%d files, %.1f KB)' % (target, len(files), size_kb))
    print('import it via aaPanel -> App Store -> Third-party -> Import')


if __name__ == '__main__':
    main()
