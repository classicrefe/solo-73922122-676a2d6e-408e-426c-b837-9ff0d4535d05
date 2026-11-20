# SQL 改写有界等价核对服务

面向数仓质控场景提供两类能力：

1. **改写等价核对**（`POST /check`）：维护员替换站点读数入仓质控 SQL 前，提交
   **原始 SELECT** 与**改写 SELECT**，服务在固定模式下枚举每表 **0~3 行**的所有
   合法取值，核对两侧结果**多重集**是否一致（按列位置比较、忽略行序、保留重复），
   用于排查改写对缺测（NULL）与重复采样的影响。
2. **分片装载核对**（`POST /shard-check`）：同一条质控 SELECT 要按站点分片归属在
   多个节点上分别执行时，服务逐片生成**装载清单与分片 SELECT**，并在隔离
   PostgreSQL 中按清单逐片装载、执行、合并，与全量快照上的原查询结果做多重集核对，
   通过才返回清单与各片 SELECT。

- 差异必须在**隔离 PostgreSQL** 中实际复现才报 `NOT_EQUIVALENT`，并返回
  可装载 SQL 与两侧实际结果；
- 界内无反例报 `EQUIVALENT`（仅三行界内结论，不构成更大表上的证明）；
- 求解或复现失败报 `UNKNOWN`。

## 固定模式

```sql
CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER);
CREATE TABLE readings (station_id INTEGER REFERENCES stations(id), value INTEGER);
```

全列 `integer`；非主键列可 `NULL`；`readings.station_id` 为外键（NULL 合法）。

## 支持的 SQL 子集（其余一律 400 拒绝）

| 子句 | 支持内容 |
| --- | --- |
| SELECT | 仅列投影（可带表限定、别名；不支持 `*`、表达式、函数、字面量） |
| FROM | 单表，或两表间至多一次 `INNER` / `LEFT [OUTER]` 等值连接（`ON a.col = b.col`） |
| WHERE | 列与整数字面量比较（`=` `!=` `<` `<=` `>` `>=`）、`IS [NOT] NULL`、`AND` / `OR` / `NOT`、括号 |

不支持：聚合 / `GROUP BY` / `HAVING` / `ORDER BY` / `LIMIT` / `DISTINCT` /
子查询 / `IN` / `BETWEEN` / 字符串与浮点字面量 / 列间比较（WHERE 内）/
自连接 / `RIGHT` / `FULL` / `CROSS` 连接 / 写操作等。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# API: http://localhost:8000  交互文档: http://localhost:8000/docs
```

`docker-compose.yml` 启动两个服务：`db`（postgres:16，仅内部网络，专用于
反例复现）与 `api`（FastAPI）。无需初始化数据——复现用的表结构由服务在
临时 schema 中自建，事务回滚即清理。

## API 用法

### `POST /check`

```json
{
  "query_a": "SELECT id FROM stations WHERE threshold = 5",
  "query_b": "SELECT id FROM stations WHERE threshold = 5 OR threshold IS NULL",
  "solver_timeout_ms": 10000
}
```

响应（`NOT_EQUIVALENT` 示例）：

```json
{
  "verdict": "NOT_EQUIVALENT",
  "bound_rows": 3,
  "message": "两侧结果存在差异，已在隔离 PostgreSQL 中实际复现。",
  "counterexample": {
    "setup_sql": "DROP TABLE IF EXISTS readings;\nDROP TABLE IF EXISTS stations;\nCREATE TABLE stations ...\nINSERT INTO stations ...",
    "query_a_result": [],
    "query_b_result": [[-1]]
  }
}
```

- `verdict=EQUIVALENT`：每表 0~3 行所有合法取值下结果多重集相同；
- `verdict=NOT_EQUIVALENT`：`counterexample.setup_sql` 可直接装载到任意
   scratch 库复现差异，`query_a_result` / `query_b_result` 为实际执行结果；
- `verdict=UNKNOWN`：z3 超时/无法判定，或 PostgreSQL 复现失败（`message`
  说明原因）。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/check \
  -H 'Content-Type: application/json' \
  -d '{"query_a":"SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id",
       "query_b":"SELECT id FROM stations"}'
```

### `POST /shard-check`

同一条站点质控 SELECT 要在多个节点（分片）上跑：每个站点恰好归属一个分片，
服务逐片给出装载清单，并实际核对"各片结果合并"与"全量查询结果"的多重集一致后
才返回清单。

**查询限制**：以 `stations` 为左表的 `LEFT [OUTER] JOIN` 等值连接
（`ON 列=列`，列投影与 WHERE 沿用上面的受支持语法）；也接受仅查 `stations`
单表的退化场景。`INNER JOIN`、以 `readings` 为驱动表等一律 400 拒绝。

请求字段：

| 字段 | 说明 |
| --- | --- |
| `query` | 质控 SELECT（stations 左表 LEFT 等值连接） |
| `stations` | stations 全量快照 `[{id, threshold}]`，沿用主键唯一非空、int4 约束 |
| `readings` | readings 全量快照 `[{station_id, value}]`，沿用外键约束（NULL 合法）；**输入顺序即行号，从 1 开始，重复读数逐条保留** |
| `assignments` | 分片归属 `[{shard, station_ids}]`，每站只归一片且所有站点均被覆盖；允许空分片 |

每片清单（`manifests[]`）字段：

