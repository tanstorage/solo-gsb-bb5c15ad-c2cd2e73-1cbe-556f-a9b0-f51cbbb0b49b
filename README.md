# 邮件归档整理服务（FastAPI）

批量提交邮件头信息（`message_id`、`in_reply_to`、`references`、发送时间、主题，
以及可选的保留截止时间 `retain_until`），服务将其归并为**稳定排序的会话树**，
并给出**重复 ID、缺失引用、引用环**三类诊断。批次请求与归并结果均持久化到 SQLite，
可随时查询。除直接提交结构化字段（`POST /batches`）外，还支持
**RFC 5322 原始邮件头导入**（`POST /imports/rfc5322`，用 Python 标准库解析折行、
重复字段与 RFC 2047 编码词，解析通过后复用同一套归档逻辑一次性写入）。
`POST /batches` 支持携带 `Idempotency-Key` **幂等提交**：相同键与相同请求体重放
首次批次响应且不重复归档，相同键不同请求体返回 409，不携带键则保持原有行为。
此外支持
**会话活跃冻结 / 解除冻结**、按保留期的**到期批量清理**
（删除中间节点时自动压缩会话树，支持 `dry_run` 预览）、**人工会话纠偏**
（`POST /thread-corrections`，带 revision 乐观校验地调整任一有效邮件的父节点）、
**会话响应时限评估**（`POST /thread-sla/evaluate`，只读地逐封判定整棵会话的
响应是否落在时限内），以及**会话主题漂移审计**（`POST /thread-subject-audit`，
只读地按分支基线逐节点判定主题漂移并归并连续偏离分组）。
另有**批次漂移审计**（`GET /batches/{batch_id}/drift`）：以批次不可变快照中的
原始会话树为基线，只读地逐封判定 `deleted` / `id_reused` / `reparented` /
`root_changed` / `unchanged` 并返回各状态计数；
以及**批次撤销预演**（`POST /batches/{batch_id}/rollback-preview`）：只读模拟
撤销某批次首次归档且当前仍归属该批次的有效邮件，按保留期清理的同一套压缩语义
在内存中为存活子孙寻找最近存活祖先，返回待删除 ID、父节点将变化的邮件、
撤销后的根分组与汇总（不写库）。

## 一条命令启动

需要 Docker 20+ 与 Docker Compose v2：

```bash
docker compose up --build
```

启动后服务监听 **8000** 端口：

| 地址 | 说明 |
| --- | --- |
| <http://localhost:8000> | 服务根 |
| <http://localhost:8000/docs> | Swagger UI（可直接发请求调试） |
| <http://localhost:8000/health> | 健康检查，返回 `{"status":"ok"}` |
| <http://localhost:8000/openapi.json> | OpenAPI 描述 |

首次启动会自动在卷内安装环境就绪后的建表全部完成（`app.main` lifespan 中执行幂等 DDL）；
数据库存放在命名卷 `mailarchive-data` 中，容器重建数据不丢。

## 必要配置

均通过环境变量注入，均有默认值：

| 变量 | 默认值（本地）/ Compose 值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `data/mailarchive.db` / `/data/mailarchive.db` | SQLite 数据库文件路径，父目录不存在会自动创建，表自动建立 |

端口通过 Compose 的 `8000:8000` 映射发布；如需换宿主机端口，例如改成本地 9000：

```yaml
ports:
  - "9000:8000"
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/batches` | 提交一批邮件，返回会话树与全部诊断（HTTP 201）；可携带 `Idempotency-Key` 幂等提交（同键同内容重放、同键不同内容 **409**） |
| `POST` | `/imports/rfc5322` | **导入一组 RFC 5322 原始邮件头**：解析折行、重复字段与 RFC 2047 编码词，归一为批次模型后一次性归档（HTTP 201） |
| `GET` | `/batches?limit=50&offset=0` | 批次列表，按提交时间倒序 |
| `GET` | `/batches/{batch_id}` | 取该批次的归档结果快照（与提交响应完全一致） |
| `GET` | `/batches/{batch_id}/drift` | **批次漂移审计**：以该批次快照的原始会话树为基线，只读逐封判定 `deleted` / `id_reused` / `reparented` / `root_changed` / `unchanged` 并返回各状态计数 |
| `POST` | `/batches/{batch_id}/rollback-preview` | **批次撤销预演（只读）**：模拟撤销该批次首次归档且当前仍归属该批次的有效邮件，按保留期清理的同一套压缩语义为存活子孙重挂最近存活祖先，返回待删除 ID、父节点将变化的邮件、撤销后的根分组与汇总；未知批次 **404** |
| `POST` | `/relinks` | 迟到邮件归档后，批量修复此前断链的有效邮件（HTTP 200） |
| `POST` | `/thread-corrections` | **人工会话纠偏**：调整一封有效邮件的父节点（`null` 拆成根），带 revision 乐观校验（HTTP 200） |
| `GET` | `/messages/{message_id}` | 查询单封邮件及其最终父节点（含 `retain_until` 与 `frozen` 状态） |
| `GET` | `/threads/{message_id}` | 查询该邮件所属完整会话树与当前拓扑 `revision`；环邮件返回 **409** |
| `GET` | `/threads/{message_id}/page?limit=&cursor=` | 会话树**游标分页**（先序扁平展开，含 `depth`），适配大型会话 |
| `POST` | `/thread-sla/evaluate` | **会话响应时限评估**：只读逐封判定整棵会话的响应是否落在 `threshold_seconds` 内 |
| `POST` | `/thread-subject-audit` | **会话主题漂移审计**：只读按分支基线逐节点判定主题漂移，并归并连续偏离分组 |
| `POST` | `/retention/freezes` | **活跃冻结**某封有效邮件当前所在的整棵会话（保护范围随会话拓扑自动扩张） |
| `POST` | `/retention/unfreezes` | **解除**以某封邮件为锚点的冻结 |
| `POST` | `/retention/cleanups` | **批量清理**截止时间前到期、且未受冻结保护的有效邮件（可 `dry_run`） |
| `GET` | `/health` | 健康检查 |

