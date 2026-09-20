# Live Subtitle Finalizer — 直播字幕定稿服务

基于 FastAPI + SQLite 的直播字幕定稿服务。客户端按频道提交带**序号 / 修订号 / 文本**的字幕片段，服务将乱序到达的片段放入缓冲区，只从序号 1 起按**连续前缀**推进水位线并生成**不可再修改**的定稿字幕。操作员可对确认永久丢失的序号区间发起**缺口裁决**，服务在同一事务中记录缺口并释放紧随其后的连续缓冲片段。已定稿字幕可通过**追加式更正层**修正：更正只追加新版本、永不改写原定稿，并提供原文 / 生效双视图查询与逐版本审计。

## 语义规则

- 片段可乱序到达；同一序号只接受**更高修订号**（更低或相等的修订号被拒绝）。
- 水位线 = 已定稿的最大连续序号（定稿字幕与永久缺口共同构成连续前缀）。遇到缺口即停，**不会越过缺口**定稿。
- 缺口被补齐的提交会在**同一事务**内一次释放整段连续片段。
- 序号 ≤ 当前水位线的提交一律不生效（定稿不可变，已裁决为缺口的序号同样不可再提交）。
- 所有提交 / 裁决 / 更正在 `BEGIN IMMEDIATE` 事务内串行执行，并发下不会重复定稿、跳过缺口或重复追加同一更正版本。

### 永久缺口裁决规则

- 区间 `[start_seq, end_seq]`（含两端）的**起点必须等于当前水位线下一位**；
- 区间内**不得存在已缓冲片段**（有缓冲说明片段只是迟到，应等待定稿而不是裁决丢失）；
- 任一校验失败返回 `applied=false` 且**不产生任何写入**；
- 裁决成功：同一事务内记录被跳过序号、推进水位线到区间终点，并继续释放紧随其后的连续缓冲片段；
- 重复提交**已完整裁决的同一区间**幂等返回当前结果（`idempotent=true`）；与既有裁决**部分重叠**则拒绝（`gap_overlap`）。

### 定稿更正规则（追加式更正层）

- **仅允许更正已定稿字幕**：缺口序号（`gap_seq`）或未定稿序号（`not_finalized`）一律拒绝且**不产生任何写入**；
- 更正是**只追加**的：每个序号的更正版本从 1 起连续递增，原定稿记录与历史更正**永不改写、永不删除**；
- 提交时必须携带 `expected_version`，且**必须等于该序号当前更正版本**（尚无更正时为 0）；成功时在同一事务内原子追加下一版本；
- 并发竞争下**只有一个请求**能追加成功，其余返回 `version_conflict` 并携带 `current_version`，客户端可据此重试；
- 每条更正必须填写非空 `reason`，与文本、版本、时间戳一起构成可审计的更正链。

## 快速启动（单条命令）

```bash
docker compose up --build
```

服务监听 **8000** 端口，启动时自动初始化数据库（建表幂等）。

- 交互式 API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 配置

| 环境变量   | 默认值                 | 说明               |
| ---------- | ---------------------- | ------------------ |
| `DB_PATH`  | `/data/subtitles.db`   | SQLite 数据库文件路径 |

Compose 默认将 `/data` 挂载到命名卷 `subtitles-data`，容器重启数据不丢失。端口映射可在 `docker-compose.yml` 的 `ports` 中调整。

## API

### 提交字幕片段

```bash
curl -X POST http://localhost:8000/channels/news/fragments \
  -H 'Content-Type: application/json' \
  -d '{"seq": 1, "revision": 1, "text": "各位观众晚上好"}'
```

响应（`applied` 表示本次提交是否生效，`watermark` 为处理后的最新水位线，`finalized` 为本次新定稿的片段）：

```json
{
  "channel": "news",
  "seq": 1,
  "applied": true,
  "reason": null,
  "watermark": 1,
  "finalized": [{"seq": 1, "revision": 1, "text": "各位观众晚上好"}]
}
```

`applied=false` 时 `reason` 取值：

- `stale_revision` — 修订号不高于已缓冲的同序号片段；
- `already_finalized` — 该序号已定稿（或已裁决为永久缺口），不可再修改。

### 裁决永久缺口

当确认某些序号永久丢失（例如推流中断），操作员可对缺失区间发起裁决：

```bash
curl -X POST http://localhost:8000/channels/news/gaps \
  -H 'Content-Type: application/json' \
  -d '{"start_seq": 3, "end_seq": 4}'
```

裁决成功的响应（`watermark` 为裁决后的新水位线，`skipped` 为本次跳过区间，`finalized` 为随裁决一并释放的连续缓冲片段）：

```json
{
  "channel": "news",
  "applied": true,
  "idempotent": false,
  "reason": null,
  "watermark": 6,
  "skipped": {"start_seq": 3, "end_seq": 4},
  "finalized": [
    {"kind": "subtitle", "seq": 5, "revision": 1, "text": "第五条字幕", "start_seq": null, "end_seq": null},
    {"kind": "subtitle", "seq": 6, "revision": 1, "text": "第六条字幕", "start_seq": null, "end_seq": null}
  ]
}
```

重复提交**完全相同**的已裁决区间时，幂等返回原裁决结果，`idempotent` 为 `true`。`applied=false` 时 `reason` 取值：

- `start_not_at_watermark` — 区间起点不是当前水位线下一位（不能跳过未裁决的序号）；
- `buffered_fragments` — 区间内存在已缓冲片段，应等待其定稿；
- `already_finalized` — 区间已完全定稿且不匹配任何缺口；
- `gap_overlap` — 与既有缺口裁决部分重叠（完全相同的区间才允许幂等提交）。

