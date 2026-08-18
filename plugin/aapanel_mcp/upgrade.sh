#!/bin/bash
# Re-run after importing a newer build: restores modes, refreshes the service
# definition and restarts. data/ (config, audit log) is left untouched.
cd "$(dirname "$0")" || exit 1
bash install.sh install
