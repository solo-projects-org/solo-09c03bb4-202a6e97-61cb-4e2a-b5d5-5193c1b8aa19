# 售后数仓 · 车型适配拆库服务

把车型适配明细拆成**备件表**与**工况表**两张表存入 SQLite，不留任何逐对
"备件 × 工况"配对明细；车间按车型把两表笛卡尔拼回时，保证不会出现不适用
的组合。FastAPI 提供装载、批量更正与取数接口，Docker Compose 一键启动。

> 本次已优化原批量更正 API：补录一两个配对（哪怕净增只有一个备件/一行维表）
> 不再整表重写该车型的两张表；核定只读"车型规模 + 本批涉及成员"，通过后
> 只增删状态变化的维表行，并在同一事务更新规模。详见下文
> [增量更正：实现与实测](#五增量更正实现与实测)。

## 核心规则

1. **按车型录入备件号与工况配对**，重复配对只算一条（服务端去重）。
2. 可入库的配对集合必须是完整矩形：`配对数 = 备件种数 × 工况种数`。
   只有两表能**原样无损拼回**这些配对时才入库；否则拒绝，并指出一对
   "两表会拼出、但拟入库数据中没有"的**实际缺失组合**，已入库的原数据照常可查。
3. **入库后不留逐对明细**：持久层只有 `vehicle_models`、`model_parts`、
   `model_conditions` 三张表（`vehicle_models` 上另维护两表规模列
   `part_count` / `condition_count`），配对关系由两表笛卡尔积隐式表达。
4. **批量更正**（plus 标"有"、minus 标"无"）按整批更正后的结果核定，
   通过才在同一事务内增量更新两张维表与规模列；不通过整体回滚，两表与规模
   保持原样。**plus 命中已有配对时按保留处理**（幂等，不多计数、不产生写入）。
5. **核定不遍历在库成员、不展开在库配对**：规模直接读 `vehicle_models`
   单行；两张维表只查本批涉及的备件/工况成员（行数 ≤ 批次去重成员数），
   未出现在批次中的成员用规模差判定，跳过不读；任何 SQL 都不同时把两张
   持久维表作为行源（从不 JOIN 两表）。反例仅在单个问题备件范围内
   `LIMIT 1` 取证，必要时最多另读一张维表。
6. **写入是行级增量**：只 `INSERT` / `DELETE` 在册状态发生变化的维表行，
   外加一行规模更新；不再 `DELETE` 整车型后重灌。无状态变化的批次
   （如重复 plus 已有配对、纯幽灵 minus）不产生任何持久表写入。
7. 取数一次返回该车型同时生效的两张表（回执读全表是接口契约，不属于核定范围）。

## 目录结构

```
app/
  main.py        FastAPI 路由与 Swagger 元数据
  service.py     装载 / 批量更正 / 取数业务逻辑（增量核定算法在此）
  database.py    SQLite 连接、建表、旧库规模列迁移与一次性扫描回填
  schemas.py     请求/响应模型
examples/        可直接 curl 的小批更正请求样例
scripts/
  smoke_small_batch.sh  一串可试的小批更正冒烟
tests/           28 个关键流程 / 范围 / 反例 / 迁移 / 回滚自测
Dockerfile
docker-compose.yml
requirements.txt
```

## 用 Docker Compose 启动

```bash
docker compose up --build
```

- API 地址：`http://localhost:8000`
- Swagger UI（在线试用）：`http://localhost:8000/docs`
- OpenAPI 文档：`http://localhost:8000/openapi.json`
- SQLite 数据保存在命名卷 `warehouse-data`（容器内 `/data/warehouse.db`），
  容器重建不丢数据。
- **用旧版本建过的 `warehouse.db` 直接挂上来即可**：启动时会自动补
  `part_count` / `condition_count` 两列，并对两张原维表做**一次**聚合扫描
  回填规模；此后更正不再全表扫描。

停止：`docker compose down`（加 `-v` 会同时删除数据卷）。

## 本地无 Docker 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
WAREHOUSE_DB=./data/warehouse.db .venv/bin/uvicorn app.main:app --port 8000
```

## 一分钟试一个小批更正

下面的小例子从 `1 备件 × 1 工况` 开始，**只补录一个配对**
`(P-1, C-B)`，目标是 `1 × 2` 完整矩形；持久层只向工况表插入 1 行。

```bash
BASE=http://localhost:8000

# 1) 装载 1×1 小车型
curl -sS -X POST $BASE/api/v1/models/load \
  -H 'Content-Type: application/json' \
  -d @examples/load_tiny.json

# 2) 只补一个配对 -> 200，回执含更正前规模 before 与更正后两表
curl -sS -X POST $BASE/api/v1/models/correct \
  -H 'Content-Type: application/json' \
  -d @examples/correct_add_one_pair.json

# 3) 重复 plus 已有配对 -> 200（保留/幂等），updatedAt 都不变
curl -sS -X POST $BASE/api/v1/models/correct \
  -H 'Content-Type: application/json' \
  -d @examples/correct_repeat_existing_pair.json

# 4) 净增一个备件 P-2（补齐两个工况）-> 备件表只多 1 行
curl -sS -X POST $BASE/api/v1/models/correct \
  -H 'Content-Type: application/json' \
  -d @examples/correct_add_one_part.json

# 5) 只删一个角 -> 422 并给出实际缺失组合，两表与规模不变
curl -sS -X POST $BASE/api/v1/models/correct \
  -H 'Content-Type: application/json' \
  -d @examples/correct_reject_missing_corner.json
```

也可以直接跑整串冒烟：

```bash
BASE_URL=http://localhost:8000 bash scripts/smoke_small_batch.sh
```

第 2 步响应（注意 `before` 是更正前规模，`parts`/`conditions` 是更正后两表）：

```json
{
  "modelId": "DEMO-TINY",
  "status": "corrected",
  "partCount": 1,
  "conditionCount": 2,
  "effectivePairCount": 2,
  "parts": ["P-1"],
  "conditions": ["C-A", "C-B"],
  "before": {"partCount": 1, "conditionCount": 1, "effectivePairCount": 1}
}
```

第 5 步拒收响应：

```json
{
  "detail": {
    "message": "按整批更正后的结果核定失败，两表会拼出更正后集合中不存在的组合，本批已整体拒绝",
    "extraCombinationExample": {"partNo": "P-1", "conditionCode": "C-A"}
  }
}
```

## Swagger 试用样例

### 1. 装载（`POST /api/v1/models/load`）

合法矩形（2 备件 × 2 工况 = 4 对，含一条重复配对，重复只算一条）：

```json
{
  "modelId": "SEDAN-A1",
  "pairs": [
    {"partNo": "P-1001", "conditionCode": "C-HOT"},
    {"partNo": "P-1001", "conditionCode": "C-COLD"},
    {"partNo": "P-1002", "conditionCode": "C-HOT"},
    {"partNo": "P-1002", "conditionCode": "C-COLD"},
    {"partNo": "P-1002", "conditionCode": "C-COLD"}
  ]
}
```

非矩形（缺 `P-1002 / C-COLD` 这一对）会返回 422，并给出反例：

```json
{
  "modelId": "SEDAN-A2",
  "pairs": [
    {"partNo": "P-1001", "conditionCode": "C-HOT"},
    {"partNo": "P-1001", "conditionCode": "C-COLD"},
    {"partNo": "P-1002", "conditionCode": "C-HOT"}
  ]
}
```

响应：

```json
{
  "detail": {
    "message": "配对集合不是完整矩形，拆成两表后会拼出拟入库数据中没有的组合",
    "extraCombinationExample": {
      "partNo": "P-1002",
      "conditionCode": "C-COLD"
    }
  }
}
```

### 2. 批量更正（`POST /api/v1/models/correct`）

整批：移除备件 P-1002 的两个工况，同时给 P-1001 增加新工况 C-DUST。
更正后为 `P-1001 × {C-HOT, C-COLD, C-DUST}` 完整矩形，整批通过。
写库时只删除 P-1002 一行备件、插入 C-DUST 一行工况并更新规模：

```json
{
  "modelId": "SEDAN-A1",
  "plus":  [{"partNo": "P-1001", "conditionCode": "C-DUST"}],
  "minus": [
    {"partNo": "P-1002", "conditionCode": "C-HOT"},
    {"partNo": "P-1002", "conditionCode": "C-COLD"}
  ]
}
```

只删一对（剩余配对不是矩形）会被整批拒绝，两表保持原样：

```json
{
  "modelId": "SEDAN-A1",
  "plus": [],
  "minus": [{"partNo": "P-1001", "conditionCode": "C-HOT"}]
}
```

重复 plus 已有配对（保留，幂等通过）：

```json
{
  "modelId": "SEDAN-A1",
  "plus":  [{"partNo": "P-1001", "conditionCode": "C-HOT"}],
  "minus": []
}
```

### 3. 取数（`GET /api/v1/models/SEDAN-A1`）

一次响应同时返回两张生效表：`parts`（备件表）与 `conditions`（工况表），
并附 `effectivePairCount`（两表拼回后的配对数）。

### 4. 车型清单（`GET /api/v1/models`）

## 错误码约定

| 状态码 | 含义 |
| --- | --- |
| 200 | 成功 |
| 404 | 车型不存在（取数/更正） |
| 409 | 车型已入库（重复装载，更正请走 correct 接口） |
| 422 | 入参为空、plus/minus 同批冲突，或核定不通过（含反例，两表不变） |

## 五、增量更正：实现与实测

### 算法要点

现行两表对车型 `M` 是完整矩形 `P × C`，规模 `p = |P|`、`c = |C|`
持久化在 `vehicle_models.part_count / condition_count`。对批次
`plus = A`、`minus = D`（各自去重、`A ∩ D = ∅`）：

1. 只读规模行 `(p, c)`，并用 batch TEMP 表 JOIN 出
   `P_b = 批次备件 ∩ P`、`C_b = 批次工况 ∩ C`（行数 ≤ 批次成员数）。
2. 在批次大小的 Python 集合上推导：
   - 新工况 `new_conds = A_conds − C_b`；
   - 某工况被多少在册备件有效 minus，等于 `p` 时整列删空；再扣除被
     plus 带回的，得到真正注销的 `gone`，`c' = c − |gone| + |new_conds|`；
   - 本批内在册备件 x 的目标工况数
     `target_x = c − removed_x + |plus_x − C_b|`，`0 < target_x ≠ c'` 即违规；
   - `p > |P_b|` 说明存在未涉及备件；此时只有 `c' = c` 才合法
     （整列删空必然已把所有备件拉进批次），否则用一条 `LIMIT 1`
     反查一个未涉及备件做反例部件；
   - 批次新备件必须覆盖全部 `c'` 个工况；只出现在 minus 中的幽灵成员忽略。
3. 集合恒有 `S_x ⊆ C'`，所以"数量相等"等价于"集合相等"，不需要展开配对。
4. 通过后写集合恰好是：删 `target=0` 的在册备件、插覆盖全工况的新备件、
   删 `gone` 工况、插 `new_conds` 工况，再把新规模写回车型行；同一事务提交。
5. 任何核定失败都在写持久表之前抛 422；写库异常走 `rollback()`，
   规模列与两表一起保持原值。

### 小批读写范围（SQL 追踪 + rowid 实测）

在 50 备件 × 3 工况的样例上追踪一次"新增一个备件（补 3 个工况）"：

```text
SELECT model_id, part_count, condition_count FROM vehicle_models WHERE model_id = ?
SELECT mp.part_no FROM model_parts mp      JOIN batch_parts bp ...   -- ≤ 批次成员
SELECT mc.condition_code FROM model_conditions mc JOIN batch_conds ... -- ≤ 批次成员
INSERT INTO model_parts(model_id, part_no) VALUES ('BIG', 'PNEW')     -- 仅 1 行
UPDATE vehicle_models SET part_count=51, condition_count=3, ...      -- 规模 1 行
-- 提交后才是回执要求的两张整表 SELECT
```

- 100 个在册备件上做"净增一个备件"，提交前后逐行比对 SQLite `rowid`：
  **100 个未涉及备件行 rowid 全部不变**，仅多出 `PNEW`；工况表 0 写入。
- 重复 plus 已有配对：持久表 DML 为 **0 条**（`updatedAt` 也不变）。
- 拒收批次：所有连接上持久表 DML 为 **0 条**，原 rowid 集合不变。
- 测试 `test_successful_tiny_batch_write_and_read_scope` 用 SQL 追踪固化了
  "写连接上维表 SELECT 必须由 batch_* 限定、不得有无成员条件的整车型 DELETE、
  不得两表 JOIN"。

### 新旧对比实测

环境：Python 3.11.2 / sqlite 3.40.1，单机 TestClient，车型
**20,000 备件 × 4 工况 = 80,000 个隐式配对**，批次 = 新增 1 个备件的 4 条 plus：

| 实现 | 更正端到端 | 在册备件行被物理重写 | 工况行被重写 |
| --- | ---: | ---: | ---: |
| 基线（整表 DELETE + 重灌） | 80.1 ms | 20000 / 20000 | 4 / 4 |
| 本次增量版 | 22.1 ms | **0 / 20000**（仅 INSERT 1 行） | **0 / 4** |

隔离"核定 + 写入"（把回执的整表读取打桩掉），车型提到
**50,000 备件 × 4 工况**，连续 5 次新增备件：

```text
增量版 check+write (ms): 16.4 15.8 10.9 10.4 9.5
基线   check+write (ms): 185.0 159.6 180.4 145.6 153.5
```

即写路径从 O(全车规模) 降到 O(批次大小)，本样例约 **12–15 倍**；
拒收单个配对两条路径都是毫秒级（与车型规模无关）。端到端数字里剩余的
主要耗时是回执按契约读回两张整表并序列化。

复现脚本（结果随机器波动，重点看"重写行数"与数量级）：

```bash
# 当前优化版
python3 scripts/bench_correction.py 20000

# 基线版对比（HEAD 即平台归档基线）
git worktree add /tmp/baseline HEAD
cd /tmp/baseline && python3 /workspace/scripts/bench_correction.py 20000
```

## 自测

```bash
.venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/python -m pytest tests/ -v
```

当前结果：**28 passed**。在原有 15 个用例（矩形装载与去重、非矩形拒绝与反例、
409、整批更正成功/失败回滚、一次取两表、无逐对明细表、plus/minus 冲突、404、
大车型小批次性能、SQL 级"从不两表 JOIN"追踪、新备件/新工况/幽灵成员边界）
之外，新增 `tests/test_incremental_scope.py` 共 13 个用例，覆盖：

- 重复 plus 已有配对按保留处理；
- 新增一个备件 / 一个工况时的 rowid 稳定性与精确写入行；
- SQL 追踪核定读范围（batch_* 限定）、禁止整车型重写 DELETE、禁止两表 JOIN；
- 拒收时零持久 DML；
- 4 组形状的反例语义合法性（属于目标矩形、但不在更正后集合中）；
- 旧库（三列 `vehicle_models` 老 schema）启动扫描回填 + 更正；
- 不经 lifespan 直接调 service 的旧库自愈；
- 触发器注入写故障后两表与规模不变；
- 120 组固定种子随机批次暴力预言机（成功比对两表/规模，失败比对反例与原样保留）。

另外用独立脚本跑过约 1800 组随机/链式更正预言机（含连续 25 步更正后
规模列与实表 COUNT 无漂移），全部通过。

## 待补事项 / 实测边界

- 本沙箱**没有 docker / compose / podman 可执行文件，也没有特权用户命名空间**，
  因此 `docker compose up --build` 未能在此环境真实起容器验证；已完成的等价验证：
  `docker-compose.yml` YAML 解析通过且仍指向现有 Dockerfile；用与镜像完全相同的
  CMD（`uvicorn app.main:app --host 0.0.0.0 --port 8000`）在本机起服并跑通
  `examples/` 全部 curl；Dockerfile 的 HEALTHCHECK urllib 单行检查实测通过；
  依赖按 `requirements.txt` 在 Python 3.11 安装并跑通全部测试。
  **在有 Docker 的机器上执行一次 `docker compose up --build` 仍建议作为上线前确认。**
- 规模列的进程内一致性由 `_write_lock` + 单事务保证；当前 compose/镜像均为单
  uvicorn 进程。若将来开多 worker 或多实例共挂一个 SQLite 卷，需要改用
  SQLite 写事务/`BEGIN IMMEDIATE` 或外部锁来替代进程内锁。
- 旧库"只扫描一次"发生在新版 `init_db` 首次启动（新增规模列时的回填 UPDATE）；
  绕过启动直接调 service 时，若读到规模 0 会按车型惰性补扫一次。库外手工
  改维表且不更新规模列不属于受支持写入路径。
- 目前没有鉴权/审计与车型废弃（清空）接口；清空车型仍按原约定返回 422。
