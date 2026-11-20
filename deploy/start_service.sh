#!/usr/bin/env bash
# Run after manually uploading/replacing code. systemd is installed once by setup.sh.
set -euo pipefail
role="${1:?请指定 collector 或 data}"
platform_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "$role" in
  collector)
    project=research-collector
    service=research-collector.service
    env_files=(collector.env)
    units=("$service")
    ;;
  data)
    project=research-data-service
    service=research-data-api.service
    env_files=(api.env sync.env)
    units=("$service" research-data-sync.service research-data-sync.timer)
    ;;
  *) echo '仅支持 collector 或 data' >&2; exit 1 ;;
esac
cd "$platform_dir/$project"
for config in "${env_files[@]}"; do
  if [[ ! -f "etc/$config" ]]; then
    echo "缺少配置：${PWD}/etc/${config}。首次请复制对应 .example 并填写，后续无需重复。" >&2
    exit 1
  fi
done
if ! command -v systemctl >/dev/null 2>&1; then
  echo '此脚本用于 Linux systemd，请先执行 bash setup.sh。' >&2
  exit 1
fi
for unit in "${units[@]}"; do
  load_state="$(systemctl show "$unit" --property=LoadState)"
  if [[ "$load_state" != LoadState=loaded ]]; then
    echo "服务无法加载：${unit}（${load_state}）。缺少服务请执行 bash setup.sh；否则检查配置错误。" >&2
    systemctl status "$unit" --no-pager --full >&2 || true
    exit 1
  fi
done
if [[ "$role" == collector ]]; then
  working_dir="$(systemctl show "$service" --property=WorkingDirectory --value)"
  if [[ -z "$working_dir" || ! -d "$working_dir" ]] ||
     [[ "$(cd "$working_dir" && pwd -P)" != "$(pwd -P)" ]]; then
    echo "采集服务的工作目录为 ${working_dir:-未设置}，当前脚本位于 ${PWD}。" >&2
    echo '请在采集服务的部署目录更新代码，再执行 bash start.sh。' >&2
    exit 1
  fi
fi
if [[ "$role" == data ]] && systemctl is-active --quiet research-data-sync.service; then
  echo '同步任务正在运行，请任务完成后再执行。' >&2
  exit 1
fi
if [[ "$role" == collector && ! -f config/schedule.yaml ]]; then
  cp config/schedule.yaml.example config/schedule.yaml
  echo '已创建首次采集时间配置：config/schedule.yaml'
fi
# Reuse the existing virtualenv; pip installs only missing/changed requirements.
bash install.sh
if [[ "$role" == collector ]]; then
  .venv/bin/python - <<'PYTHON'
from pathlib import Path
import yaml
p=Path('config/schedule.yaml')
config=yaml.safe_load(p.read_text())
changed=False
for job in config.get('jobs',[]):
    if job.get('id')=='collect-all' and job.get('cron')=='23 * * * *':
        job['cron']='0 1 * * *'
        changed=True
if changed:
    config['timezone']='Asia/Shanghai'
    temp=p.with_suffix('.tmp')
    temp.write_text(yaml.safe_dump(config,allow_unicode=True,sort_keys=False))
    temp.replace(p)
    print('已将原默认小时调度调整为每天北京时间 01:00。')
PYTHON
fi
control=(systemctl)
if [[ "$EUID" -ne 0 ]]; then control=(sudo systemctl); fi
"${control[@]}" restart "$service"
if [[ "$role" == data ]]; then
  "${control[@]}" start research-data-sync.timer
fi
"${control[@]}" is-active --quiet "$service"
echo "已启动：${service}（配置和数据保持不变）"
echo "查看启动或运行错误：journalctl -u $service -n 100 --no-pager"
