# license-obligation-api

软件制品许可证义务核算 API。接收组件依赖图、SPDX 风格的 `AND` / `OR` / `WITH`
许可证表达式、链接方式、分发范围与可配置义务规则，沿依赖关系传播署名
（attribution）、源码提供（source-offer）、同许可证发布（same-license）等义务，
为 `OR` 分支确定性地选择**满足约束且义务最少**的方案；无法放行时返回最小冲突
组件集与已确认子图，**绝不给出放行结论**。

仅使用 Python 3 标准库（`http.server.ThreadingHTTPServer` + `sqlite3`），
无第三方依赖。

## 快速开始

端口：**8080**（环境变量 `PORT` 可改）。

```bash
# 方式一：Docker Compose（单条命令）
docker compose up --build

# 方式二：Docker
docker build -t license-obligation-api .
docker run -p 8080:8080 -v license-data:/data license-obligation-api

# 方式三：本地 Python 3.9+
python3 main.py
```

数据持久化在 SQLite（容器内 `/data/app.db`，本地默认 `./data/app.db`，
环境变量 `APP_DB` 可改）。

自检（启动临时实例跑 294 项端到端检查）：

```bash
python3 tests/selftest.py
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/v1/rulesets` | 列出规则集 |
| PUT | `/v1/rulesets/{name}` | 创建/更新义务规则集（旧语义，向后兼容） |
| GET | `/v1/rulesets/{name}` | 读取当前活动规则集 |
| POST | `/v1/rulesets/{name}/revisions` | 按内容哈希去重生成不可变草稿修订 |
| GET | `/v1/rulesets/{name}/revisions` | 列出修订历史与激活记录 |
| POST | `/v1/rulesets/{name}/revisions/{revision}/activate` | CAS 原子激活（回滚同一接口） |
| POST | `/v1/assessments` | 提交核算（幂等） |
| GET | `/v1/assessments/{id}` | 读取核算结果 |
| POST | `/v1/assessments/{id}/confirm` | 生成确认版（幂等） |
| GET | `/v1/assessments/{id}/confirmation` | 读取确认版 |
| POST | `/v1/assessments/{id}/fulfillment-check` | 履约材料/义务证据核算 |
| POST | `/v1/assessments/{id}/obligation-schedule` | 义务截止日排期 |
| POST | `/v1/assessments/{id}/responsibility-plan` | 义务责任分工（执行/复核） |
| POST | `/v1/assessments/compare` | 两次核算的原子差异与最小归因对比 |

### 不可变修订与原子发布（含并发发布与回滚）

规则集的每次内容都是一条**不可变修订**：修订 id 是规范化内容的 SHA-256
前 16 位（完整哈希在 `content_hash` 给出）。内容相同（规范化后）的多次提交
折叠到同一条修订，历史内容**绝不覆盖、绝不删除**。发布 = 在一个事务里把
“活动修订”指针从旧修订切到目标修订，配合两处乐观并发控制：

- `POST /v1/rulesets/{name}/revisions` 只保存一条**不可变草稿**，必须携带
  `base_revision`（客户端所依据的活动修订；**规则集首次创建时传 `null`**）。
  服务端在事务开始时读取活动修订，若已不等于 `base_revision`（基线过期）返回
  **409**，响应体给出 `reason: "stale-base"` 与当前 `active_revision`。
  草稿事务**只**追加修订内容，不写活动指针、不写激活审计、不更新旧 `rulesets`
  镜像，因此首版在显式发布前不参与任何核算。
- `POST .../revisions/{revision}/activate` 是唯一的发布入口：激活草稿（或任意
  旧修订），必须携带 `expected_active_revision`，**规则集首次发布时传
  `null`**。服务端在**同一事务**内重读活动修订并完成指针写入/切换、审计记录
  与旧规则镜像同步；活动修订已被他人改变（或首版已被他人抢先发布）时返回
  **409**（`reason: "active-moved"`，附当前活动修订），整笔发布不发生。

**生成草稿**（内容先经与 PUT 相同的规范化校验；草稿不会自动激活）：

```bash
curl -sS -X POST "$BASE/v1/rulesets/acme/revisions" \
  -H 'Content-Type: application/json' \
  -d '{"rules": {"licenses": {"Acme-1.0": {"obligations": ["attribution"]}}},
       "base_revision": null}'
```

```json
{
  "name": "acme",
  "revision": "a1b2c3d4e5f60718",
  "content_hash": "a1b2c3d4…（完整 64 位 SHA-256）",
  "base_revision": null,
  "created_at": "2026-09-28T10:00:00+00:00",
  "active": false,
  "created": true,
  "ruleset": { …规范化后的完整规则… }
}
```

- 首次创建（`base_revision: null`）**只保存首版草稿**，响应
  `"active": false`；再次提交相同（规范化）内容返回 `"created": false`
  折叠到同一修订（仍未发布）；已有规则集提交新内容同样返回
  `"active": false` 的草稿。
- **草稿阶段**该名称对旧接口与核算不可见：旧 `GET /v1/rulesets/{name}` 返回
  **404**、`GET /v1/rulesets` 列表不含该名称、命名规则集核算
  （`POST /v1/assessments`）返回 **404**；只有
  `GET .../revisions` 可见该草稿，其 `active_revision` 为 `null`、
  `activations` 为空数组。
