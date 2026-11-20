#!/usr/bin/env bash
# Prepare a Linux/systemd host once; do not start the application or touch databases.
set -euo pipefail
role="${1:?请指定 collector 或 data}"
shift
if [[ "$EUID" -ne 0 ]]; then exec sudo bash "$0" "$role" "$@"; fi
run_user="${SUDO_USER:-}"
if [[ "${1:-}" == --user && $# == 2 ]]; then
  run_user="$2"
elif [[ $# != 0 ]]; then
  echo '用法：bash setup.sh [--user 运行账号]' >&2; exit 1
fi
if [[ -z "$run_user" || "$run_user" == root ]]; then
  echo '请使用普通账号执行 bash setup.sh，或指定 --user 普通账号。' >&2; exit 1
fi
id "$run_user" >/dev/null
run_group="$(id -gn "$run_user")"
command -v systemctl >/dev/null || { echo '需要 Linux systemd。' >&2; exit 1; }
platform_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "$role" in
  collector) project=research-collector; env_files=(collector.env); units=(research-collector.service) ;;
  data) project=research-data-service; env_files=(api.env sync.env); units=(research-data-api.service research-data-sync.service research-data-sync.timer) ;;
  *) echo '仅支持 collector 或 data' >&2; exit 1 ;;
esac
project_dir="$platform_dir/$project"
# Choose a separate modern interpreter, never change /usr/bin/python3 or alternatives.
python_bin=''
for candidate in python3.11 python3.12 python3.10 python3; do
  if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    python_bin="$(command -v "$candidate")"; break
  fi
done
if [[ -z "$python_bin" ]]; then
  if [[ -f /etc/os-release ]]; then . /etc/os-release; fi
  if [[ "${ID:-}" != rocky || "${VERSION_ID:-}" != 8.* ]]; then
    echo '自动安装目前支持 Rocky Linux 8；其他系统请先安装 Python 3.10+ 后重试。' >&2; exit 1
  fi
  dnf install -y python3.11 python3.11-pip
  python_bin="$(command -v python3.11)"
fi
"$python_bin" --version
mkdir -p "$project_dir"/{etc,data,state,logs,outputs}
for name in "${env_files[@]}"; do
  if [[ ! -e "$project_dir/etc/$name" ]]; then
    cp "$project_dir/etc/$name.example" "$project_dir/etc/$name"
    # A needs no data credentials: generate distinct tokens on the host once.
    if [[ "$role" == collector ]]; then
      "$python_bin" - "$project_dir/etc/$name" <<'PY'
from pathlib import Path
import secrets,sys
path=Path(sys.argv[1]);text=path.read_text()
for name in ('CORPUS_API_TOKEN','CORPUS_TRANSFER_TOKEN'):
    text=text.replace(name+'=\n',name+'='+secrets.token_urlsafe(36)+'\n')
path.write_text(text)
PY
    fi
  fi
  chmod 600 "$project_dir/etc/$name"
done
if [[ "$role" == collector && ! -f "$project_dir/config/schedule.yaml" ]]; then
  cp "$project_dir/config/schedule.yaml.example" "$project_dir/config/schedule.yaml"
fi
# Fix ownership after sudo tar, without changing unrelated applications.
for item in "$project" shared contracts deploy docs README.md; do
  if [[ -e "$platform_dir/$item" ]]; then
    chown -R "$run_user:$run_group" "$platform_dir/$item"
    chmod -R u+rwX "$platform_dir/$item"
  fi
done
# Render units from the real extraction location and current operator account.
"$python_bin" - "$platform_dir" "$run_user" "$run_group" "${units[@]}" <<'PY'
from pathlib import Path
import sys
root,user,group,*units=sys.argv[1:]
# systemd 239 treats WorkingDirectory and EnvironmentFile as raw paths, not shell words.
def quoted(value):
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
for name in units:
    target=Path('/etc/systemd/system')/name
    if name.endswith('.timer') and target.exists():
        continue  # Preserve an already configured server-local sync time.
    text=(Path(root)/'deploy'/name).read_text()
    lines=[]
    for line in text.splitlines():
        if line.startswith('User='):line='User='+user
        elif line.startswith('Group='):line='Group='+group
        elif line.startswith('WorkingDirectory='):
            value=line.split('=',1)[1].replace('/opt/research-data-platform',root)
            line='WorkingDirectory='+value.replace('%','%%')
        elif line.startswith('EnvironmentFile='):
            value=line.split('=',1)[1].replace('/opt/research-data-platform',root)
            line='EnvironmentFile='+value.replace('%','%%')
        elif line.startswith('ExecStart='):
            executable,args=line[len('ExecStart='):].split(' ',1)
            line='ExecStart='+quoted(executable.replace('/opt/research-data-platform',root))+' '+args
        lines.append(line)
    target.write_text('\n'.join(lines)+'\n')
PY
systemctl daemon-reload
for unit in "${units[@]}"; do
  load_state="$(systemctl show "$unit" --property=LoadState)"
  if [[ "$load_state" != LoadState=loaded ]]; then
    echo "服务配置未加载：${unit}（${load_state}）。请检查以下诊断。" >&2
    systemctl status "$unit" --no-pager --full >&2 || true
    exit 1
  fi
done
if [[ "$role" == collector ]]; then
  systemctl enable research-collector.service
else
  systemctl enable research-data-api.service research-data-sync.timer
fi
echo "初始化完成，运行账号：${run_user}；目录：${project_dir}"
echo '已有配置、数据、日志和采集时间均保留。尚未启动服务。'
echo "请检查 ${project_dir}/etc/ 中的配置，然后执行：bash ${project_dir}/start.sh"
