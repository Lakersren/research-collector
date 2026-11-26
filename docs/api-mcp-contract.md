# 语料读取 API 与 MCP 接口契约 v1

状态：读取 API / MCP 位于 research-data-service 项目，由服务器 B 部署。A 采集并发布日期归档，B 的独立 sync.py 每日主动拉取并写入既有四表，api_service.py 负责查询。详见 deployment.md 和 transfer-protocol.md。

## 1. 服务关系与范围

看板通过 API 读取数据；开发 Agent、云端分析 Agent 通过 MCP 调用同一套 API。MCP 是工具适配层，不直接连接 MySQL 或对象存储，也不复制查询逻辑。

```text
看板 ───────────────────────→ /v1/* 读取 API → 文章查询与图片签名服务 → MySQL / S3
开发 Agent、云端 Agent → MCP ──┘
```

| 能力 | HTTP API | MCP 工具 |
|---|---|---|
| 来源清单 | GET /v1/sources | list_sources |
| 文章筛选 | GET /v1/articles | search_articles |
| 文章详情 | GET /v1/articles/{id} | get_article |
| 配图访问及刷新 | GET /v1/articles/{id}/images | get_article_images |

API 机器定义见 [openapi-read.yaml](../contracts/openapi-read.yaml)；MCP 的 `tools/list` 定义见 [mcp-tools.json](../contracts/mcp-tools.json)。行为约定以本文配合上述参数、类型定义为准。

第一版复用 `source`、`doc`、`doc_image`；不需要新表。`collection_run` 继续用于采集管理，不通过这四个读取接口暴露。文章搜索只提供标题字面量包含匹配，不承诺全文检索、相关性排序或语义搜索。公司、证券代码、行业等字段当前未保存，不提供相应筛选。

## 2. 公共约定

- 请求和响应使用 UTF-8 JSON；REST 查询参数按 URL 标准编码。
- ID 大小写敏感，必须原样传递。路径 ID 进行 URL 编码，不让调用方拼接 SQL 或对象路径。
- 时间参数为带时区的 RFC 3339 时间，接受 `Z` 或显式偏移。数据库仍保存 UTC DATETIME(6)，响应统一输出 UTC `Z`。禁止无时区时间和仅日期输入。
- 所有 `from` 边界包含，所有 `to` 边界不包含；同一组同时提供时要求 `from < to`。
- 成功响应为 `{"data": ..., "request_id": "UUID"}`，并返回同值 `X-Request-ID`。请求可携带合法 UUID 的 `X-Request-ID`；省略或不合法时由服务端生成。
- 返回字段稳定：未知值用 `null`，没有作者或图片用 `[]`，不在不同响应中省略同一字段。
- 所有读取响应设置 `Cache-Control: private, no-store`，尤其不能共享缓存临时图片 URL。
- 未声明的查询参数、空关键词、错误枚举及不合法游标均返回 422，不静默忽略。GET 接口不接收请求体。

使用两个独立凭证：