- 首版经激活接口发布后，旧 GET 才返回该规则集并附带 `active_revision`
  字段；旧 PUT 仍可用（等价于“以当前活动修订为基线、无条件追加并激活”，
  对仅有草稿的名称则直接发布首版，相同内容为空操作）。

**首次发布（CAS，期望值为 null）**：

```bash
curl -sS -X POST "$BASE/v1/rulesets/acme/revisions/a1b2c3d4e5f60718/activate" \
  -H 'Content-Type: application/json' \
  -d '{"expected_active_revision": null}'
# 200 {"revision": "a1b2c3d4e5f60718", "active_revision": "a1b2c3d4e5f60718",
#      "activated": true, "previous_revision": null,
#      "activated_at": "2026-09-28T10:05:00+00:00"}
```

同一首版被两个发布者并发发布时，同样只有一人成功，另一人得到 **409**
（`active_revision` 已不再为 `null`），事务保证只产生一条首版激活审计。
目标修订不存在时：名称下从无草稿返回 404 规则集不存在，名称存在但修订
未知返回 404 修订不存在。

**并发发布（CAS）**：两个发布者各自先读活动修订，只有一人的
`expected_active_revision` 仍匹配：

```bash
# 双方都基于 r1 准备了各自的草稿 r2 / r3
curl -sS -X POST "$BASE/v1/rulesets/acme/revisions/r2aaaaaaaaaaaaaa/activate" \
  -H 'Content-Type: application/json' \
  -d '{"expected_active_revision": "r1bbbbbbbbbbbbbb"}'
# 200（r2 激活成功）

curl -sS -X POST "$BASE/v1/rulesets/acme/revisions/r3cccccccccccccc/activate" \
  -H 'Content-Type: application/json' \
  -d '{"expected_active_revision": "r1bbbbbbbbbbbbbb"}'
# 409
# {"error": "expected active revision 'r1bbbbbbbbbbbbbb' but it is 'r2aaaaaaaaaaaaaa'",
#  "reason": "active-moved",
#  "expected_active_revision": "r1bbbbbbbbbbbbbb",
#  "active_revision": "r2aaaaaaaaaaaaaa"}

# 落败方重新 GET 历史、合并/确认后，以新的活动修订为期望重试即可
```

激活成功返回 `previous_revision`（事务开始时读到的活动修订，即被替换者；
首次发布为 `null`）与 `activated_at`；重复激活当前已活动的修订是幂等空操作
（`activated: false`，不新增审计记录）。目标修订不存在时：名称下从无修订
返回 404，名称存在但修订未知返回 404；缺少 `expected_active_revision`
字段或取值既非字符串也非 `null` 返回 400。

**修订历史**：`GET /v1/rulesets/{name}/revisions` 返回
`active_revision`（仅草稿、尚未发布时为 `null`）、按创建顺序排列的全部
`revisions`（每条含完整 `ruleset` 内容、`content_hash`、`base_revision`、
`active` 标记、`created_at`）以及 `activations` 激活审计链
（`revision` / `previous_revision` / `activated_at`；首次发布前为空数组）。
名称下从无任何修订（连草稿也没有）时返回 404。

**回滚**：旧修订仍是一条内容完整、哈希不变的不可变修订，**用同一个
CAS 激活接口即可回滚**（无需也无法重建内容），回滚只新增一条激活审计，
不新建内容行、不删除任何历史：

```bash
# 当前活动 r3，回滚到更早的 r1
curl -sS -X POST "$BASE/v1/rulesets/acme/revisions/r1bbbbbbbbbbbbbb/activate" \
  -H 'Content-Type: application/json' \
  -d '{"expected_active_revision": "r3dddddddddddddd"}'
# 200 {"revision": "r1bbbbbbbbbbbbbb", "active_revision": "r1bbbbbbbbbbbbbb",
#      "activated": true, "previous_revision": "r3dddddddddddddd", …}
```

**核算与活动修订**：命名规则集的核算（`POST /v1/assessments`）在单个
事务内读取**事务开始时的活动修订**、按其规则快照求值，并把该修订 id
记入评估记录（响应 `ruleset_revision`，`GET /v1/assessments/{id}` 的
`revision`）。因此与激活并发的核算只会绑定二者之一，绝不出现“按新规则
求值却记录旧修订”的错配；既有评估的规则快照（`rules` 列）继续保留，
评估结果与确认/复演不因后续激活或回滚而漂移——内联规则（`"rules": {...}`）
的评估依旧不依赖任何规则集。

**旧库迁移**：升级后首次打开旧版 SQLite 时，每个现有 `rulesets` 行
（即旧 PUT 写入的内容）原样成为该规则集的**首个活动修订**（修订 id 为
其内容哈希，`base_revision` 为 `null`），可核验的旧评估记录补盖修订
id；旧 PUT/GET、既有评估快照与确认版语义全部保持不变。

### 提交核算 `POST /v1/assessments`

