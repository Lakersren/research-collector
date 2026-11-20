# Research Data Platform · 投研数据平台

一个仓库、两个独立部署项目。外网采集服务器 A 不连接内部数据库或 MinIO；内网服务器 B 每日主动下载数据文件，校验后使用参数化 SQL 写入既有四表，并提供 API / MCP 查询。

```text
外部网站
  ↓
服务器 A · research-collector
  采集 → 日期归档 → HTTPS 下载接口
                                              ↑ B 主动拉取
服务器 B · research-data-service
  每日同步 → 校验 → 图片写 MinIO / XSKY → 文章写 MySQL
                                         ↓
                              读取 API / MCP → 看板 / Agent
```

## 代码目录

```text
research-collector/       A：采集、日期归档、下载接口、采集调度
research-data-service/    B：同步入库、查询 API、MCP
contracts/               两端共享的数据包 v1 定义、OpenAPI 和 MCP 工具定义
shared/                  两端共享的路径、来源配置加载和日志工具
deploy/                  独立发布包构建脚本、systemd 服务和每日 timer
docs/                    部署与协议说明
tests/                   解析、API、MCP、同步与故障恢复测试
```

两端分别拥有 requirements、install.sh、配置模板与启动入口。独立部署包只包含本项目、共享运行库和协议，不包含另一项目的业务代码、真实配置、本地数据或虚拟环境。

## 部署入口

[采集服务 A 部署说明](docs/deployment.md)包含首次安装、更新和试采。

## 手动操作

A 服务启动后，在 `research-collector/` 目录执行：

```bash
bash ./test-source.sh      # 列出来源并选择
bash ./test-source.sh 7    # 采集编号 7
bash ./test-source.sh all  # 采集全部来源
```

每个目标发表日的来源汇总、逐篇结果和正文图片保存在 `data/archive/YYYYMMDD/`。打开 `report.md` 核对。

`collect` 只采集昨天并生成日期报告、正文和图片。每日模式下旧 `export`、`ingest` 命令也按重新采集昨天处理，不再回灌历史，A 不写数据库。默认每天北京时间 01:00 采集 29 个来源中昨天发布的文章；修改 `research-collector/config/schedule.yaml` 后重启 A。采集管理接口 `/jobs`、`/schedules` 使用管理令牌；`/transfer/v1/*` 使用独立同步令牌。

B 目录下加载 `etc/sync.env` 后，可手动运行：

```bash
.venv/bin/python sync.py 20261109  # 按北京时间的目标发表日同步
.venv/bin/python sync.py          # 同步北京时间昨天的结果
```

另一个干净的 shell 加载 `etc/api.env` 后，可启动查询：

```bash
.venv/bin/python api_service.py
```

systemd 会分别加载三个进程各自的配置文件。不要在同一个 shell 混合加载读写和只读配置。

## 查询能力

| API | MCP 工具 |
|---|---|
| GET /v1/sources | list_sources |
| GET /v1/articles | search_articles |
| GET /v1/articles/{id} | get_article |
| GET /v1/articles/{id}/images | get_article_images |

MCP 地址为服务器 B 的 `/mcp`，使用 `CORPUS_READ_API_TOKEN`。查询接口保留独立鉴权、标题筛选、时间区间、签名游标、正文三态和 15 分钟图片临时地址。无需向 Agent 提供同步令牌、写库账号或对象存储密钥。可选的 `mcp_service.py` 支持进一步独立部署 MCP 适配器。

## 同步行为与恢复

- A 仅抓取北京时间昨天发布的文章，按发表日和来源归档，完成后原子发布。B 按日期读取归档中的报告、正文和图片。
- 日期报告包含文章明细、文件大小和 SHA-256。B 不接收或执行 A 传来的 SQL，SQL 模板仅存在 B 的代码中。
- B 使用本地 state/sync.sqlite 保存日期及来源进度和预留的记录 ID。采集失败或零文章也写入 collection_run。传输/入库失败则停止，修复后重跑。
- B 全量校验文件和文章后上传图片，再用一个 MySQL 事务提交整批文章、图片关联和采集记录。失败时回滚 SQL；已上传图片通过稳定对象路径复用。
- 重复同步不会增加同 ID 文章，保留首次 `ingested_at`。成功提交后才向 A 确认；确认失败时只重发确认，提交后检查点失败也可恢复，不重复增加采集记录。
- 生产库保留 `source`、`doc`、`doc_image`、`collection_run` 四张表，不自动执行 DDL。SQLite 是 B 本地同步状态，不是新增生产业务表。
- A 每次采集后清理七天前的日期归档。B 仍需备份 sync.sqlite，不要清空同步进度。

采集完整性与同步状态分开：外网单张图片失败仍可发布失败标记和可用文章，保留 `[图N]` 位置；B 成功接收不把原始 `collection_run.status=failed` 改成成功。同日手动重跑可重试昨天文章的失败图片；下一天不会自动补采更早日期。

日志为脱敏 JSON Lines：A 的 `logs/service.log`、`logs/pipeline.log`；B 的 `logs/sync.log`、`logs/api.log`。可按 batch_id、job_id、request_id 排查；不记录正文、查询关键词、同步认证头或签名图片地址。

## 开发测试

```bash
bash install.sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python tests/web_sources.py --no-live
```

集成测试用真实日期归档、A 的 HTTP 路由和本地 SQL 适配器覆盖跨服务同步及恢复；使用模拟对象存储，不连接真实生产库。真实 Linux 双机、MySQL 与 MinIO 联调在部署环境进行。

协议见 [同步协议](docs/transfer-protocol.md)、[查询契约](docs/api-mcp-contract.md)、[OpenAPI](contracts/openapi-read.yaml)、[MCP 工具](contracts/mcp-tools.json)。

## 每日模式升级

从旧的全量采集模式升级到每日模式时，A、B 都需要更新发布包。A start.sh 会把之前随项目提供的默认 `23 * * * *` 调度迁移为 `0 1 * * *`，保留其他设置；自定义调度请在 config/schedule.yaml 自行调整。A 首次执行每日任务才清理旧历史缓存，不是在解压或启动时删除。

任务结果保存在 A 的 `data/archive/YYYYMMDD/report.json`，B 同步后将结果保存在 `data/incoming/days/YYYYMMDD.json`。详情见 [同步协议](docs/transfer-protocol.md)。