| 字段 | 含义 |
| --- | --- |
| `station_ids` | 本片归属站点 ID |
| `matched_reading_rows` | 与本片归属站点满足 **ON** 的读数**输入行号**（1 起、升序去重）。只由 ON 决定，**WHERE 会滤掉的匹配读数同样随片装载**；读数可跨片使用——匹配多个分片的读数会出现在每个分片清单中（片内只装一份） |
| `extra_station_ids` | 清单读数的外键 `station_id` 所需、但不归属本片的额外站点 ID（去重升序）。当 ON 不是外键列时尤其关键，不装载会违反外键约束 |
| `shard_query` | 在原 SELECT 上把左表驱动限定为 `s.id IN (本片站点)` 的分片 SELECT；空分片用恒假条件 `s.id IS NULL`，不产出任何驱动行 |

服务随后在**隔离 PostgreSQL** 中：为全量基线与每个分片各建独立临时 schema，
按清单先装 stations（归属站点 + 额外站点）再装 readings（父表先行以满足外键），
执行全量原查询与各片 `shard_query`，把各片结果合并后按列位置做多重集比较，
事务回滚即清理，不留任何对象。

响应 `status`：

- `VERIFIED`：两边多重集一致，返回 `manifests`、各片实际行数 `shard_row_counts`
  与两侧结果（超长截断，见 `full_result_truncated`）；
- `MISMATCH`：核对未通过，`message` 说明原因，`mismatch` 给出取值行在两侧的
  出现次数差异样例，**不返回清单**；
- `ERROR`：隔离 PostgreSQL 执行失败（如不可达），`message` 说明原因。

curl 示例（跨片匹配，注意行 2 的读数会被 WHERE 滤掉但仍随 north 片装载）：

```bash
curl -s -X POST http://localhost:8000/shard-check \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "SELECT s.id, r.value FROM stations s LEFT JOIN readings r ON s.id = r.station_id WHERE r.value > 10",
    "stations": [{"id":1,"threshold":20},{"id":2,"threshold":30},{"id":3,"threshold":40}],
    "readings": [{"station_id":1,"value":11},{"station_id":1,"value":5},
                 {"station_id":2,"value":22},{"station_id":3,"value":9}],
    "assignments": [{"shard":"north","station_ids":[1,2]},
                    {"shard":"south","station_ids":[3]}]
  }'
```

`/docs`（Swagger）内置三个可直接 "Try it out" 的命名示例：

- `cross-shard-left-join`：跨片匹配 + WHERE 滤掉的读数仍随片装载；
- `cross-shard-extra-fk-stations`：ON 为非外键等值列时，外键额外站点随片装载；
- `single-table-stations`：仅 stations 单表的退化分片。

### 其他端点

- `GET /`：服务信息；
- `GET /health`：健康检查（含数据库连通性）；
- `GET /docs`：Swagger 交互文档，内置等价 / 不等价（NULL 语义、LEFT JOIN
  重复行）/ 跨片分片等命名示例，可直接 "Try it out"。

## 判定原理

1. **解析与白名单校验**（`app/sql_parser.py`，基于 sqlglot）：越界语法拒绝。
2. **z3 有界判定**（`app/z3check.py`）：两表各 0~3 行符号化，施加主键唯一、
   外键引用、int4 值域约束；WHERE 按 SQL 三值逻辑编码（`UNKNOWN` 被过滤）；
   JOIN/LEFT JOIN 展开为符号行；结果多重集差异编码为"存在某元组值两侧计数
   不同"。`UNSAT` 即界内等价，`SAT` 提取候选反例。
3. **PostgreSQL 复现**（`app/verifier.py`）：候选反例在独立临时 schema 中
   建表、装载、实际执行两条查询并比较多重集；确认差异才报
   `NOT_EQUIVALENT`，否则报 `UNKNOWN`。

分片核对（`POST /shard-check`）的处理流程：

1. **解析与限制**（`app/sql_parser.py` + 端点校验）：复用白名单解析器，并要求
   `stations` 为左表的 LEFT 等值连接（或 stations 单表）；
2. **快照与归属校验**（`app/sharding.py`）：int4 值域、主键唯一、外键引用完整；
   分片名唯一、每站恰好归一片且全覆盖；
3. **清单规划**（`app/sharding.py`）：按 ON 的 SQL 等值语义（任一侧 NULL 不匹配）
   逐片求匹配读数行号，与 WHERE 无关；再由清单读数的外键求额外站点 ID；
   分片 SELECT 用固定别名 `s`/`r` 渲染，在 WHERE 追加 `s.id IN (...)` 驱动谓词；
4. **隔离执行与核对**（`app/verifier.py::ShardVerifier`）：全量基线与每个分片各用
   独立临时 schema，单事务内建表带约束、按清单装载、实际执行，回滚清理；
   合并各片结果与全量结果做规范化多重集比较（NULL 可比较、忽略行序、保留重复）。

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/postgres` | 复现用 PostgreSQL DSN |
| `SOLVER_TIMEOUT_MS` | `10000` | z3 求解超时（可被请求字段 `solver_timeout_ms` 覆盖） |

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/                       # 单元测试（解析器 / z3 / API 打桩）
DATABASE_URL=postgresql://... pytest tests/test_e2e.py   # 端到端（需真实 PG）
```

## 已知边界

- 等价结论仅限每表 ≤3 行（有界模型检验，非全量证明）；
- 仅支持上述 SQL 子集；两查询投影列数不一致返回 400；
- 复现使用单事务 + 临时 schema，不在库中留存对象；数据库不可达时
  不等价判定降级为 `UNKNOWN`（等价判定不依赖数据库）；
- `/shard-check` 仅接受 stations 左表 LEFT 等值连接（或 stations 单表）；
  快照违反固定表约束、分片归属不满足每站一片均返回 400；
- `/shard-check` 的结果回传按结果集行数上限截断（多重集比较本身不截断）；
  数据库不可达时返回 `ERROR`，不返回清单。