```json
{
  "product": "edge-gateway",
  "distribution": "binary",
  "ruleset": "default",
  "components": [
    {"id": "gateway", "name": "Gateway", "version": "4.2.0", "license": "Apache-2.0"},
    {"id": "crypto", "name": "CryptoLib", "version": "1.9",
     "license": "(LGPL-2.1-only OR Apache-2.0) AND MIT", "linkage": "dynamic"},
    {"id": "sqlite", "name": "SQLite", "version": "3.45",
     "license": "Unlicense", "linkage": "static"}
  ],
  "dependencies": [
    {"from": "gateway", "to": "crypto"},
    {"from": "gateway", "to": "sqlite"}
  ]
}
```

- `distribution`：`internal` | `binary` | `source` | `saas`（默认 `binary`），
  决定义务是否触发（如 `source-offer` 默认只在 `binary`/`source` 下生效，
  `network-source` 只在 `saas` 下生效，可由 `obligation_scopes` 配置）。
- `linkage`：`static` | `dynamic` | `process` | `none`（默认 `static`），
  描述该组件以何种方式被其依赖方纳入。
- `dependencies[].from` 依赖 `to`。
- `ruleset` 指定已存规则集（默认 `default`，启动时自动播种常见许可证）；
  也可在请求体内直接给 `"rules": {...}` 使用内联规则。

响应（`status: "ok"` 时）：

```json
{
  "id": "45c9821d…",
  "created": true,
  "result": {
    "status": "ok",
    "release": true,
    "selection_mode": "exhaustive",
    "artifact_obligations": ["attribution", "license-text", "notice-preserve"],
    "adopted": {"crypto": "Apache-2.0 AND MIT", "gateway": "Apache-2.0", "sqlite": "Unlicense"},
    "components": [
      {"id": "gateway", "licenses": ["Apache-2.0"],
       "obligations": ["attribution", "license-text", "notice-preserve"],
       "effective_licenses": ["Apache-2.0", "MIT", "Unlicense"],
       "propagated_from": {}}
    ],
    "summary_hash": "5b4d6f10…"
  }
}
```

### 结果状态

| `status` | 含义 | `release` |
| --- | --- | --- |
| `ok` | 存在无冲突方案，给出义务摘要与采用分支 | `true` |
| `conflict` | 所有 OR 分支组合均冲突 | `false` |
| `invalid-expression` | 表达式无法解析（含组件、位置、原因） | `false` |
| `missing-rules` | 引用了规则集中不存在的许可证/例外 | `false` |
| `dependency-cycle` | 依赖图成环（Tarjan SCC，含自环） | `false` |

非 `ok` 状态一律返回：

- `conflicting_components`：**最小冲突组件集**——取所有"最少冲突"分支组合，
  将其冲突涉及的组件求并；每条冲突本身只含参与组件（不兼容为成对组件 +
  冲突发生地）。该集合完整解释"为何任何分支都不可行"，且集合中任一组件
  被排除后实例即可满足；
- `confirmed_subgraph`：**已确认子图**——与冲突无关、且分支选择在所有最少
  冲突组合中一致的组件及其已解析信息；
- `release: false`，确认接口对这些状态返回 `409`，不产生任何放行结论。

### 确认版 `POST /v1/assessments/{id}/confirm`

仅 `status: "ok"` 可确认，幂等，输出：

- `manifest.artifacts`：制品清单（id、名称、版本、采用许可证、义务）；
- `adopted_licenses`：每个组件采用的许可证分支；
- `notice`：由带 `attribution`/`license-text` 义务的组件生成的 NOTICE 文本；
- `replay`：**可直接提交的固定复演请求**；
- `confirmation_hash`：确认版内容哈希。

`replay` 结构：

```json
{
  "endpoint": "POST /v1/assessments",
  "ruleset": "default",
  "rules_hash": "…",
  "request": { "product": "…", "components": [ … ], "rules": { … } },
  "expected_summary_hash": "…"
}
```

- `request` 本身就是完整的 `POST /v1/assessments` 请求体：规范化后的原始
  请求 + 内联的**规则快照**（`rules`），无需任何加工即可原样重放。
- 复演读取的是快照而非当前同名规则集——即使该规则集事后被同名覆盖（或原
  请求本来就使用内联规则），重放仍落到同一评估 id、义务与 `summary_hash`。
- `ruleset` 仅为出处记录（内联规则的评估为 `null`），`rules_hash` 是所绑定
  规则快照的 SHA-256，便于核对。重放返回同一 id（`created: false`）且
  `summary_hash == expected_summary_hash` 即复演成功。

**旧数据兼容迁移**：升级后首次打开旧版 SQLite 会自动为 `assessments` 增加
规则快照列，并对每条旧记录做**核验式修复**——仅当“同名规则集仍在、其当前
规范化内容哈希到该评估 id、且以其重算得到同一 `summary_hash`”三者同时成立
时才回填快照。规则集已删除/被覆盖、或旧记录源于从未持久化的内联规则时，
无法证明原规则为何，`confirm` 返回 `409` 明确拒绝（绝不生成可能失真的伪复演
内容）；若之后恢复了与原内容一致的规则集，下次确认会重新核验并修复。已存在
的确认版不可变，保持原样。

### 履约核算 `POST /v1/assessments/{id}/fulfillment-check`

针对一个已存在的**放行核算**（`status: "ok"`）提交履约材料与义务证据规则，
由服务把每个组件在该分发范围下的义务展开为待满足实例，并**全局**选择一组
材料使结论唯一确定。非 `ok` 的核算返回 `409`，不产生任何履约结论；核算
不存在返回 `404`。