提交邮件时每封邮件可选填 `retain_until`（UTC 归一，无时区按 UTC 处理）；
`retain_until` 早于或等于清理 `cutoff` 即视为到期。环内 `invalid` 邮件
不参与冻结与清理。

未知 ID：批次查询、批次漂移审计、**批次撤销预演**、邮件查询、`/relinks` 返回 **404**；请求体不合法返回 **422**；
`/relinks` 若合并候选关系后形成环，整次返回 **409** 且不写库。冻结锚点未知
返回 **404**，锚点为环内邮件返回 **409**；解除一个不存在的冻结返回 **404**。
`POST /batches` 的 `Idempotency-Key` 格式非法（空值 / 含空白 / 非可见 ASCII /
超过 255 字符）返回 **422**；键已被不同请求体使用返回 **409**
`idempotency_conflict` 且不写库。
`POST /thread-sla/evaluate` 锚点邮件未知返回 **404**，锚点为环内邮件返回 **409**。
`POST /thread-subject-audit` 锚点邮件未知返回 **404**，锚点为环内邮件返回 **409**。
`POST /thread-corrections` 的错误码见下节（待调整邮件 / 目标父未知 **404**；
环内邮件、revision 不匹配、自挂/成环均为 **409** 且不写库）。

### 大型会话分页：`GET /threads/{message_id}/page`

完整树 `GET /threads/{message_id}` 行为保持不变；需要遍历大型会话时使用分页接口：

- 首次请求**不带** `cursor`，沿用与完整树完全一致的子节点排序
  （`(sent_at, message_id)` 升序）做**先序展开**；
- 返回扁平 `items`（节点字段与树节点相同，去掉 `children`、增加整数
  `depth`，根为 0）、`root_id`、`limit` 与不透明 `next_cursor`；
- `limit` 限定 **1～200**（默认 50）；取到末页时 `next_cursor` 为 `null`；
- 游标封装**根 ID、下一偏移与 revision**：revision 根据整棵树每个节点的
  `message_id` / `parent_id` / `sent_at` 计算。后续请求**必须重新计算
  revision**——若会话树在此期间被补链（`POST /relinks`）、人工纠偏
  （`POST /thread-corrections`）、保留期清理压缩或新邮件并入而发生变化，
  返回 **409** `{"detail": {"error": "cursor_stale", …}}`，
  调用方应丢弃游标、从第一页重新开始，从而避免跨页漏项或重复；
- 游标格式错误（无法解析、字段缺失/非法、版本不符）返回 **422**；
  游标来自**另一棵会话树**（与路径 `message_id` 当前所属会话不符）同样
  返回 **422**；路径邮件未知返回 **404**；环内邮件返回 **409**。
- 游标为不透明字符串，客户端不应自行解析或构造。

```bash
# 服务监听 8000 端口；首页（无需 cursor）
curl 'http://localhost:8000/threads/A/page?limit=2'
```

```jsonc
{
  "root_id": "A",
  "limit": 2,
  "items": [
    {"message_id": "A", "parent_id": null, "depth": 0,
     "in_reply_to": null, "references": [],
     "sent_at": "2026-09-18T08:00:00+00:00", "subject": "例会通知"},
    {"message_id": "B", "parent_id": "A", "depth": 1,
     "in_reply_to": "A", "references": ["A"],
     "sent_at": "2026-09-18T09:00:00+00:00", "subject": "Re: 例会通知"}
  ],
  "next_cursor": "djEcxY…"          // 原样回传取下一页；末页为 null
}
```

```bash
# 翻页：把上一页的 next_cursor 原样带上
curl 'http://localhost:8000/threads/A/page?limit=2&cursor=djEcxY%E2%80%A6'
```

补链后继续使用旧游标（HTTP **409**，树已变化）：

```json
{
  "detail": {
    "error": "cursor_stale",
    "message": "the thread has changed since the cursor was issued; restart from the first page"
  }
}
```

### 提交示例

```bash
curl -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "messages": [
      {"message_id": "A", "in_reply_to": null, "references": [],
       "sent_at": "2026-09-18T08:00:00Z", "subject": "例会通知"},
      {"message_id": "B", "in_reply_to": "A", "references": ["A"],
       "sent_at": "2026-09-18T09:00:00Z", "subject": "Re: 例会通知"},
      {"message_id": "C", "in_reply_to": "B", "references": ["A", "B"],
       "sent_at": "2026-09-18T10:00:00Z", "subject": "Re: 例会通知"},
      {"message_id": "D", "in_reply_to": "lost-123", "references": ["ghost-9"],
       "sent_at": "2026-09-18T11:00:00Z", "subject": "断链邮件"},
      {"message_id": "X", "in_reply_to": "Y", "references": ["Y"],
       "sent_at": "2026-09-18T12:00:00Z", "subject": "环一"},
      {"message_id": "Y", "in_reply_to": "X", "references": ["X"],
       "sent_at": "2026-09-18T13:00:00Z", "subject": "环二"}
    ]
  }'
```

### 响应结构

```jsonc
{
  "batch_id": "…",                 // 批次主键，查询接口据此取快照
  "submitted_at": "…UTC ISO8601",
  "totals": {                      // 计数：提交数 / 新唯一数 / 历史重复 / 环内无效 / 树数 …
    "submitted": 6, "unique_new": 6, "archived_duplicates": 0,
    "invalid": 2, "missing_reference_messages": 2, "cycles": 1, "trees": 2
  },
  "threads": [                     // 稳定排序的会话树（见下）
    {
      "message_id": "A",
      "parent_id": null,          // 根节点
      "in_reply_to": null,
      "references": [],
      "sent_at": "2026-09-18T08:00:00+00:00",
      "subject": "例会通知",
      "children": [ { "message_id": "B", "parent_id": "A", "children": [ … ] } ]
    }
  ],
  "duplicates": [                  // 重复 message_id（本批重复 + 历史归档重复）
    {"message_id": "Q", "archived": true,  "first_submitted_seq": null, "occurrences": [2]}
  ],
  "missing_references": [          // 每封邮件引用中、已知集合里不存在的去重 ID
    {"message_id": "D", "missing": ["ghost-9", "lost-123"]}
  ],
  "cycles": [                      // 引用环：整环成员（环序，环内最小 ID 起头）
    {"index": 1, "members": ["X", "Y"], "invalid_messages": ["X", "Y"]}
  ],
  "invalid_messages": [            // 被判 invalid 的邮件明细及原因
    {"message_id": "X", "reason": "reference_cycle", "cycle_index": 1, "…": "…"}
  ]
}
```

