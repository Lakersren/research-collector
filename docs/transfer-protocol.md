# A → B 同步协议：按日文章与任务结果

A 每天北京时间 01:00 采集昨天发布的文章，B 每天北京时间 06:00 主动读取已结束的任务结果文件，再拉取其引用的文章/图片批次。B 不通过“目录里有文件”推断任务成功。A 不连接内部数据库或对象存储。

所有 `/transfer/v1/*` 使用独立 Bearer 同步令牌，生产对外使用 HTTPS。接口不接受数据库地址、SQL、对象存储密钥或任意下载 URL。

| 请求 | 结果 |
|---|---|
| GET /transfer/v1/runs?after=&limit=100 | 已结束任务的结果文件 ID，按 ID 升序分页 |
| GET /transfer/v1/runs/{id} | 一轮任务完整 JSON 结果，包括成功、失败或中断 |
| GET /transfer/v1/batches?after=&limit=100 | 已发布批次清单，供运维检查；B 不再从这里决定入库范围 |
| GET /transfer/v1/batches/{id} | v1 manifest，含来源、采集时间/结果、文件路径、大小、SHA-256 |
| GET /transfer/v1/batches/{id}/files/{path} | 下载该 manifest 中列出的固定文件 |
| POST /transfer/v1/batches/{id}/ack | B 提交内部数据库后发送的幂等确认 |

任务 ID 和批次 ID 都是 20 位 UTC 年月日时分秒微秒 + 短横线 + 32 位 UUID hex。两者不是同一个业务编号。任务结果见 `contracts/run-report-v1.schema.json`，批次见 `contracts/batch-v1.schema.json`，文章见 `contracts/article-v1.schema.json`。

## 任务结果文件

A 保存在 `data/runs/<run_id>.json`。执行期间只更新隐藏的 `.pending-<job_id>.json`，不通过接口发布；结束后原子写入最终文件。服务记录运行中的任务意外重启后，会补写中断报告；超时、来源失败也有报告。进程故障期间报告尚未恢复时，B 不会看到半成品。

报告字段：version、id、job_id、execution_date（执行日期）、target_date（文章发布日期）、timezone（业务日期为 Asia/Shanghai）、started_at、finished_at、status、sources。开始/结束时间戳带时区，当前以 UTC 保存。

每个 sources 项保存 source_id、source_name、batch_id，以及 collection：开始/结束时间、发现条数、抓取成功条数、失败条数、status、error。发现条数是来源清单扫描量，抓取条数才是本轮实际下载成功量。没有昨天文章是成功且抓取 0 篇，仍生成可导入的空文章批次。无法确定发布日期的候选文章不会保存，并会在能识别该问题的采集器中计入失败。

报告 status 有 success、failed、interrupted；collection.status 为 success 或 failed。来源因异常或中断没有生成批次时，batch_id 为 null，collection 标记失败。批次部分完成并不等于整轮任务已完成。

## 文章范围和文件布局

文章发布日期统一按北京时间判断；包含昨天 00:00，排除今天 00:00。不回灌更早历史，不自动补抓前天失败文章，不以修改日期代替发布日期。网站列表没有日期时可能需要请求文章页才能判定；无法确认日期则不保存。网站日期、延迟上架和 RSS 滚动窗口会限制可发现范围。

```text
data/
  daily/<run_id>/raw/       本轮独立的原始文章、正文和图片缓存
  daily/<run_id>/state/     本轮来源状态，不沿用旧历史水位线
  runs/<run_id>.json        整轮结束后的结果文件
  batches/<batch_id>/
    manifest.json
    articles.jsonl
    images/<内容SHA256>.<扩展名>
```

每个批次仅包括本轮抓到的昨天文章及其图片，不再包括之前的全历史快照。每次手动重跑也只针对昨天，并使用新工作目录，不夹带其他日期缓存。export、ingest 旧命令在每日模式下兼容为重新采集昨天。

保留文章原有 ID、source_id、正文三态、作者数组和 `[图N]`。图片项保存 position、status、width、height 和包内 path；仅 ok 有 path。A 不提供内部 file_key，B 上传后生成。

## B 的处理与恢复

1. 按任务结果 ID 顺序读取结果文件，校验版本、来源与目标日期。
2. 对有批次的来源，核对报告与 manifest 的来源、采集结果完全一致；校验每个文件大小、SHA-256、每行文章日期和图片引用，然后上传图片并写 SQL。
3. 对没有批次的失败来源，使用报告中的结果写一条 collection_run，不插入文章。通过稳定的本地状态批次编号去重。
4. 零文章成功来源也写 collection_run，避免“没有更新”和“没有执行”混淆。
5. 每个来源沿用一个 MySQL 事务，保存 source、doc、doc_image、collection_run；成功提交后再确认远程批次。
6. 整轮所有来源处理后，将结果文件保存在 B 的 `data/incoming/runs/`，在本地 SQLite 标记任务已同步。A 采集失败仍可以完成 B 的结果同步；数据库中保留采集失败状态。

仍使用四张现有生产表，不新增生产表、不自动执行 DDL。整轮报告和 target_date 在本地 JSON 中留档，各来源执行结果进入 collection_run。B 本地 SQLite 增加 report 检查点表，它不属于生产业务表。

B 校验限制：单文件 256 MiB、批次 4 GiB、manifest 或报告 8 MiB、文章单行 16 MiB。拒绝路径穿越、未知来源、错误日期或报告与批次不一致的数据。

B 在 MySQL 提交前持久化预留 collection_run.id，覆盖“MySQL 已提交，SQLite 尚未完成”的崩溃窗口。重试复用已提交结果或预留 ID，不重复增加采集记录。确认失败只重试确认；未完成整轮不会越过失败任务导入后续任务。已有同 ID 文章 upsert，保留首次 ingested_at；图片使用稳定对象路径复用。

## 本地清理与首次切换

A 每次开始执行采集时，在独占 pipeline.lock 内清理七天前的工作目录、批次、结果文件和相应确认记录。按北京时间执行日期保留今天及前六天，共七个执行日期；即使批次未被 B 确认也按期清理。B 超过保留期未同步的数据无法再从 A 补回。

首次执行每日模式任务，会删除 A 原来的 `data/raw/` 历史缓存及旧 `data/batches/` 全量批次，并写 `state/daily-only-v1.json` 标记；后续不会重复执行全清。配置、令牌、代码、日志、必要状态、B 已入库文章和内部对象存储均不删除。

A、B 必须一起更新。B 更新前停同步 timer，等待当前同步结束；A 切换完成后再启用新 B 同步，避免旧 B 在整轮报告完成前导入新批次。保留 B 的 state/sync.sqlite，不能为升级清空检查点。