请求体：

```json
{
  "materials": [
    {"id": "notice-bundle", "digest": "sha256:9f2c…",
     "type": "notice", "scope": "product"},
    {"id": "gpl-source-a", "digest": "sha256:1a7b…",
     "type": "source-archive", "scope": "component", "component": "libg"},
    {"id": "gpl-source-b", "digest": "sha256:1a7b…",
     "type": "source-archive", "scope": "component", "component": "libg"}
  ],
  "rules": {
    "obligations": {
      "attribution": {"type": "notice"},
      "license-text": {"all": [
        {"type": "notice"},
        {"any": [{"scope": "product"}, {"scope": "component"}]}
      ]},
      "source-offer": {"all": [
        {"type": "source-archive"}, {"scope": "component"}
      ]},
      "same-license": {"any": [
        {"type": "source-archive"}, {"type": "copyleft-notice"}
      ]}
    }
  }
}
```

- `materials[]`：履约材料。`id` 在请求内唯一；`digest` 是材料摘要（调用方
  自管哈希算法，按字符串比较）；`type` 是材料类型；`scope` 为
  `product`（产品级，可覆盖所有适用组件）或 `component`（组件级，必须带
  `component` 且**仅覆盖其声明的组件**）。产品级材料不得带 `component`。
- **同一摘要只算一份材料**：相同 `digest` 且元数据一致的多条材料合并为
  一份（以字典序最小的 `id` 为准，返回中给出 `deduplicated` 明细）；摘要
  相同但 `type`/`scope`/`component` 冲突时返回 **422**。
- `rules.obligations`：按义务 id 给出的证据规则。规则节点是可嵌套的
  `all`/`any` 表达式（`all` 与 `any` 互斥、数组非空），叶节点约束单个材料
  的字段：`type`（材料类型）、`scope`（`product`/`component`），以及可选的
  `component`（把叶节点进一步钉在指定组件上）。**叶子命题按已选材料集合
  累积求值**：一个叶节点成立，当且仅当已选材料中**至少有一份**匹配它；
  因此同一 `all` 的各个合取叶子（任意嵌套深度）可以由**不同材料**分别
  满足——例如一份署名材料命中 `type=notice`、另一份许可证文本命中
  `type=license-copy`，二者共同满足
  `{"all": [{"type": "notice"}, {"type": "license-copy"}]}`。接口绝不再
  要求存在某一份材料独自命中整棵规则。

**选择规则（全局最优，禁止逐项贪心/固定截断）**：在所有材料子集上按以下
字典序目标取最小，方案因此唯一：

1. 未满足实例数最少（先追求**满足全部实例**）；
2. 使用材料数最少；
3. 选中材料 id 元组的字典序最小。

无法全部满足时，仍按同一目标返回**最佳部分方案**：最大化已满足实例数，
再最少材料、最后 id 字典序；响应 `ready=false`，并给出：

- `gaps`：每个未满足实例的具体缺口——组件、义务、原因与说明：
  - `no-evidence-rule`：未提交该义务的证据规则；
  - `no-matching-material`：提交的所有材料都不匹配规则中的任何叶子
    （规则在该实例上完全无命中）；
  - `incomplete-evidence`：有材料命中部分叶子，但已选材料集合仍不能使
    整棵规则成立（例如规则要求署名材料与许可证文本两者，却只提交了
    前者）；条目额外给出 `missing_leaves`——仍无任何材料匹配的叶子
    约束（按规范化形式排序），以及 `candidate_materials`——命中过规则
    任意叶子的材料，便于精确定位缺哪一类证据；
- `hits`：命中轨迹——每份入选材料分别为哪些实例的哪些合取叶子提供了
  证据（`satisfied_by` 是各实例轨迹的并）；
- `satisfied`：已满足实例及由哪些入选材料共同满足；
- `unused_materials`：未入选材料。

**多材料共同满足 `all` 的示例**：一份产品级署名通知与一份钉在组件 `app`
上的许可证文本，分别命中同一义务规则的两个合取叶子，即共同满足该实例；
两者都会出现在 `selected_materials`、各自的 `hits` 轨迹以及该实例的
`satisfied_by` 中：

```json
{
  "materials": [
    {"id": "attribution-notice", "digest": "sha256:9f…",
     "type": "notice", "scope": "product"},
    {"id": "app-license-text", "digest": "sha256:1a…",
     "type": "license-copy", "scope": "component", "component": "app"}
  ],
  "rules": {"obligations": {
    "license-text": {"all": [
      {"type": "notice"},
      {"any": [{"type": "license-copy"}, {"type": "license-bundle"}]}
    ]}
  }}
}
```

```json
{
  "ready": true,
  "instances_satisfied": 1,
  "selected_materials": ["app-license-text", "attribution-notice"],
  "satisfied": [{"component": "app", "obligation": "license-text",
                 "satisfied_by": ["app-license-text", "attribution-notice"]}],
  "hits": [
    {"material_id": "app-license-text", "instances": [
      {"component": "app", "obligation": "license-text"}]},
    {"material_id": "attribution-notice", "instances": [
      {"component": "app", "obligation": "license-text"}]}
  ],
  "gaps": []
}
```

