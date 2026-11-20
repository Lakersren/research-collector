#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Reuse a modern venv, or select a separate modern Python without changing the OS Python.
valid_python() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null
}
python_bin="${PYTHON_BIN:-}"
if [[ -n "$python_bin" ]]; then
  if ! valid_python "$python_bin"; then
    echo "指定的 ${python_bin} 不可用或低于 Python 3.10。" >&2; exit 1
  fi
elif [[ -x .venv/bin/python ]] && valid_python .venv/bin/python; then
  python_bin="$PWD/.venv/bin/python"
else
  for candidate in python3.11 python3.12 python3.10 python3; do
    if command -v "$candidate" >/dev/null && valid_python "$candidate"; then
      python_bin="$(command -v "$candidate")"; break
    fi
  done
fi
if [[ -z "$python_bin" ]]; then
  echo '未找到 Python 3.10+，请先执行 bash setup.sh。' >&2; exit 1
fi
if [[ -e .venv ]]; then
  if [[ ! -x .venv/bin/python ]] || ! valid_python .venv/bin/python; then
    backup=".venv-old-$(date +%Y%m%d%H%M%S)-$$"
    mv .venv "$backup"
    echo "已保留旧虚拟环境：${backup}"
  fi
fi
if [[ ! -d .venv ]]; then "$python_bin" -m venv .venv; fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
