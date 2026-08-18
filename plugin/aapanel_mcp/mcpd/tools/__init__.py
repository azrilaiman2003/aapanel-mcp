# coding: utf-8
"""Tool registration.

`build_registry()` returns a Registry with every tool defined. Which of them a client
actually sees is decided later, by the permission tiers and by each tool's availability
check (Docker tools only exist where the Docker manager is installed, mail tools only
where a mail plugin is).
"""

from ..registry import Registry
from . import (apps, cron, databases, docker, files, firewall, ftp, mail, raw, sites,
               ssl_certs, system)

MODULES = (sites, ssl_certs, databases, mail, ftp, files, cron, firewall, system, docker,
           apps, raw)


def build_registry():
    registry = Registry()
    for module in MODULES:
        module.register(registry)
    return registry