> 会话树节点（`threads` 与分页 `items`）沿用固定字段集，不内嵌
> `retain_until` / 冻结状态——这两项通过 `GET /messages/{id}` 查询；批次快照
> 的结构因此保持稳定，归档后永不改写。

### 幂等提交：`Idempotency-Key` 请求头

网络重试或客户端超时重发时，可在 `POST /batches` 请求头携带
`Idempotency-Key`，服务以**请求体摘要 + 键**在单个写事务内原子登记首次结果，
保证同一逻辑提交只归档一次：

- **首次提交**（键第一次出现）：正常归并归档、返回 **201**，响应头
  `Idempotency-Replayed: false`，并在同一事务内登记「键 → 请求体摘要 → 批次」；
- **同键 + 同内容重放**：原样返回**首次批次响应**（同一个 `batch_id` /
  `submitted_at` / 诊断，HTTP 仍为 **201**），响应头
  `Idempotency-Replayed: true`；**不会再次归档**——即使首封邮件此时已在库中，
  重放结果也不会把它们记成“历史归档重复”，而是逐字节复现首次快照（与
  `GET /batches/{batch_id}` 一致）；
- **同键 + 不同内容**：返回 **409**
  `{"detail": {"error": "idempotency_conflict", …}}`，**不写库**；冲突不占用
  键，随后以原内容重放仍成功；
- **不携带键**：保持原有行为，每次提交都正常归档一批。

“相同内容”按**归一化后的请求体**比较（对摘要做 sha256）：与请求 JSON 的
排版空白、字段书写顺序无关；时间先统一换算到 UTC（naive 视为 UTC），因此
`2026-09-18T09:00:00Z` 与 `2026-09-18T09:00:00+00:00` 视为相同内容，而
`2026-09-18T09:00:00+01:00`（晚一小时）是不同内容；邮件数组的**顺序参与
比较**（顺序不同即 409）。

**键格式**：1～255 个可见 ASCII 字符（`!`～`~`，不含空格 / 制表符等空白与
控制字符）；空值、含空白、含非 ASCII 或超长的键返回 **422**，此时不归档、
也不登记键。建议使用 UUID 等客户端生成的不透明随机串。

并发语义安全：同键请求并发到达时，登记与归档在**同一写事务**
（`BEGIN IMMEDIATE`）内完成——仅一个请求完成首次归档，其余并发请求要么重放
首次结果（内容一致），要么收到 409（内容不同），不会出现重复批次。
幂等仅作用于 `POST /batches`；`POST /imports/rfc5322` 不读取该头。

```bash
# 首次提交
curl -i -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 7f3c9b1e-9a2d-4e6f-8b0a-1c2d3e4f5a6b' \
  -d '{"messages":[{"message_id":"A","in_reply_to":null,"references":[],
       "sent_at":"2026-09-18T08:00:00Z","subject":"例会通知"}]}'
# -> HTTP 201，响应头 Idempotency-Replayed: false

# 同键同内容重试：重放首次响应，不重复归档
curl -i -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 7f3c9b1e-9a2d-4e6f-8b0a-1c2d3e4f5a6b' \
  -d '{"messages":[{"message_id":"A","in_reply_to":null,"references":[],
       "sent_at":"2026-09-18T08:00:00Z","subject":"例会通知"}]}'
# -> HTTP 201，响应头 Idempotency-Replayed: true，响应体与首次完全一致
```

同键不同内容时（HTTP **409**，不写库）：

```json
{
  "detail": {
    "error": "idempotency_conflict",
    "message": "Idempotency-Key has already been used with a different request payload"
  }
}
```

### RFC 5322 原始邮件头导入：`POST /imports/rfc5322`

不方便预先把邮件头拆成结构化字段时，可直接提交**一组 RFC 5322 原始头文本**
（每个数组元素一封邮件，正文可带可不带——空行之后的内容一律忽略）。服务仅用
Python 标准库（`email` + `policy.default`）解析并归一为与 `/batches` 完全一致的
批次模型，随后**复用同一套归档服务**一次性写入，响应即标准 `BatchResult`。

服务端口仍是 **8000**：

```bash
curl -X POST http://localhost:8000/imports/rfc5322 \
  -H 'Content-Type: application/json' \
  -d '{
    "headers": [
      "Message-ID: <root@example.com>\r\nDate: Fri, 18 Sep 2026 08:00:00 +0000\r\nSubject: =?UTF-8?B?5L2g5aW9?= =?UTF-8?Q?=20World?=\r\n\r\n正文忽略\r\n",
      "Message-ID: <reply@example.com>\nIn-Reply-To: <root@example.com>\nReferences: <root@example.com>\n\t<x@external>\nDate: Fri, 18 Sep 2026 17:30:00 +0800\nSubject: Re:\n =?UTF-8?B?5L2g5aW9?= World\n"
    ]
  }'
```

解析规则：

- **折行（folding）**：CRLF 后接空格/制表符的续行自动展开（如第二条的
  `References` 与 `Subject`）；
- **RFC 2047 编码词**：`Subject` 等非结构化字段中的 `=?charset?b/q?…?=`
  自动解码（第一条主题解码为 `你好 World`）；