材料数不超过 16 时穷举全部子集；更多时使用完备分支定界（响应中
`selection_mode` 标明 `exhaustive` / `branch-and-bound`，两者结果完全一致）。
**材料与规则的输入顺序（含 `all`/`any` 数组顺序）不改变结果**，
`fulfillment_hash` 是规范化响应的 SHA-256，可用于核对。

上例响应（节选；两份相同摘要的源码包合并为一份）：

```json
{
  "ready": true,
  "selection_mode": "exhaustive",
  "instances_total": 3,
  "instances_satisfied": 3,
  "selected_materials": ["gpl-source-a", "notice-bundle"],
  "hits": [
    {"material_id": "notice-bundle", "instances": [
      {"component": "app", "obligation": "attribution"},
      {"component": "app", "obligation": "license-text"},
      {"component": "libg", "obligation": "attribution"}
    ]}
  ],
  "gaps": [],
  "unused_materials": [],
  "deduplicated": [
    {"digest": "sha256:1a7b…", "canonical_id": "gpl-source-a",
     "duplicate_ids": ["gpl-source-a", "gpl-source-b"]}
  ],
  "fulfillment_hash": "…"
}
```

```bash
curl -sS -X POST \
  "$BASE/v1/assessments/$ID/fulfillment-check" \
  -H 'Content-Type: application/json' -d @fulfillment.json
```

### 义务排期 `POST /v1/assessments/{id}/obligation-schedule`

针对一个已存在的**放行核算**（`status: "ok"`）计算每项制品义务的截止日。
非 `ok` 的核算返回 `409`，核算不存在返回 `404`；缺少义务规则、非法时区、
非法日期或依赖成环时返回 `422`，**绝不生成部分计划**。

请求体：

```json
{
  "release_at": "2026-09-25T16:00:00+08:00",
  "timezone": "Asia/Shanghai",
  "workdays": [1, 2, 3, 4, 5],
  "holidays": ["2026-10-01", "2026-10-02"],
  "obligations": {
    "attribution":  {"offset": 5,  "unit": "business_day", "depends_on": []},
    "license-text": {"offset": 20, "unit": "calendar_day", "depends_on": ["attribution"]},
    "source-offer": {"offset": 10, "unit": "calendar_day", "depends_on": ["license-text"]}
  }
}
```

- `release_at`：RFC 3339 时间戳（必须带 `Z` 或 `±HH:MM` 偏移）。先换算到
  `timezone` 指定的 IANA 时区，取其**当地日期**作为第 0 天。
- `timezone`：IANA 时区名（如 `Asia/Shanghai`、`UTC`）；未知名称返回 422。
- `workdays`：每周工作日，ISO  weekday 编号（`1`=周一 … `7`=周日），去重后
  生效，顺序无关。
- `holidays`：`YYYY-MM-DD` 节假日列表（可省略），去重后生效，顺序无关。
- `obligations`：按义务 id 给出的排期规则。**核算结果中的每个制品义务
  （`artifact_obligations`）都必须有规则**，否则 422；额外的里程碑义务
  （非制品义务）也允许出现并参与排期（其 `components` 为空），便于
  `depends_on` 引用中间节点。
  - `offset`：非负整数，自发布当地日期起算的偏移量（`0` 即发布当日）；
  - `unit`：`calendar_day`（日历日）或 `business_day`（工作日——只数
    属于 `workdays` 且非节假日的日子，发布当日不计入）；
  - `depends_on`：该义务依赖的前置义务 id 列表（可省略）。前置义务的
    截止日**不得晚于**后继义务；引用不存在的义务或依赖成环（含自环）
    返回 422。

**计算规则**：先按 `offset`/`unit` 算出每项义务的**原始截止日**，再沿无环
依赖图**反向收紧**——`final(o) = min(raw(o), 所有依赖 o 的后继的 final)`，
保证前置截止日不晚于后继。响应逐项给出：

```json
{
  "assessment_id": "…",
  "release_at": "2026-09-25T08:00:00Z",
  "timezone": "Asia/Shanghai",
  "release_date": "2026-09-25",
  "workdays": [1, 2, 3, 4, 5],
  "holidays": ["2026-10-01", "2026-10-02"],
  "schedule": [
    {"obligation": "attribution", "components": ["app", "liba"],
     "offset": 5, "unit": "business_day", "depends_on": [],
     "raw_deadline": "2026-10-06", "deadline": "2026-10-05",
     "tightened": true,
     "tightening_chain": ["attribution", "license-text", "source-offer"]},
    {"obligation": "license-text", "components": ["app", "liba"],
     "offset": 20, "unit": "calendar_day", "depends_on": ["attribution"],
     "raw_deadline": "2026-10-15", "deadline": "2026-10-05",
     "tightened": true, "tightening_chain": ["license-text", "source-offer"]},
    {"obligation": "source-offer", "components": ["liba"],
     "offset": 10, "unit": "calendar_day", "depends_on": ["license-text"],
     "raw_deadline": "2026-10-05", "deadline": "2026-10-05",
     "tightened": false, "tightening_chain": []}
  ],
  "schedule_hash": "…"
}
```