请求体校验（如 `start_seq < 1`、`end_seq < start_seq`）返回 HTTP 422。

### 查询频道定稿

定稿查询按序号升序**混合返回字幕与缺口标记**，`kind` 字段供下游区分丢失内容：

```bash
curl 'http://localhost:8000/channels/news/finalized?after=0&limit=1000'
```

```json
{
  "channel": "news",
  "watermark": 6,
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕", "start_seq": null, "end_seq": null, "correction_version": null},
    {"kind": "subtitle", "seq": 2, "revision": 1, "text": "第二条字幕", "start_seq": null, "end_seq": null, "correction_version": null},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4, "correction_version": null},
    {"kind": "gap",      "seq": 4, "revision": null, "text": null, "start_seq": 3, "end_seq": 4, "correction_version": null},
    {"kind": "subtitle", "seq": 5, "revision": 1, "text": "第五条字幕", "start_seq": null, "end_seq": null, "correction_version": null},
    {"kind": "subtitle", "seq": 6, "revision": 1, "text": "第六条字幕", "start_seq": null, "end_seq": null, "correction_version": null}
  ]
}
```

- `kind=subtitle`：定稿字幕，取 `revision` / `text`；
- `kind=gap`：永久缺口标记，`start_seq` / `end_seq` 标明所属裁决区间（同一区间内每个被跳过序号各占一条标记）；
- `after` 用于增量拉取，对字幕与缺口标记统一按序号计数；
- `view=original`（默认）：返回定稿原文，`correction_version` 恒为 `null`，行为与旧版完全兼容；
- `view=effective`：合并最新更正后的文本，字幕条目附带 `correction_version`（无更正为 `0`，缺口条目仍为 `null`）。

```bash
curl 'http://localhost:8000/channels/news/finalized?view=effective'
```

### 提交定稿更正

对**已定稿**字幕追加一条不可变更正（原定稿永不改写）：

```bash
curl -X POST http://localhost:8000/channels/news/corrections \
  -H 'Content-Type: application/json' \
  -d '{"seq": 5, "expected_version": 0, "text": "第五条字幕（修正）", "reason": "错别字更正"}'
```

成功响应（`correction_version` 为本次追加的版本号）：

```json
{
  "channel": "news",
  "seq": 5,
  "applied": true,
  "reason": null,
  "correction_version": 1,
  "current_version": 1
}
```

再次更正同一序号时 `expected_version` 必须递增（上例中下一条应为 `1`）。`applied=false` 时 `reason` 取值：

- `not_finalized` — 序号尚未定稿（未提交、仍在缓冲区或超出水位线）；
- `gap_seq` — 序号已裁决为永久缺口，无字幕可更正；
- `version_conflict` — `expected_version` 与当前更正版本不一致，`current_version` 返回当前版本供重试。

所有拒绝均不产生任何写入。请求体校验（如 `seq < 1`、`expected_version < 0`、`reason` 为空）返回 HTTP 422。

### 查询更正审计链

按版本升序返回指定序号的全部更正记录：

```bash
curl http://localhost:8000/channels/news/corrections/5
```

```json
{
  "channel": "news",
  "seq": 5,
  "finalized": true,
  "correction_version": 2,
  "corrections": [
    {"version": 1, "text": "第五条字幕（修正）", "reason": "错别字更正", "created_at": "2026-09-20T10:00:00.000Z"},
    {"version": 2, "text": "第五条字幕（终稿）", "reason": "润色", "created_at": "2026-09-20T10:05:00.000Z"}
  ]
}
```

`finalized=false` 表示该序号未定稿（或为缺口），此时 `corrections` 恒为空。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest                     # 含乱序、修订竞争、缺口裁决、幂等/重叠、更正追加/冲突、并发事务等用例
uvicorn app.main:app --reload --port 8000
```

## 实现要点

- **事务化提交 / 裁决 / 更正**：`app/service.py` 中「校验 → 写入缓冲区 / 记录缺口 / 追加更正 → 推进水位线 → 生成定稿」在单个 `BEGIN IMMEDIATE` 事务内完成；所有校验先于任何写入执行，失败即只读返回，不产生副作用。`finalized`、`gaps` 表主键 `(channel, seq)` 与 `corrections` 表主键 `(channel, seq, version)` 作为防重复定稿 / 裁决 / 追加的最后防线。
- **连续前缀推进**：`_consume_contiguous` 从给定水位线的下一位起逐条消费缓冲区，遇到第一个缺失即停止；片段补齐与缺口裁决都复用该逻辑。
- **缺口存储**：`gap_decisions` 记录每次裁决区间及其当时释放到的序号（用于幂等返回原结果），`gaps` 按被跳过序号逐行落表，定稿查询用 `UNION ALL` 按序合并字幕与缺口标记。
- **追加式更正**：`corrections` 表只 INSERT、不 UPDATE/DELETE，版本对每个序号从 1 起连续递增；`expected_version` 乐观并发控制在事务内先比对当前最大版本再追加，竞争请求天然只有一个成功。`view=effective` 通过 `LEFT JOIN` 每个序号的最新更正版本合并文本，原定稿查询路径（`view=original`）不受影响。
- **存储**：`channels`（水位线）、`fragments`（待定稿缓冲）、`finalized`（定稿，不可变）、`gap_decisions` / `gaps`（永久缺口）、`corrections`（定稿更正，只追加）六张表；WAL 模式 + `busy_timeout`。建表语句幂等，已部署实例重启后自动补建新表。