- **候选 ID**：`Message-ID`、`In-Reply-To`、`References` 均只取尖括号
  token（`<local@domain>`）内的 msg-id，自动剥掉 CFWS（含嵌套注释、折行
  空白），注释或引号里的伪尖括号不会误判；裸文本不作为候选；同一字段
  （或多个同名字段行）出现多个候选时，**严格按头中出现顺序**处理：
  `Message-ID` / `In-Reply-To` 取第一个合法候选，`References` 保留全部
  合法候选（保序、不去重，与 `/batches` 语义一致）；
- **时间归一**：`Date` 按 RFC 5322 解析，带偏移时换算为 UTC，无时区时按 UTC 处理；
- 必填字段为 `Message-ID`、`Date`、`Subject`；`In-Reply-To`、`References` 可缺省。

任一头文本缺失必填字段或字段格式非法，都会被**逐项**拒绝并返回 **422**，
`errors` 中每项带有输入数组的下标 `index`（从 0 起）、`field`
（`message_id` / `date` / `subject`）与 `code`（`missing` / `invalid`）；
**整批校验通过前绝不写库**——任何一项失败，同次请求中的合法项也不会落库：

```json
{
  "detail": {
    "message": "one or more RFC 5322 header blocks are invalid; nothing was written",
    "errors": [
      {"index": 0, "field": "message_id", "code": "missing",
       "message": "required header Message-ID is absent"},
      {"index": 1, "field": "date", "code": "invalid",
       "message": "Date is not a valid RFC 5322 date-time"}
    ]
  }
}
```

全部通过后，导入批次与 `/batches` 提交的批次行为完全一致：同批重复 ID、跨批
历史归档重复、缺失引用与引用环诊断均照常产生，可用 `GET /batches/{batch_id}`
取回同一份快照。`headers` 为空数组返回 **422**。

### 迟到邮件补链：`POST /relinks`

邮件归档时若 `in_reply_to` / `references` 指向的邮件尚未到达，该邮件会断链自成根。
等迟到邮件在后续批次归档后，可用本接口按**原父节点规则**批量修复这些断链：

- 请求体给出待处理 `message_ids`（自动去除空白项、去重并保持顺序；空列表 / 全空白返回 **422**）；
- 每封邮件**仅在当前 `parent_id` 为空**（断链根、且非引用环内 `invalid` 邮件）时处理：
  1. 先取现已存在且有效的 `in_reply_to`；
  2. 否则从 `references` 右向左回退，取最后一个已知项；
  3. 仍无法命中、或邮件本就挂接在树下、或属于引用环，则**保持不变**；
- 所有候选关系会**合并到既有父指针图上统一检测环**：任一变更形成环（含自指）则整次
  返回 **409**（响应体给出环成员），**不写任何库**；
- 无环时在**单事务**内更新 `parent_id`，并按请求顺序返回每封邮件的
  `old_parent_id` / `new_parent_id` / `old_root_id` / `new_root_id`（未变更者新旧值相同），
  `relinked` 为实际发生变更的计数；不存在的 ID 返回 **404** 且不写库。

补链成功后 `GET /threads`、`GET /messages` 随即反映新树；**既有批次结果快照
（`GET /batches/{id}`）与诊断明细保持归档时原样，不做改写。**

```bash
# 1) D 归档时其父 late 尚未到达 -> D 断链为根
curl -X POST http://localhost:8000/batches -H 'Content-Type: application/json' -d '{
  "messages": [
    {"message_id": "D", "in_reply_to": "late", "references": ["ghost"],
     "sent_at": "2026-09-18T09:00:00Z", "subject": "断链邮件"}
  ]
}'

# 2) 迟到邮件归档
curl -X POST http://localhost:8000/batches -H 'Content-Type: application/json' -d '{
  "messages": [
    {"message_id": "late", "in_reply_to": null, "references": [],
     "sent_at": "2026-09-18T07:00:00Z", "subject": "迟到的原邮件"}
  ]
}'

# 3) 修复断链
curl -X POST http://localhost:8000/relinks -H 'Content-Type: application/json' \
  -d '{"message_ids": ["D"]}'
```

```jsonc
{
  "relinked": 1,
  "results": [
    {
      "message_id": "D",
      "old_parent_id": null,
      "new_parent_id": "late",   // 命中现已存在的 in_reply_to
      "old_root_id": "D",
      "new_root_id": "late"
    }
  ]
}
```

成环时（HTTP **409**，不写库）：

```json
{
  "detail": {
    "message": "relink would create a reference cycle; no changes were written",
    "cycles": [["p", "q"]]
  }
}
```

### 人工会话纠偏：`POST /thread-corrections`

自动归并可能因邮件头缺失 / 迟到而挂错会话；本接口允许运维**人工调整任一有效
邮件的父节点**，把邮件移到另一封有效邮件下，或传入 `new_parent_id: null`
把该邮件（连同其子树）**拆成新的根**。

`GET /threads/{message_id}` 的响应在 `root_id` / `tree` 之外还带有当前整棵
会话拓扑的 `revision`（与分页游标使用同一摘要：按每个节点的
`message_id` / `parent_id` / `sent_at` 计算）：

```bash
curl http://localhost:8000/threads/C
```

```jsonc
{
  "root_id": "A",
  "revision": "9f1a…（64 位十六进制）",
  "tree": { "message_id": "A", "parent_id": null, "children": [ … ] }
}
```

纠偏时把读到的 revision 原样放入 `expected_revision`，作为**乐观并发校验**：

- 仅允许调整**有效邮件**（引用环之外），目标父节点也必须有效（存在且非环内
  `invalid` 邮件）：待调整邮件不存在 **404**；目标父不存在 **404**
  （`detail.new_parent_id` 给出该 ID）；任一为环内邮件 **409**；
- revision 必须等于该邮件**当前所在整棵会话拓扑**的 revision；自读取后若
  会话被补链、纠偏、清理压缩或新邮件并入而改变，返回 **409**
  `{"detail": {"error": "revision_conflict", "current_revision": "…"}}`，
  **不写库**；调用方应重新 `GET /threads` 取得 `current_revision` 后重试；