- `raw_deadline` / `deadline`：原始 / 收紧后的最终截止日（`YYYY-MM-DD`）。
- `components`：该义务在核算结果中覆盖的组件（排序）。
- `tightening_chain`：造成收紧的**完整依赖链**——从该项出发、沿后继义务
  直到最终约束来源（其原始截止日生效的那项）；未被收紧时为空数组。
  并列同样紧的后继按义务 id 字典序取规范链，结果与输入顺序无关。
- `release_at` 在响应中规范化为 UTC（`Z` 后缀）；同一时刻的不同写法
  得到同一计划。
- `schedule_hash` 为规范化响应（不含哈希字段本身）的 SHA-256。
  **输入对象键序、`workdays`、`holidays` 及 `depends_on` 的顺序都不改变
  `schedule_hash`**。

IANA 时区数据来自操作系统的 tz database（Docker 镜像已内置 `tzdata`；
本地运行需系统提供 `/usr/share/zoneinfo` 或等效数据）。

```bash
curl -sS -X POST \
  "$BASE/v1/assessments/$ID/obligation-schedule" \
  -H 'Content-Type: application/json' -d @schedule.json
```

### 责任分工 `POST /v1/assessments/{id}/responsibility-plan`

针对一个已存在的**放行核算**（`status: "ok"`）把每项义务落实为责任项并分配
人员。非 `ok` 的核算返回 `409`，核算不存在返回 `404`；请求体不合法返回
`400`。责任项生成规则：

- 每个**制品级义务**（`artifact_obligations`）生成一项，id 为
  `artifact:<obligation>`；
- 每个**组件级义务**按组件各生成一项，id 为
  `component:<component>:<obligation>`。

请求体提交人员及其执行/复核资格、总容量与相应成本。以下示例假设核算只含
一个组件 `app`，其许可证带 `attribution` 义务，因此责任项为
`artifact:attribution` 与 `component:app:attribution` 两项：

```json
{
  "people": [
    {"id": "alice",
     "qualifications": {"execute": ["attribution"], "review": ["attribution"]},
     "capacity": 2,
     "costs": {"execute": 4, "review": 2}},
    {"id": "bob",
     "qualifications": {"execute": ["attribution"], "review": ["attribution"]},
     "capacity": 2,
     "costs": {"execute": 3, "review": 3}}
  ]
}
```

- `people[]`：人员数组（允许为空数组，表示无人可用）。`id` 在请求内唯一；
  `capacity` 为非负整数总容量（必填）；`qualifications.execute` /
  `qualifications.review` 为该人员可执行 / 可复核的义务 id 列表（省略视为
  无资格）；`costs.execute` / `costs.review` 为担任相应角色一次的成本
  （非负数值，省略为 0）。
- 每项责任项必须分配**不同的两个人**担任执行人与复核人；执行人须持有该
  义务的执行资格，复核人须持有复核资格。每种角色各消耗该人员**一单位**
  总容量（一人可混合担任多个执行/复核角色，总数不超过其 `capacity`）。

**分配规则（全局最优，禁止先分执行人再贪心补复核人或固定截断）**：在所有
可行分工上按以下字典序目标取最小，方案因此唯一：

1. 已同时分配两种角色的责任项数最大（先追求**覆盖全部责任项**）；
2. 总成本最小（每项的执行人执行成本 + 复核人复核成本之和）；
3. 按责任项 ID、执行人 ID、复核人 ID 序列字典序最小破平局。

责任项组合空间不超过 65536 时穷举全部方案；更大时启用带最小费用流界
的完备分支定界（响应中 `selection_mode` 标明 `exhaustive` /
`branch-and-bound`，两者结果完全一致）。成本按精确有理数计算，浮点输入
不会干扰优化与破平局。

响应（上例：bob 的执行成本更低、alice 的复核成本更低，容量恰好覆盖）：

```json
{
  "assessment_id": "…",
  "ready": true,
  "selection_mode": "exhaustive",
  "items_total": 2,
  "items_assigned": 2,
  "assignments": [
    {"item": "artifact:attribution", "kind": "artifact",
     "obligation": "attribution", "executor": "bob", "reviewer": "alice",
     "cost": 5},
    {"item": "component:app:attribution", "kind": "component",
     "component": "app", "obligation": "attribution",
     "executor": "bob", "reviewer": "alice", "cost": 5}
  ],
  "unassigned": [],
  "total_cost": 10,
  "capacity_usage": [
    {"person": "alice", "execute": 0, "review": 2, "used": 2, "capacity": 2},
    {"person": "bob", "execute": 2, "review": 0, "used": 2, "capacity": 2}
  ],
  "plan_hash": "…"
}
```

**覆盖不全时返回 409**，响应仍给出按同一目标求得的**最佳部分方案**
（`ready=false`），并在 `unassigned` 中逐项给出未分配项及受资格或容量
限制的原因：

- `no-qualified-executor`：没有任何提交人员持有该义务的执行资格；
- `no-qualified-reviewer`：没有任何提交人员持有该义务的复核资格；
- `no-distinct-pair`：只有同一人同时具备该义务的两种资格，而执行人与
  复核人必须不同；
- `insufficient-capacity`：有资格的人存在，但在最优方案中其剩余容量
  不足以覆盖该项。

