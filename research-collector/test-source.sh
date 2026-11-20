#!/usr/bin/env bash
set -euo pipefail
collector_dir="${COLLECTOR_DIR:-/opt/app/research-collector}"
python="$collector_dir/.venv/bin/python"
if [[ ! -x "$python" ]]; then
    printf '找不到采集服务的 Python：%s\n' "$python" >&2
    exit 1
fi

# Keep stdin attached to the terminal so the operator can choose a source.
"$python" /dev/fd/3 "$collector_dir" "$@" 3<<'PYTHON'
import argparse
import http.client
import json
from pathlib import Path
import sys
import time


ROOT = Path(sys.argv[1]).resolve()
POLL_SECONDS = 2
JOB_TIMEOUT_SECONDS = 7200


def settings():
    config = {}
    for line in (ROOT / "etc/collector.env").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in {"CORPUS_API_TOKEN", "CORPUS_PORT"}:
            value = value.strip()
            if len(value) >= 2 and value[0] in "\"'" and value[0] == value[-1]:
                value = value[1:-1]
            config[key.strip()] = value
    token = config.get("CORPUS_API_TOKEN", "")
    if not token:
        raise ValueError("请先在 etc/collector.env 设置 CORPUS_API_TOKEN")
    return int(config.get("CORPUS_PORT", "8000")), token


def request(port, token, path, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {"Authorization": f"Bearer {token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request("GET" if payload is None else "POST", path,
                     body=json.dumps(payload) if payload is not None else None, headers=headers)
        response = conn.getresponse()
        body = response.read()
        if response.status == 401:
            raise RuntimeError("管理令牌校验失败，请核对 etc/collector.env")
        if response.status == 409:
            raise RuntimeError("采集服务正在执行其他任务，请等它结束后再试")
        if response.status not in (200, 202):
            raise RuntimeError(f"采集服务返回 HTTP {response.status}，请查看 {ROOT / 'logs/service.log'}")
        return json.loads(body)
    finally:
        conn.close()


def choose_scope(sources, selection):
    if not sources:
        raise RuntimeError("采集服务没有配置来源")
    interactive = selection is None
    if interactive:
        for number, source in enumerate(sources, 1):
            print(f"{number:2}. {source['name']} [{source['id']}]")
    while True:
        if interactive:
            selection = input(f"输入来源编号（1–{len(sources)}）或 all（全部，回车退出）：")
        choice = selection.strip().lower()
        if not choice and interactive:
            return None
        if choice == "all":
            return {"scope": "all"}
        if choice.isdecimal() and len(choice) <= len(str(len(sources))):
            number = int(choice)
            if 1 <= number <= len(sources):
                return {"scope": "source", "source_id": sources[number - 1]["id"]}
        if not interactive:
            raise ValueError(f"请输入 1–{len(sources)} 的编号或 all")
        print("编号无效，请重新输入。")


def show_source(source, target):
    result = source["collection"]
    print(f"\n{source['source_name']} [{source['source_id']}] · {result['status']}")
    if result.get("error"):
        print(result["error"])
    audit = source.get("articles")
    dates_ok = True
    if audit is not None:
        print(f"已保存 {len(audit['success'])} 篇，失败 {len(audit['failed'])} 篇，跳过 {len(audit['skipped'])} 篇")
        dates_ok = all(article["publication_date_beijing"] == target for article in audit["success"])
        if audit["success"]:
            print("已保存文章的发布日期核对通过" if dates_ok else "发现发布日期与目标日期不符的文章")
        for article in audit["success"]:
            print(f"  {article['publication_date_beijing']}  {article['title']}")
    if not source.get("batch_id"):
        print("本轮未生成文章批次。")
        return False

    batch_dir = ROOT / "data/batches" / source["batch_id"]
    article_file = batch_dir / "articles.jsonl"
    if not article_file.is_file():
        print(f"找不到批次正文：{article_file}")
        return False
    print(f"正文：{article_file}")
    if (batch_dir / "images").is_dir():
        print(f"图片目录：{batch_dir / 'images'}")
    return result["status"] == "success" and dates_ok


def show_result(job, run_id):
    report_file = ROOT / "data/runs" / f"{run_id}.json"
    print(f"任务状态：{job['status']}")
    if job.get("error"):
        print(job["error"])
    if not report_file.is_file():
        print(f"没有生成执行报告。请查看：{ROOT / 'logs/pipeline.log'}")
        return 1

    report = json.loads(report_file.read_text(encoding="utf-8"))
    print(f"目标发布日期（北京时间）：{report['target_date']}")
    completion_file = report_file.with_suffix(".md")
    if completion_file.is_file():
        print(f"采集任务完成表：{completion_file}")
    print(f"逐篇 JSON：{report_file}")
    raw_dir = ROOT / "data/daily" / run_id / "raw"
    if raw_dir.is_dir():
        print(f"本轮原始文件目录：{raw_dir}")
    if any("articles" not in source for source in report["sources"]):
        print("本轮报告没有逐篇明细。请升级并重启 A 的采集服务。")
        return 1
    results = [show_source(source, report["target_date"]) for source in report["sources"]]
    if job["status"] != "success" or not all(results):
        print(f"采集日志：{ROOT / 'logs/pipeline.log'}")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="采集指定编号的来源或全部来源")
    parser.add_argument("source", nargs="?", help="来源编号或 all，省略时交互选择")
    args = parser.parse_args(sys.argv[2:])
    port, token = settings()
    sources = request(port, token, "/sources")
    payload = choose_scope(sources, args.source)
    if payload is None:
        return 0
    job = request(port, token, "/jobs/today", payload)
    job_id, run_id = job["id"], job["run_id"]
    count = len(sources) if payload["scope"] == "all" else 1
    print(f"正在采集 {count} 个来源，任务 ID {job_id}", flush=True)
    started = time.monotonic()
    last_update = started
    while job["status"] in {"queued", "running"}:
        now = time.monotonic()
        if now - started > JOB_TIMEOUT_SECONDS + 60:
            raise RuntimeError(f"等待任务结果超时，任务 ID {job_id}。请查看 {ROOT / 'logs/pipeline.log'}")
        if now - last_update >= 15:
            print(f"已等待 {int(now - started)} 秒，当前状态 {job['status']}", flush=True)
            last_update = now
        time.sleep(POLL_SECONDS)
        job = request(port, token, f"/jobs/{job_id}")
    return show_result(job, run_id)


try:
    raise SystemExit(main())
except (KeyboardInterrupt, EOFError):
    print("\n已退出试采脚本。已提交的任务仍由采集服务执行。", file=sys.stderr)
    raise SystemExit(130) from None
except (OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
    print(f"试采未完成：{error}", file=sys.stderr)
    raise SystemExit(1) from None
PYTHON
