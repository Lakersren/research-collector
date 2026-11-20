#!/usr/bin/env bash
set -euo pipefail
bash "$(dirname "$0")/../deploy/setup_service.sh" data "$@"