- **禁止自挂**（`new_parent_id` 等于 `message_id`，构成单节点环），**禁止
  挂到自身后代下**形成引用环：候选关系合并到既有父指针图上统一检测，命中时
  整次返回 **409**（`detail.cycles` 给出环成员）且**不写库**；
- 校验通过后在**单个事务**内更新 `parent_id`（仅父指针，不改动邮件头
  `in_reply_to` / `references`），返回节点调整前后的父节点、根节点以及
  **新 revision**（随即 `GET /threads/{message_id}` 得到的就是同一值）；
  目标与当前父节点相同属于无变更操作，revision 保持不变；
- **既有批次结果快照与诊断明细永不改写**（`GET /batches/{id}` 仍返回归档
  时原样）；纠偏前领取的分页游标会收到 **409** `cursor_stale`；
- **活跃冻结按新拓扑自然生效**：锚点不保存成员快照，邮件被移入受冻结会话即
  自动受保护，被拆出 / 移出受冻结会话即自动失去保护，无需重新冻结。

```bash
# 把 C 从 A 树下移到 Q 下（Q 在另一棵会话中）
curl -X POST http://localhost:8000/thread-corrections \
  -H 'Content-Type: application/json' \
  -d '{
    "message_id": "C",
    "new_parent_id": "Q",
    "expected_revision": "9f1a…（GET /threads/C 读到的 revision）"
  }'
```

```jsonc
{
  "message_id": "C",
  "old_parent_id": "A",   // 调整前的父节点
  "new_parent_id": "Q",   // 调整后的父节点（为 null 表示已拆成根）
  "old_root_id": "A",     // 调整前所在会话的根
  "new_root_id": "P",     // 调整后所在会话的根
  "revision": "3c2b…"     // 调整后新会话拓扑的 revision
}
```

拆成根时把 `new_parent_id` 置为 `null`（该字段必填；空字符串返回 **422**）：

```bash
curl -X POST http://localhost:8000/thread-corrections \
  -H 'Content-Type: application/json' \
  -d '{"message_id": "C", "new_parent_id": null, "expected_revision": "9f1a…"}'
```

revision 不匹配时（HTTP **409**，不写库）：

```json
{
  "detail": {
    "error": "revision_conflict",
    "message": "expected_revision does not match the current thread topology; no changes were written",
    "current_revision": "d04e…"
  }
}
```

### 会话响应时限评估：`POST /thread-sla/evaluate`

以会话内**任意一封有效邮件为锚点**，对其**当前所在的整棵有效会话**逐封判定响应
是否落在时限内。接口**只读**：不写库、不改写批次快照与诊断明细。

请求体：`message_id`（评估锚点）、`as_of`（评估时刻，无时区按 UTC 处理）、
`threshold_seconds`（响应时限阈值，秒，不得为负）。逐封判定规则：

- **有效响应**：仅该邮件的**直接子邮件**中、`sent_at` **不早于**父邮件 `sent_at`
  者可以作为响应；有多封时取**最早**的一封（并列按 `message_id`，与稳定排序一致）。
  孙邮件及更深的后代不算响应；
- **时序异常**：`sent_at` 早于父邮件的直接子邮件列入 `chronology_anomalies`，
  **不得作为响应、不消除超时**；
- **已有有效响应**：响应耗时 `response_delay_seconds <= threshold_seconds` 标记
  `within_sla`，否则 `late`（边界含等于）；
- **无有效响应**：以 `as_of` 计算年龄 `age_seconds = as_of - sent_at`，
  未超阈值标记 `pending`，超过阈值标记 `overdue`。

响应包含评估基于的当前整棵会话拓扑 `revision`（与 `GET /threads/{message_id}`
一致）、按**稳定先序**排列的逐封结果 `results`、按状态分类的计数汇总 `summary`，
以及 `chronology_anomalies`。锚点邮件未知返回 **404**；锚点为环内 `invalid`
邮件（不属于任何会话）返回 **409**。

```bash
curl -X POST http://localhost:8000/thread-sla/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"message_id": "C", "as_of": "2026-09-18T11:00:00Z", "threshold_seconds": 3600}'
```

```jsonc
{
  "message_id": "C",
  "root_id": "A",
  "as_of": "2026-09-18T11:00:00Z",
  "threshold_seconds": 3600.0,
  "revision": "9f1a…",                 // 与 GET /threads/C 相同的拓扑摘要
  "results": [                          // 稳定先序：A -> B -> C
    {"message_id": "A", "parent_id": null, "depth": 0,
     "sent_at": "2026-09-18T08:00:00Z", "status": "within_sla",
     "response_message_id": "B", "response_sent_at": "2026-09-18T08:30:00Z",
     "response_delay_seconds": 1800.0, "age_seconds": null},
    {"message_id": "B", "parent_id": "A", "depth": 1,
     "sent_at": "2026-09-18T08:30:00Z", "status": "late",
     "response_message_id": "C", "response_sent_at": "2026-09-18T10:30:00Z",
     "response_delay_seconds": 7200.0, "age_seconds": null},
    {"message_id": "C", "parent_id": "B", "depth": 2,
     "sent_at": "2026-09-18T10:30:00Z", "status": "pending",
     "response_message_id": null, "response_sent_at": null,
     "response_delay_seconds": null, "age_seconds": 1800.0}
  ],
  "summary": {"total": 3, "within_sla": 1, "late": 1, "pending": 1, "overdue": 0},
  "chronology_anomalies": []            // sent_at 早于父邮件的直接子邮件
}
```

### 会话主题漂移审计：`POST /thread-subject-audit`

以会话内**任意一封有效邮件为锚点**，对其**当前所在的整棵有效会话**审计主题漂移。
接口**只读**：不写库、不改写批次快照与诊断明细。

请求体仅含 `message_id`（审计锚点）。判定规则：

- **规范主题**：对每封邮件的 `subject` 重复剥离大小写不敏感的
  `Re:` / `Fw:` / `Fwd:` 前缀（须带冒号，可连续叠加，如 `Re: Fw: x` 逐层剥离），
  随后把任意连续空白折叠为单个空格并去除首尾空白；
