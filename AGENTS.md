# 投研采集项目

- `research-collector/` 是外网服务器 A 的采集服务，systemd 单元为 `research-collector.service`。来源在 `research-collector/config/web_sources.yaml`，SemiAnalysis 由来源加载器额外注册。采集器按 Substack、RSS / Atom、HTML 列表和专用入口处理，所有来源共用每日日期校验、批次发布和任务报告。
- `research-data-service/` 是服务器 B 的同步和查询服务。B 主动拉取 A 的已结束任务和对应批次，再写 MySQL 和对象存储，对外提供 API / MCP。
- A 默认每天北京时间 01:00 运行，只保留北京时间昨天发布的文章。手动触发走同一条流程。执行日与目标发表日分别以报告中的 `execution_date` 和 `target_date` 为准。

## 在 A 上查询和触发

从 Git 仓库根目录进入 `research-collector/`，也就是含 `etc/collector.env` 的项目目录。以下函数在同一个 shell 中使用。管理令牌只从本机配置加载，用标准输入交给 curl，避免出现在 curl 参数、聊天内容和日志里。

```bash
cd "$(git rev-parse --show-toplevel)/research-collector" || exit 1
collector_api() (
    set +x
    . ./etc/collector.env || exit 1
    : "${CORPUS_API_TOKEN:?缺少管理令牌}"
    printf 'header = "Authorization: Bearer %s"\n' "$CORPUS_API_TOKEN" |
        curl --config - -fsS --max-time 15 "$@" \
            "http://127.0.0.1:${CORPUS_PORT:-8000}$collector_path"
)

collector_path=/health collector_api
collector_path=/schedules collector_api
collector_path=/sources collector_api
collector_path=/jobs collector_api
```

用户只要求评估时，从最近的任务和报告开始检查。用户要求手动触发时，按指定范围调用下列命令之一。单来源的 `source_id` 从 `GET /sources` 获取。

```bash
collector_path=/jobs/today collector_api -X POST \
    -H 'Content-Type: application/json' -d '{"scope":"all"}'

collector_path=/jobs/today collector_api -X POST \
    -H 'Content-Type: application/json' -d '{"scope":"source","source_id":"web:servethehome"}'
```

HTTP 202 表示任务已提交，响应含 `id`、`run_id` 与 `target_date`。HTTP 409 表示已有任务在跑，检查该任务进度。用 `collector_path="/jobs/实际任务ID" collector_api` 查询，直到 `success`、`failed` 或 `interrupted`。用返回的 `run_id` 查找本轮完成表；`queued` 和 `running` 都表示尚未结束。

## 评估采集结果

1. 读取 `data/runs/<run_id>.md` 和同名 JSON。核对执行时间、目标日期、来源范围。全量任务应覆盖服务当前 `GET /sources` 返回的清单，单来源任务只评价该来源。
2. 用 JSON 的 `sources[].articles.success`、`failed`、`skipped` 汇总保存、失败与跳过篇数。另列失败或未完成的来源，即使没有逐篇失败记录。成功且保存 0 篇表示本轮没有落盘文章，结合跳过原因判断。
3. 核对每篇成功文章的 `publication_date_beijing` 等于 `target_date`。有 `batch_id` 时，再复核 `data/batches/<batch_id>/articles.jsonl` 的每篇 `published_at` 转成北京时间后都属于目标日，且正文条数与成功列表一致。用 `target_date` 复核已有报告，不能用检查当天重新算昨天。
4. 失败时按任务 ID 查看 `logs/pipeline.log` 和 `logs/service.log`，区分整站清单失败、单篇抓取失败、日期缺失、图片失败和任务中断。接口不可用时查 `systemctl status research-collector.service` 和 `journalctl -u research-collector.service -n 50 --no-pager`。
5. 向用户给出结论、完整报告路径、失败来源和原因，以及日期核对结果。明确区分“已保存文章日期都符合”和“所有来源都成功”。A 的报告说明采集状态，B 是否已入库要查 B 的同步记录。