每个未分配项另附 `qualified_executors` / `qualified_reviewers`（具备相应
资格的人员，排序）与 `detail` 说明。例如上例中把 bob 改为仅执行资格、
容量 1：

```json
{
  "ready": false,
  "error": "cannot staff every responsibility item: 1 of 2 items assigned",
  "items_total": 2,
  "items_assigned": 1,
  "assignments": [
    {"item": "artifact:attribution", "kind": "artifact",
     "obligation": "attribution", "executor": "bob", "reviewer": "alice",
     "cost": 5}
  ],
  "unassigned": [
    {"item": "component:app:attribution", "kind": "component",
     "component": "app", "obligation": "attribution",
     "reason": "insufficient-capacity",
     "qualified_executors": ["alice", "bob"],
     "qualified_reviewers": ["alice"],
     "detail": "qualified people exist for obligation 'attribution', but …"}
  ],
  "total_cost": 5
}
```

`capacity_usage` 汇总每个人的执行/复核/合计用量与容量；`plan_hash` 为
规范化响应（不含哈希字段本身）的 SHA-256。**人员数组顺序与资格列表顺序
（含重复项）都不改变结果与 `plan_hash`**。

```bash
curl -sS -X POST \
  "$BASE/v1/assessments/$ID/responsibility-plan" \
  -H 'Content-Type: application/json' -d @people.json
```

### 核算对比 `POST /v1/assessments/compare`

比较两次已持久化的核算（各自的**规范化请求 + 规则快照**），回答"目标版本
相对基线改了什么、其中哪些改动真正导致了结论变化"。请求体给出两个核算 id：

```json
{"baseline_id": "45c9821d…", "target_id": "9be1042a…"}
```

任一核算不存在返回 **404**；核算的规则快照无法核验恢复（旧库记录、规则集
已删除或被覆盖）时返回 **409**，绝不按猜测的规则对比。

**原子差异**：两次持久化状态的全部变化被拆成稳定编号的原子差异
（`D0001`、`D0002`…，编号按差异种类与键的字典序确定，同一对核算永远得到
同一编号）：

| `kind` | 粒度 | `before` / `after` |
| --- | --- | --- |
| `distribution` | 分发范围 | 两侧取值 |
| `product` | 产品名 | 两侧取值 |
| `component` | 单个组件记录 | 两侧记录，不存在的一侧为 `null` |
| `dependency` | 单条依赖边 | 两侧存在性（`true`/`false`） |
| `license-rule` | 单条许可证规则 | 两侧规则，不存在的一侧为 `null` |
| `exception-rule` | 单条例外规则 | 同上 |
| `obligation-scope` | 单条义务范围配置 | 两侧范围列表或 `null` |
| `combined-work-linkage` | 单个组合作品链接方式 | 两侧存在性 |

每条差异另带 `change`（`added` / `removed` / `changed`）。回滚一条差异即把
该方面恢复为基线侧取值；回滚**全部**差异精确复现基线状态，因此恢复集合
必然存在。

**最小归因集（反事实回滚）**：以目标版本为起点，对差异子集做反事实回滚并
重新核算，找出能使**状态、采用分支、制品与组件义务及冲突解释**恢复为基线
的全局最小集合——先比较集合的差异数量，再按差异 ID 序列的字典序取唯一
结果。搜索按集合大小递增、同大小按 ID 字典序**完备枚举**：绝不贪心、绝不
固定截断；回滚后不合法的中间请求（如依赖边指向已被回滚的组件、规则集被
抽空）直接跳过，**绝不算作命中**。结论本就一致时（`conclusions_agree:
true`）归因集为空。

响应（上表 `component` 记录从双分支改为单一 LGPL、同时改了产品名的例子；
产品名改动不影响结论，因此不在归因集中）：

```json
{
  "baseline_id": "45c9821d…",
  "target_id": "9be1042a…",
  "baseline_status": "ok",
  "target_status": "ok",
  "conclusions_agree": false,
  "diff_count": 2,
  "diffs": [
    {"id": "D0001", "kind": "product", "change": "changed",
     "before": "edge-gateway", "after": "edge-gateway-2"},
    {"id": "D0002", "kind": "component", "component": "crypto",
     "change": "changed",
     "before": {"id": "crypto", "license": "(LGPL-2.1-only OR Apache-2.0) AND MIT",
                "linkage": "dynamic", "name": "crypto", "version": ""},
     "after":  {"id": "crypto", "license": "LGPL-2.1-only AND MIT",
                "linkage": "dynamic", "name": "crypto", "version": ""}}
  ],
  "minimal_set": ["D0002"],
  "minimal_size": 1,
  "counterfactual": {
    "status": "ok",
    "release": true,
    "summary_hash": "e1b7ff58…",
    "baseline_summary_hash": "e1b7ff58…",
    "restores_baseline_conclusion": true
  },
  "comparison_hash": "53e92bab…"
}
```

- `diffs`：全部直接差异；`minimal_set`：最小归因集（差异 ID 列表）。
- `counterfactual`：回滚最小集合后的反事实结果摘要——状态、是否放行、
  结果 `summary_hash` 与基线 `summary_hash`；回滚集合恰为全部差异时，
  两个哈希相同。