- **分支基线**：按稳定先序遍历整棵会话，每个节点以其**最近祖先（父节点）的
  规范主题**为分支基线；根节点无基线、恒不偏离（它定义整棵树的初始基线）。
  父节点自身偏离时，子节点以父节点偏离后的新主题为基线——沿用新话题的
  分支不再视为漂移；
- **偏离判定**：节点规范主题与分支基线不同即 `drifted: true`（精确比较）；
- **drift_groups**：把先序序列中**连续偏离**的节点合并为一组，
  `baseline_subject` 为组首节点所偏离的基线。

响应包含审计基于的当前整棵会话拓扑 `revision`（与 `GET /threads/{message_id}`
一致）、按稳定先序排列的逐节点结果 `results`（原主题、规范主题、分支基线、
是否偏离）以及 `drift_groups`。锚点邮件未知返回 **404**；锚点为环内
`invalid` 邮件（不属于任何会话）返回 **409**。

```bash
curl -X POST http://localhost:8000/thread-subject-audit \
  -H 'Content-Type: application/json' \
  -d '{"message_id": "C"}'
```

```jsonc
{
  "message_id": "C",
  "root_id": "A",
  "revision": "9f1a…",                 // 与 GET /threads/C 相同的拓扑摘要
  "results": [                          // 稳定先序：A -> B -> C
    {"message_id": "A", "parent_id": null, "depth": 0,
     "subject": "项目周报", "canonical_subject": "项目周报",
     "baseline_subject": null, "drifted": false},
    {"message_id": "B", "parent_id": "A", "depth": 1,
     "subject": "Re: 项目周报", "canonical_subject": "项目周报",
     "baseline_subject": "项目周报", "drifted": false},
    {"message_id": "C", "parent_id": "B", "depth": 2,
     "subject": "Fwd: 团建通知", "canonical_subject": "团建通知",
     "baseline_subject": "项目周报", "drifted": true}
  ],
  "drift_groups": [                     // 先序连续偏离节点合并成组
    {"members": ["C"], "baseline_subject": "项目周报"}
  ]
}
```

### 批次漂移审计：`GET /batches/{batch_id}/drift`

以**该批次不可变结果快照中的原始会话树**（`threads`）为基线，只读审计这些节点
当前的归档状态。接口**只读**：不写库、不改写批次快照与诊断明细；未知批次返回
**404**。

- **仅审计快照 `threads` 中的节点**（环内 `invalid` 邮件与诊断明细不参与），
  按**快照原始先序**逐封输出 `original_parent_id` / `original_root_id`
  （快照基线）、`current_parent_id` / `current_root_id`（当前归档）与 `status`；
- `status` 依次判定：
  1. **deleted**：当前已不存在（如保留期清理删除），`current_*` 为 `null`；
  2. **id_reused**：同 ID 当前属于其它批次——原归档被清理删除后，同 ID 由更晚
     的批次重新归档（当前行与快照时不是同一归档）；
  3. **reparented**：来源一致（仍是快照时的同一归档）但父节点变化（人工纠偏、
     补链、清理压缩等）；
  4. **root_changed**：父节点未变、仅所在树的根变化（祖先被纠偏或清理压缩）；
  5. **unchanged**：父节点与根均与快照一致；
- 快照树中的**历史祖先**（由更早批次归档、被本批树引用）以来源批次为准，
  未重归档即视为来源一致，不会误判为 `id_reused`；
- `summary` 返回 `total` 与各状态计数。

服务监听 **8000** 端口：

```bash
curl http://localhost:8000/batches/<batch_id>/drift
```

```jsonc
{
  "batch_id": "…",
  "submitted_at": "2026-09-18T08:00:00+00:00",   // 快照基线时刻（批次归档时间）
  "results": [                 // 按快照原始先序逐封输出
    {"message_id": "A", "original_parent_id": null, "original_root_id": "A",
     "current_parent_id": "Q", "current_root_id": "P", "status": "reparented"},
    {"message_id": "C", "original_parent_id": "A", "original_root_id": "A",
     "current_parent_id": "A", "current_root_id": "P", "status": "root_changed"},
    {"message_id": "D", "original_parent_id": "C", "original_root_id": "A",
     "current_parent_id": null, "current_root_id": null, "status": "deleted"}
  ],
  "summary": {"total": 3, "deleted": 1, "id_reused": 0,
              "reparented": 1, "root_changed": 1, "unchanged": 0}
}
```

### 批次撤销预演：`POST /batches/{batch_id}/rollback-preview`

在**不写库**的前提下，模拟“撤销”一个批次：删除该批次**首次归档且当前仍归属
该批次的有效邮件**，并在内存中按与保留期清理**完全相同的压缩语义**为存活子孙
寻找最近存活祖先。接口**只读**，可重复调用、结论幂等；未知批次返回 **404**。

- **拟删除范围**（`deleted_ids`，升序）：仅当前 `messages` 表中
  `batch_id == 该批次` 且为有效（非环内 `invalid`）的邮件。以下三类**不处理**：
  1. 快照树中的**历史祖先**——由更早批次归档（`batch_id` 不同），撤销本批时
     它们继续作为存活祖先保留；
  2. 本批归档的**环内 invalid 邮件**——从不参与有效会话森林（计入
     `summary.skipped_invalid_count`）；
  3. **已被更晚批次重用的同 ID**——原归档已被保留期清理删除、同 ID 由更晚批次
     重新归档（当前行 `batch_id` 已变更），撤销不得误删新归档（计入
     `summary.skipped_reused_count`）；
- **压缩**（与 `POST /retention/cleanups` 共用同一纯函数实现）：存活节点若父
  节点被删，则沿父链跳过全部被删节点重挂到**最近存活祖先**；链顶没有存活祖先
  时**成为新根**。撤销不引入保留期 / 冻结概念——候选有效邮件一律视为待删除，
  冻结保护不影响预演结论；