| 用途 | 配置变量 | 权限 |
|---|---|---|
| 已有采集管理 | CORPUS_API_TOKEN | 现有 /jobs、/schedules、/sources 等管理接口 |
| 读取服务 | CORPUS_READ_API_TOKEN | 本文四个 /v1/* 接口及只读 MCP 工具 |

读取凭证使用 `Authorization: Bearer <token>`，至少 24 字符。读取令牌不能触发采集；管理令牌也不自动等同于读取令牌。Agent 只接收读取凭证，不接收数据库、存储密钥或管理凭证。现有未版本化接口及其响应格式不在此次契约中变更。

## 3. 来源清单

`GET /v1/sources`，无查询参数。

返回配置中的全部 29 个来源（未来以配置为准），按完整 ID 升序排列。包括暂未入库的来源，不因当前 MySQL 只有少数来源记录而少返回。数据库有统计时使用其数据；尚无来源记录时 `n_docs=0`、时间为空。配置中未登记的历史来源不进入清单，也不接受作为文章查询条件。

每项字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| id | string | 完整来源 ID，如 web:servethehome |
| name | string | 配置中的来源名称 |
| channel | string | 当前固定 web |
| n_docs | integer | 当前已入库文章数 |
| first_ts | string / null | 最早的已知文章发布时间 |
| last_ts | string / null | 最新的已知文章发布时间 |

来源数量为零不代表来源不存在，数量大于零也不证明本轮采集正常。接口不暴露来源信任等级、内部备注、采集状态文件或运行错误。

## 4. 文章列表与筛选

`GET /v1/articles`

| 参数 | 类型 | 默认与规则 |
|---|---|---|
| q | string | 可选；去掉首尾空白后为 1–200 字符，仅匹配标题，大小写不敏感 |
| source_id | 重复 string 参数 | 可选；完整来源 ID，最多 29 个、不重复；来源间 OR，省略表示全部配置来源 |
| content_state | string | 可选；ready / preview / no_text，省略时均包含 |
| published_from / published_to | 时间 | 可选；文章发布区间 |
| ingested_from / ingested_to | 时间 | 可选；首次入库区间 |
| sort | string | published_at 或 ingested_at，默认 published_at |
| page_size | integer | 默认 20，范围 1–100 |
| cursor | string | 可选；上一页返回的不透明游标，最多 2048 字符 |

不同条件之间为 AND。例如“今天入库且上周发布”的文章，需要同时满足两个时间区间。`q` 中的 `%`、`_` 按普通文字处理，不是 SQL 通配符；不接收 SQL、正则或搜索表达式。未知来源返回 422；已登记但无文章的来源返回成功空列表。

排序为所选时间倒序，再按文章 ID 的二进制字典序倒序。`published_at` 为空的文章排在有发布时间文章之后；使用发布时间筛选时，未知发布时间的文章不匹配。按 `ingested_at` 排序时，发布时间为空不影响排序。

列表每项包括 `id`、`source_id`、`source_name`、`title`、`authors`、`published_at`、`ingested_at`、`url`、`content_state`、`summary`。不返回全文或签名图片地址。`summary` 为已存正文压缩连续空白后截取前 300 个 Unicode 字符；不是 AI 生成摘要，`no_text` 时为 `null`。

`data.page` 仅返回 `next_cursor` 和 `has_more`，不计算总条数。末页和空列表均为 `next_cursor=null`、`has_more=false`。

游标规则：

1. 服务端生成并签名，绑定规范化后的所有筛选条件、排序、页大小及最后一项排序键。调用方不可自行生成或修改。
2. 下一页继续传递同样的条件和页大小，仅更新 cursor；更换条件时从第一页重新开始。等价时区时间、来源顺序在绑定前规范化。
3. 最长有效 24 小时，过期、签名错误、条件不匹配均返回 422，错误码 INVALID_ARGUMENT。
4. 首次请求固定入库时间上界，减少翻页中新增文章造成的漂移；后续页也使用此上界。游标分页不提供数据库事务级快照，正文状态、发布时间等被更新时仍可能改变结果，批量消费者按文章 ID 去重。
5. 不以可修改的明文偏移量作为游标；签名使用独立服务端配置 CORPUS_CURSOR_SECRET，不把读令牌或数据库密码放入游标。

北京时间 2026-11-17 的新增文章区间，应使用 `ingested_from=2026-11-16T16:00:00Z`、`ingested_to=2026-11-17T16:00:00Z`。需要“这一天发布的文章”时改用 published 参数。

下面是合成的响应示例，不代表实际采集文章：

```json
{
  "data": {
    "items": [
      {
        "id": "web:servethehome:example001",
        "source_id": "web:servethehome",
        "source_name": "ServeTheHome",
        "title": "NVIDIA 网络设备分析（接口示例）",
        "authors": [],
        "published_at": "2026-11-17T01:00:00Z",
        "ingested_at": "2026-11-17T01:05:00Z",
        "url": "https://example.test/articles/example001",
        "content_state": "ready",
        "summary": "这是一段示例正文。[图1]"
      }
    ],
    "page": {"next_cursor": null, "has_more": false}
  },
  "request_id": "7afba495-ff09-414d-bf7a-159093ac397e"
}
```

列表和 `search_articles` 支持 `industry`、`theme` 标签值筛选。两项同时提供时取交集。已打标的文章在列表和详情附带 `labels`，每项包含 `label_type`、`label_value`、`is_primary`。

设置 `merge_duplicates=true` 可合并筛选结果中完整正文相同的文章。每组按请求的排序取首条并返回不同来源数 `source_count`，分页也按合并后的结果计算。默认逐篇返回。

## 5. 文章详情

`GET /v1/articles/{id}`

返回列表中的共同文章字段，但将 `summary` 换成 `content`，并增加 `images`，不重复返回摘要。文章不存在或所属来源已不在可读取的配置清单内时返回 404。

| content_state | content | 调用方含义 |
|---|---|---|
| ready | 完整纯文本正文，保留 [图N] | 已采集全文 |
| preview | 已采集到的公开预览，保留 [图N] | 内容不完整，分析时必须说明 |
| no_text | null | 只有标题等信息，不应生成全文结论 |

API 不再次猜测或升级正文完整性，不绕过付费墙，不生成缺失内容。正文不静默截断；MCP 调用方应先筛选摘要、再按需逐篇取详情。提供原文 `url` 和文章 ID，供分析结果引用。

`images` 与下节的图片项格式一致，按 position 升序。无图时为空数组。

## 6. 配图与临时访问地址

`GET /v1/articles/{id}/images`

返回 `data.article_id` 和 `data.items`。文章存在但没有图片时返回 200 空列表，文章不存在返回 404。该接口用于看板单独加载图片，或在临时地址过期时刷新地址。

| 字段 | 类型 | 规则 |
|---|---|---|
| position | integer | 与正文 [图N] 对应，不重新编号，不因失败丢弃位置 |
| status | string | 原样返回 pending / ok / failed |
| width / height | integer / null | 已知宽高，不现场计算，不补造 |
| url | string / null | ok 时根据 file_key 生成签名 GET 地址；其他状态为空 |
| expires_at | 时间 / null | 对应地址过期时间；url 为空时也为空 |

第一版签名有效期固定 900 秒，以服务端生成时间起算。详情和图片接口都会重新签名；调用方收到过期错误后重新请求，不能把临时 URL 当永久文件标识。`file_key`、bucket、原始图片 URL 和存储密钥不作为响应字段。

图片地址必须使用看板浏览器和分析执行环境都能访问的存储域名。使用独立的 CORPUS_IMAGE_PUBLIC_ENDPOINT创建签名客户端，保留内部 XSKY_ENDPOINT 供采集上传使用；两个入口必须指向同一存储服务和 bucket。缺省使用 XSKY_ENDPOINT。不能先签内部地址，再替换域名，否则可能破坏 S3 签名。测试环境内网地址只保证能访问相应内网的客户端可用。

列表接口不签图片地址。详情和图片接口根据数据库状态签名，不对每一张图片额外发 HEAD 请求；因此 status=ok 表示采集时已确认上传，不保证对象后来未被人工删除。获取对象出现错误时，由调用方报告文章 ID 与 position 供 IT 排查，读取接口不会因此修改采集状态。

服务端配置或记录不一致，例如 ok 的记录没有 file_key，应记录内部异常并返回 500；签名服务必要依赖不可用返回 503，不返回虚构地址。

## 7. 错误格式与日志

仅针对本文 `/v1/*` 接口，失败响应格式如下：

```json
{
  "error": {
    "code": "INVALID_ARGUMENT",
    "message": "published_from 必须早于 published_to",
    "request_id": "7afba495-ff09-414d-bf7a-159093ac397e"
  }
}
```

| HTTP 状态 | code | 处理方式 |
|---|---|---|
| 401 | UNAUTHORIZED | 未提供或提供了无效凭证 |
| 403 | FORBIDDEN | 凭证有效但无相应读取权限 |
| 404 | NOT_FOUND | 文章不存在或不在可读取来源范围 |
| 422 | INVALID_ARGUMENT | 修正参数或从第一页重新查询 |
| 500 | INTERNAL_ERROR | 根据 request_id 排查内部错误 |
| 503 | SERVICE_UNAVAILABLE | 数据库等依赖不可用，可稍后重试 |

业务参数错误不是空结果。响应不回显完整请求体、凭证、SQL、堆栈或存储签名。读取接口沿用项目 JSON 日志及脱敏机制，增加 request_id、接口名、耗时、状态码；详情操作可增加 doc_id。客户端断开和依赖异常应记录，日志不保存全文、查询关键词、签名 URL 或认证头。

## 8. MCP 适配约定

提供集中式 Streamable HTTP MCP 服务，入口 `/mcp`，供开发 Agent 和云端分析 Agent 接入；使用官方 SDK 处理协议、初始化和工具调用，不手写一套 JSON-RPC 协议。MCP 入口不属于 OpenAPI 中的四个 REST 路径。

MCP 入口使用同一类只读 Bearer 凭证。适配层将已验证的调用方读取凭证传递给固定的 API 服务；CORPUS_DATA_API_BASE_URL 指定 API 根地址，工具参数不能传入任意 URL、SQL、bucket 或 file_key。独立部署的 MCP 服务只需读取令牌与 API 地址，不需要采集管理凭证、数据库或对象存储密钥。当前两机方案中，API 和 MCP 共用服务器 B 的读取服务进程；API 查询模块使用只读数据库及图片签名配置；独立同步进程使用 sync.env 的读写配置，不加载服务器 A 的管理令牌和采集计划。MCP 适配逻辑仅调用服务器 B 本机 HTTP API。

工具输入参数与 REST 参数同名；REST 的重复 source_id 参数在 MCP 中为字符串数组。get_article 和 get_article_images 的输入为 `{"id":"文章ID"}`。list_sources 输入为空对象。

工具成功结果同时提供：

- `structuredContent`：与 API 成功响应同结构的 JSON，符合工具 outputSchema。
- `content`：同一份 JSON 的文本内容，兼容只读取文本结果的客户端。
- `isError=false`。

工具输入不符合 inputSchema 时由官方 SDK 校验，并返回 isError=true 的工具错误（SDK 错误文本）；API 业务错误转换为 `isError=true` 的 CallToolResult，content 中给出 API 的错误 JSON（保留 request_id），不伪装为成功空数组，且不放入成功 structuredContent。非法 JSON-RPC 消息等协议错误仍按 SDK 协议响应处理。适配层到 API 的网络错误转换为 SERVICE_UNAVAILABLE，使用同一请求 ID 记录诊断；不在工具内部无限重试。

四个工具均声明 readOnlyHint=true、destructiveHint=false、idempotentHint=true、openWorldHint=false，表示读取固定语料范围、不执行采集或外部任意访问；重复读取仍可能返回更新后的文章或新签名 URL。

## 9. 实现顺序与验收

1. 实现 MySQL 只读查询与图片签名服务，再增加 `/v1/*` 路由；参数化 SQL，查询排序字段用固定白名单。来源与文章 ID 保持现有大小写语义。
2. 实现读取鉴权、请求 ID、公共错误格式及日志，不让新接口改变已有采集管理接口的语义。
3. 根据 openapi-read.yaml 验证响应，再实现只调用这些 API 的 MCP 工具适配层。
4. 使用测试库现有文章验收：配置中的 29 个来源完整返回；关键词、两个时间窗口和状态筛选正确；翻页不重复当前稳定数据；全文与数据库一致；[图N] 与图片位置匹配；客户端能通过临时 URL 读取真实图片。
5. 验证空结果、无图、预览、无正文、未知文章、未知来源、无效与过期游标、权限拒绝、下游异常和日志脱敏。MCP 同样验证结构化结果与错误结果。

本文未授权或执行额外建表、加索引或语义索引建设。当前 `doc(source_id,published_at)` 可用于部分查询；入库时间排序、标题 LIKE 等性能需要在实际数据量下评估，确需索引时单独提出变更，不在采集启动中自动修改数据库。