- `comparison_hash`：规范化响应（不含哈希字段本身）的 SHA-256；同一对
  核算重复对比结果逐字节一致，比较本身不改变任何已存核算。

```bash
curl -sS -X POST "$BASE/v1/assessments/compare" \
  -H 'Content-Type: application/json' \
  -d "{\"baseline_id\": \"$BASE_ID\", \"target_id\": \"$TARGET_ID\"}"
```

## 语义模型

**表达式**：`AND` / `OR` / `WITH`、括号、许可证 id（含 `+` 后缀与
`LicenseRef-*`），操作符大小写不敏感。`OR` 枚举分支，`AND` 合取，
`WITH` 例外按规则集修改义务或取消传播。

**传播**（沿依赖边向上游，依赖先于被依赖方处理）：

- *共存（presence）*：经 `combined_work_linkages`（默认 `static`、`dynamic`）
  连成同一组合作品的许可证集合，用于 `incompatible_with` 不兼容判定；
- *义务传播（spread）*：按各许可证 `propagates` 列表传播的 copyleft 义务，
  用于 `same-license` 冲突（同一组件被要求以两个不同许可证发布即冲突）。

**OR 分支选择**：组合数不超过 4096 时穷举所有分支组合；超过 4096 时启用
完备的分支定界搜索（结果中 `selection_mode` 标明 `exhaustive` /
`branch-and-bound`）——只剪除被证明不可能优于当前最优解的组合，结果与
穷举完全一致。两条路径都按
`(冲突数, 制品级义务数, 组件义务总数, 分支字典序)` 取最小——即先满足约束，
再使义务最少，字典序保证唯一确定解；只有证明全部分支组合均冲突时才返回
`conflict`。

**确定性**：请求在入口处规范化（组件、依赖排序去重），评估顺序、冲突列表、
摘要均按字典序产生，`summary_hash` 为规范化结果（不含哈希字段本身）的
SHA-256。**输入顺序变化不改变摘要**（自检中用乱序请求验证同一 `id` 与
`summary_hash`）。评估 id 为 `(规范化请求, 规范化规则)` 的哈希，重复提交幂等
返回；每次核算都把当时的规则快照随评估持久化，因此结果不随同名规则集的后续
变更而漂移。

## 规则集配置

```json
{
  "licenses": {
    "GPL-2.0-only": {
      "obligations": ["attribution", "license-text", "source-offer", "same-license"],
      "propagates": ["static", "dynamic"],
      "incompatible_with": ["Apache-2.0"]
    }
  },
  "exceptions": {
    "Classpath-exception-2.0": {
      "removes": ["same-license"],
      "adds": [],
      "removes_propagation": true
    }
  },
  "obligation_scopes": {"source-offer": ["binary", "source"]},
  "combined_work_linkages": ["static", "dynamic"]
}
```

义务词汇开放（内置使用 `attribution`、`license-text`、`notice-preserve`、
`no-endorsement`、`source-offer`、`same-license`、`network-source`、
`relink-notice`）。内置 `default` 规则集覆盖 MIT、BSD-2/3-Clause、ISC、
Apache-2.0、GPL-2.0/3.0-only、LGPL-2.1/3.0-only、AGPL-3.0-only、MPL-2.0、
EPL-2.0、CC0-1.0、Unlicense 及 Classpath/LLVM 例外。

## 项目结构

```
main.py               入口：播种默认规则集并启动服务
app/common.py         常量、错误类型、规范化 JSON / 稳定哈希
app/expression.py     SPDX 风格表达式解析（AND/OR/WITH）
app/rules.py          默认规则集 + 规则校验规范化
app/revisions.py      不可变修订标识：规范化内容哈希（修订 id / content_hash）
app/solver.py         义务传播、OR 分支选择（穷举 + 分支定界）、冲突检测、确认版生成
app/fulfillment.py    履约材料核算：义务实例展开、all/any 证据规则（叶子命中按材料集合累积，可由多份材料共同满足）、全局最小集合选择（穷举 + 分支定界）
app/schedule.py       义务截止日排期：RFC 3339/IANA 时区解析、日历日/工作日偏移、沿无环依赖反向收紧与收紧链还原
app/responsibility.py 责任分工：责任项展开（制品级/组件级）、人员资格/容量/成本规范化、全局最优分工（穷举 + 最小费用流界的完备分支定界）、409 最佳部分方案与原因
app/compare.py        核算对比：原子差异分解（稳定编号）、反事实回滚与全局最小归因集（按大小递增、ID 字典序完备枚举，非法中间状态不计命中）
app/store.py          SQLite 持久化（WAL，写锁）、规则快照绑定与旧库核验式迁移、不可变修订/CAS 激活/激活审计的事务实现
app/server.py         ThreadingHTTPServer JSON 路由
tests/selftest.py     294 项端到端自检（含草稿/首版发布门禁、并发发布与回滚）
Dockerfile / docker-compose.yml / .dockerignore
```

## 限制

- 分支定界是完备搜索：任意大的组合空间都给出与穷举一致的最优解，
  但问题本身 NP 难，病态实例的耗时可能随组合空间指数增长；服务绝不
  在超时后退回近似结论，也绝不把本可避免的冲突报告为冲突；
- 规则即事实：兼容性/义务结论完全取决于所配置规则集，服务本身不构成
  法律意见。