- `affected`：父节点或根节点**将变化**的存活邮件，逐封给出
  `old_parent_id` / `new_parent_id` / `old_root_id` / `new_root_id`，按
  `message_id` 升序（祖先链中间被删只改父指针；根被删时整棵剩余子树的根随之改变）；
- `root_groups`：**撤销后的根分组**，只覆盖受撤销触及旧会话的存活子孙（完全
  无关的其它会话不出现）；组按新根的稳定顺序 `(sent_at, message_id)` 排列，
  组内 `members` 按撤销后结构稳定先序排列，并给出组大小 `size`；
- `summary`：`deleted_count`（拟删除数）、`affected_count`（父/根将变化数）、
  `skipped_invalid_count` / `skipped_reused_count`（上面两类跳过数）、
  `touched_tree_count`（删除集压缩前所属旧会话树去重数）、
  `root_group_count`（撤销后根分组数）、`surviving_count`（受触及旧会话中
  撤销后仍存活的邮件数，即各分组成员总数）。

```bash
curl -X POST http://localhost:8000/batches/<batch_id>/rollback-preview
```

对上例（拟撤销批次拥有 `B`/`C`，更早批次归档了历史祖先 `H`，更晚批次归档了
`D`/`E`；当前结构 `H -> B -> C -> D`、`H -> B -> E`）：

```jsonc
{
  "batch_id": "…",
  "submitted_at": "2026-09-18T08:00:00+00:00",
  "deleted_ids": ["B", "C"],        // 仅本批当前仍归属本批的有效邮件
  "affected": [                     // 存活子孙重挂到最近存活祖先 H
    {"message_id": "D", "old_parent_id": "C", "new_parent_id": "H",
     "old_root_id": "H", "new_root_id": "H"},
    {"message_id": "E", "old_parent_id": "B", "new_parent_id": "H",
     "old_root_id": "H", "new_root_id": "H"}
  ],
  "root_groups": [                  // 撤销后：H 为唯一新根
    {"root_id": "H", "size": 3, "members": ["H", "D", "E"]}
  ],
  "summary": {"deleted_count": 2, "affected_count": 2,
              "skipped_invalid_count": 0, "skipped_reused_count": 0,
              "touched_tree_count": 1, "root_group_count": 1, "surviving_count": 3}
}
```

预演**绝不写库**：批次快照、诊断明细、`messages` 表（父指针、`retain_until`、
`batch_id` 等）与会话冻结状态在调用前后完全一致；本服务也不提供真正执行撤销
的接口。

## 保留期：冻结与到期清理

归档时可给邮件设置可选的 `retain_until`（保留截止时间）：

```bash
curl -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "messages": [
      {"message_id": "A", "in_reply_to": null, "references": [],
       "sent_at": "2026-09-10T08:00:00Z", "subject": "季度公告",
       "retain_until": "2026-10-01T00:00:00Z"},
      {"message_id": "B", "in_reply_to": "A", "references": ["A"],
       "sent_at": "2026-09-10T09:00:00Z", "subject": "Re: 季度公告",
       "retain_until": "2026-09-15T00:00:00Z"},
      {"message_id": "C", "in_reply_to": "B", "references": ["A", "B"],
       "sent_at": "2026-09-10T10:00:00Z", "subject": "Re: 季度公告"}
    ]
  }'
```

`GET /messages/{id}` 会返回 `retain_until`，以及该邮件当前是否处于冻结保护
（`frozen`：与任一冻结锚点同属一棵当前会话树即为 `true`，随拓扑实时变化）。

### 冻结 / 解除冻结：`POST /retention/freezes`、`POST /retention/unfreezes`

以会话内**任意一封有效邮件为锚点**发起冻结，会活跃冻结其**当前所在的整棵会话**
（返回的 `members` 为调用时刻的当前会话成员，按先序排列）：

```bash
# 以深层邮件 C 为锚点：A、B、C 全部进入保护
curl -X POST http://localhost:8000/retention/freezes \
  -H 'Content-Type: application/json' -d '{"message_id": "C"}'
```

```json
{
  "anchor_id": "C",
  "root_id": "A",
  "frozen_at": "2026-09-19T03:00:00Z",
  "created": true,
  "members": ["A", "B", "C"]
}
```

冻结语义（**活跃冻结**，不保存成员快照）：

- 冻结只记录锚点，保护范围是锚点**当前**所在的整棵会话，随会话拓扑实时扩张：
  此后经**新批次归档**、**`POST /relinks`** 或**人工纠偏
  （`POST /thread-corrections`）**并入该会话的邮件自动受保护；被纠偏拆出 /
  移出该会话的邮件则自动失去保护，均无需再次冻结；
- 对同一锚点再次调用即为**刷新**（`created: false`），仅更新 `frozen_at`；
  响应中的 `members` 始终反映调用时刻的当前会话成员；
- 同一棵会话可以由不同锚点多次冻结，保护取**并集**；解除其中一个锚点
  **不会释放**仍被其它锚点覆盖的邮件；
- 锚点邮件不存在返回 **404**；锚点是引用环内 `invalid` 邮件（不属于任何会话）
  返回 **409**。

解除冻结以**锚点 ID** 调用（冻结成员不是锚点，不能代为解除）：

```bash
curl -X POST http://localhost:8000/retention/unfreezes \
  -H 'Content-Type: application/json' -d '{"message_id": "C"}'
```

```jsonc
{
  "anchor_id": "C",
  "root_id": "A",
  "frozen_at": "2026-09-19T03:00:00Z",
  "released_members": ["A", "B", "C"],   // 解除后不再被任何冻结覆盖的成员
  "still_protected": []                  // 仍被其它冻结覆盖的成员（多冻结重叠时）
}
```

`released_members` 与 `still_protected` 均按**解除时刻的当前保护关系**计算：
该锚点当前会话的成员中，不再被任何剩余冻结覆盖的进入 `released_members`，
仍被其它锚点覆盖的进入 `still_protected`（清理时依旧受保护）。
不存在以该邮件为锚点的冻结时返回 **404**。

### 批量清理：`POST /retention/cleanups`

请求体给出截止时刻 `cutoff`（缺省为当前 UTC）与 `dry_run` 开关：

- 删除条件：`is_invalid = 0`（有效邮件）**且** `retain_until` 非空
  **且** `retain_until <= cutoff`（边界包含）**且**不被任何活跃冻结覆盖；
- 保护集合按**当前父子关系**实时计算（各冻结锚点当前所在会话的并集）；
  `dry_run` 与实际执行在**同一事务视图**中用同一算法展开保护集合，结论一致；
- 未设置 `retain_until` 的邮件永不因到期被删；环内 `invalid` 邮件从不参与；
- **会话压缩**：删除节点后，仍保留的后代沿父链跳过全部被删节点，重挂到
  **最近的保留祖先**；链顶没有存活祖先时**成为新根**（同一套压缩算法也被
  `POST /batches/{batch_id}/rollback-preview` 用于批次撤销预演）；
- 响应返回删除 ID（`deleted_ids`）以及每个受影响存活节点的
  `old_parent_id` / `new_parent_id` / `old_root_id` / `new_root_id`
  （祖先链中间被删只改父指针；根被删时整棵剩余子树的根随之改变）；
- `dry_run: true` 与实际清理**使用完全相同的算法**，只计算并返回结果、**不写库**；
- 实际清理在**单事务**内完成全部重挂与删除，要么全部生效、要么全部回滚；
- 清理后 `GET /threads`、`GET /threads/.../page`、`GET /messages` **立即**反映
  压缩后的结构（压缩前领取的分页游标会收到 **409** `cursor_stale`，重新从首页
  开始即可）；**既有批次结果快照 `GET /batches/{id}` 永不改写**，即使其中的
  节点已被删除。

先预览（`dry_run`，不写库）：

```bash
curl -X POST http://localhost:8000/retention/cleanups \
  -H 'Content-Type: application/json' \
  -d '{"cutoff": "2026-09-20T00:00:00Z", "dry_run": true}'
```

对上例（B 到期；A 保留到 10 月；C 未设保留期）：

```jsonc
{
  "dry_run": true,
  "cutoff": "2026-09-20T00:00:00Z",
  "deleted_ids": ["B"],
  "affected": [
    {
      "message_id": "C",
      "old_parent_id": "B",
      "new_parent_id": "A",   // 中间节点 B 被删 -> C 重挂到最近保留祖先 A
      "old_root_id": "A",
      "new_root_id": "A"
    }
  ],
  "deleted_count": 1,
  "affected_count": 1,
  "expired_count": 1,         // 有效邮件中已到期的数量
  "protected_count": 0        // 已到期但被冻结保护的数量
}
```

确认后去掉 `dry_run`（或置 `false`）实际执行；重复执行是幂等的——已删除的
节点不会再出现，新到期前的第二次调用为空操作。

## 归并规则

- **父节点确定**
  1. 优先采用有效的 `in_reply_to`：指向已知邮件（本批或历史已归档）即采用；
  2. 否则从右向左扫描 `references`，取其中**最后一个已知项**；
  3. 都命不中（含 `in_reply_to`/`references` 指向不存在的 ID）即为断链邮件，**自成根节点**。
- **引用环**：候选父子关系构成功能图（每封新邮件至多一个候选父）。
  检测到环时，**环上所有邮件整体标记为 `invalid`，绝不部分挂接**（不会下沉成根、
  不会挂到其它树）；随后引用了该环的邮件会回到 `references` 中重新寻找其它有效父节点，
  重复检测直至无环。自引用（`in_reply_to` 指向自身）视为单节点环。
- **重复 ID**：同批次重复出现时仅采用首次出现的内容参与归并；若 ID 已在历史批次归档，
  本次不重复落库。两种情况都会在 `duplicates` 中标注。
- **稳定排序**：根之间、同一父节点的子节点之间统一按
  `(sent_at UTC, message_id)` 升序，先序输出，结果全序确定、可复现。
- **跨批次**：历史邮件是合法的父节点。归档时历史父指针不可变，因此引用环只可能完全位于
  同一批新邮件内部；归档后迟到邮件可经 `POST /relinks` 修复断链根（见上），修复前会把全部
  候选关系合并到既有图上统一检测环，绝不允许写后成环。
- **时间**：`sent_at` 无时区时按 UTC 处理，有偏移时换算为 UTC 存储与排序。

## 持久化

SQLite（WAL 模式），表在服务启动时自动创建：

- `batches`：批次元数据与计数；
- `messages`：每封唯一邮件、最终 `parent_id`、是否 `is_invalid`、可选 `retain_until`；
- `batch_duplicates` / `batch_missing_refs` / `batch_cycles`：诊断明细；
- `batch_results`：完整结果 JSON 快照，`GET /batches/{id}` 原样返回（补链、人工
  纠偏、保留期清理只更新/删除 `messages`，快照与诊断表永不改写）；
- `idempotency_keys`：`POST /batches` 幂等登记（键为主键，存请求体摘要与对应
  `batch_id`）；与批次结果在同一写事务内登记，保证并发下只归档一次；
- `retention_freezes`：冻结锚点（活跃冻结不存成员快照；保护集合在使用时按当前
  父子关系实时展开锚点所在会话计算）。

写操作在进程内串行化（线程锁 + 单连接写事务），保证并发提交 / 补链 / 纠偏 /
冻结 / 清理下的归并一致性；幂等登记与批次归档、补链、人工纠偏、保留期实际
清理的全部父指针变更与删除均在**单个事务**内提交。

服务启动的建表 DDL 幂等；早期数据库（`messages` 无 `retain_until` 列、含旧版
冻结成员快照表 `retention_freeze_members`）会在启动时自动补列、并删除已废弃的
快照表（冻结锚点保留，保护范围自升级起随当前会话拓扑实时计算），无需手工迁移。

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 服务监听 8000 端口：http://localhost:8000  （/docs 为 Swagger UI；
# DATABASE_PATH 默认 data/mailarchive.db）
```

运行测试：

```bash
pip install pytest
python -m pytest -q
```
