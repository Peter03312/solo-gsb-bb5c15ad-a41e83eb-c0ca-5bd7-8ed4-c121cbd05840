# 化学试剂混配风险预检 HTTP API

提交**容器初始液体、按顺序执行的加液步骤、试剂浓度与体积、容器材质与温度**，
服务逐步计算混配后各组分的浓度，依据内置的公开化学相容性规则识别：

- **禁配组合**（forbidden_mix）：如强酸 + 漂白水 → 氯气、漂白水 + 氨水 → 氯胺、氧化剂 + 易燃物等；
- **超时接触**（overtime_contact）：允许短时接触的反应对超过允许接触秒数（可用 `contact_allowances` 声明覆盖默认值）；
- **材质不相容**（material_incompatible）：如 HF 进玻璃容器、强碱长期接触玻璃、酸对碳钢/铝的腐蚀等。

输出**首次触发步骤、涉及组分、触发时刻的估算浓度与阻断原因**；`dilute`（稀释）、`pour`（倒出）、
`clean`（清洗）会改变后续状态。输入存在问题时**逐项拒绝**（序号重复、体积非正、浓度越界、
未知试剂、清洗后仍引用旧状态等），一次性返回全部错误。

> 用途：实验室/工艺 SOP 的自动化预检与教学。规则库为简化的公开化学常识集合，
> **不能替代** MSDS/SDS 与专业 EHS 评估。

> **本轮新增**：
> 1. 双容器定量转移预检 `POST /api/v1/transfer-precheck`
>    （[说明见此节](#双容器转移预检)）；
> 2. **实测浓度对账** `POST /api/v1/reconcile` 与 `POST /api/v1/transfer-reconcile`
>    （[说明见此节](#实测浓度对账本轮新增)）：提交**原流程请求 + 统一容差 + 若干实测观测**，
>    先沿用原校验与模拟，再比较对应步骤完成后的**未四舍五入**预测浓度，返回预检风险、
>    逐条预测/偏差与最早超差位置。
>
> 原 `/api/v1/precheck`、`/api/v1/transfer-precheck` 的**请求与响应完全保持兼容，
> 启动方式不变**（仍是同一个 `uvicorn app.main:app` / `docker compose up`，同一端口 8000）。

---

## 目录

- [快速开始（Docker Compose）](#快速开始docker-compose)
- [本地运行（不用 Docker）](#本地运行不用-docker)
- [API 说明](#api-说明)
- [双容器转移预检](#双容器转移预检)
- [实测浓度对账（本轮新增）](#实测浓度对账本轮新增)
- [逐步状态与浓度模型](#逐步状态与浓度模型)
- [操作类型](#操作类型)
- [规则、阈值与接触计时](#规则阈值与接触计时)
- [错误码（逐项拒绝）](#错误码逐项拒绝)
- [内置试剂 / 材质 / 规则](#内置试剂--材质--规则)
- [调用示例（curl / Python）](#调用示例curl--python)
- [测试](#测试)
- [项目结构](#项目结构)

---

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000
curl http://localhost:8000/health
curl http://localhost:8000/catalog | python -m json.tool
```

发起一次预检（禁配：往漂白水里加盐酸 → 氯气）：

```bash
curl -s http://localhost:8000/api/v1/precheck \
  -H 'Content-Type: application/json' \
  --data @examples/forbidden_mix.json | python -m json.tool
```

## 本地运行（不用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## API 说明

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查，返回规则库版本 |
| `GET` | `/catalog` | 内置试剂、容器材质与全部规则（含选择器、阈值、默认接触时长） |
| `POST` | `/api/v1/precheck` | 单容器混配流程，返回风险与逐步状态（请求/响应保持兼容） |
| `POST` | `/api/v1/transfer-precheck` | **双容器转移预检**：两只容器之间的定量转移与静置，返回带容器编号的风险与两侧状态 |
| `POST` | `/api/v1/reconcile` | **单容器实测浓度对账（本轮新增）**：原预检请求 + 容差 + 观测，返回预检结果与逐条偏差 |
| `POST` | `/api/v1/transfer-reconcile` | **双容器实测浓度对账（本轮新增）**：观测须带容器编号，其余同上 |

### 请求体

```json
{
  "vessel_material": "glass",
  "temperature_c": 22.0,
  "initial_liquid": [
    {"reagent_id": "naocl", "concentration": 10.0, "volume_ml": 100.0}
  ],
  "steps": [
    {"seq": 1, "op": "add", "reagent_id": "hcl",
     "concentration": 30.0, "volume_ml": 50.0, "at_seconds": 30}
  ],
  "contact_allowances": []
}
```

| 字段 | 说明 |
| --- | --- |
| `vessel_material` | 容器材质编号，见 `/catalog`（如 `glass`/`hdpe`/`ptfe`/`aluminum`） |
| `temperature_c` | 体系温度 ℃，允许 [-50, 300]；达到部分规则的高温阈值时阻断原因会追加风险提示 |
| `initial_liquid[]` | 容器初始液体；同一试剂不得重复出现 |
| `steps[]` | 按顺序执行的操作；`seq` 为正整数且**不得重复**，乱序提交会按 `seq` 稳定排序处理 |
| `steps[].at_seconds` | 该步骤完成时刻（相对流程起点，秒），非负、不早于上一步；缺省沿用上一步时刻 |
| `contact_allowances[]` | 为反应对声明允许接触时长（秒），覆盖规则默认值；端可写试剂编号或材质编号（`glass` 或 `material:glass` 均可） |

### 响应（发现风险时 HTTP 200，`blocked=true`）

```json
{
  "safe": false,
  "blocked": true,
  "first_blocking_seq": 1,
  "ruleset_version": "chem-rules-2026-10-01",
  "vessel_material": "glass",
  "temperature_c": 22.0,
  "findings": [
    {
      "seq": 1,
      "category": "forbidden_mix",
      "rule_id": "F01",
      "rule_name": "强酸 + 次氯酸盐释放氯气",
      "entities": ["hcl", "naocl"],
      "concentrations": {"hcl": 10.0, "naocl": 6.6667},
      "contact_seconds": null,
      "allowed_seconds": null,
      "severity": "critical",
      "reason": "强酸使次氯酸盐转化为氯气（Cl₂）……",
      "guidance": "严禁向漂白水中加酸……",
      "blocking_reason": "禁配组合：强酸 + 次氯酸盐释放氯气（F01）在步骤 1 首次共存"
    }
  ],
  "steps": [ /* 每个步骤完成后的状态快照，按 seq 排序 */ ],
  "final_state": { /* 末态快照 */ }
}
```

- `findings` 按 `(seq, 类别)` 稳定排序：初始液体触发时 `seq=0`；类别顺序为禁配 → 材质不相容 → 超时接触。
- 每个状态快照包含：`at_seconds`、`liquid_volume_ml`、`cleaned`、`components[]`
  （`reagent_id` / `name` / `concentration` / `pure_volume_ml`）。
- 超时类 finding 另含 `contact_seconds`（实际接触时长）与 `allowed_seconds`（允许时长）。

完整样例见 [`examples/response_blocked.json`](examples/response_blocked.json)。

### 错误响应（HTTP 400，逐项列出）

```json
{
  "error": "validation_error",
  "message": "预检请求被逐项拒绝，未进行模拟",
  "details": [
    {"code": "DUPLICATE_STEP_SEQ", "message": "步骤序号 2 重复", "seq": 2, "field": "seq"},
    {"code": "INVALID_VOLUME", "message": "体积须为正数（毫升），收到 0.0", "seq": 3, "field": "volume_ml"}
  ]
}
```

---

## 双容器转移预检

`POST /api/v1/transfer-precheck` 判断**已配液在两只容器之间定量转移**时的风险。
请求描述**编号互异、各有材质与初始液体的两只容器、统一温度**，以及按时刻执行、
**同刻按序号（seq）执行**的 `transfer`（定量转移）与 `hold`（静置）步骤。
沿用同一套试剂、浓度阈值与相容性规则（F/T/M 规则、`contact_allowances` 覆盖均通用）。

### 转移语义（充分混合假设）

- **transfer**：按转出体积占源容器当前体积的比例，**同步扣减源容器各组分纯物量与总体积**
  （部分转出浓度不变）；同一份纯物量与体积**计入目标容器**（目标既有液体会被稀释，各组分重新计浓）。
- **接触计时跨容器延续规则**：两侧**既有且仍达阈值**的接触片段继续计时；
  **仅新达阈值**的接触（如转入的试剂与目标容器既有组分/材质首次共存）**从转入时刻起**作为新片段计时；
  任一条件跌破阈值（含源容器被**转空**）片段即结束，重新满足则重新计时。
- **检查顺序**：初始状态（t=0）也检查风险；每个步骤**先把时间推进到该时刻并结算两侧此前超时**，
  再执行操作，然后检查转入后的**即时禁配与材质风险**。
- 发现风险时模拟继续完成（可看到全部首次触发点），响应给**首次阻断**；输入非法时**整体拒绝、不返回任何部分模拟结果**。

### 请求体

```json
{
  "vessels": [
    {"vessel_id": "acid-bottle", "vessel_material": "hdpe",
     "initial_liquid": [{"reagent_id": "hcl", "concentration": 30.0, "volume_ml": 100.0}]},
    {"vessel_id": "bleach-tank", "vessel_material": "hdpe",
     "initial_liquid": [{"reagent_id": "naocl", "concentration": 10.0, "volume_ml": 100.0}]}
  ],
  "temperature_c": 22.0,
  "steps": [
    {"seq": 1, "op": "transfer", "from_vessel": "acid-bottle",
     "to_vessel": "bleach-tank", "volume_ml": 50.0, "at_seconds": 30.0}
  ],
  "contact_allowances": []
}
```

| 字段 | 说明 |
| --- | --- |
| `vessels` | **恰好 2 只**容器；`vessel_id` 非空且**互异**，`vessel_material` 见 `/catalog`，各自带 `initial_liquid`（同容器内试剂不得重复） |
| `temperature_c` | 两只容器的统一温度 ℃，允许 [-50, 300] |
| `steps[].op` | `transfer`（定量转移）或 `hold`（仅推进时间） |
| `transfer` 字段 | `from_vessel` / `to_vessel`（两只互异的已知容器）、`volume_ml`（>0，且不超过源容器当前体积） |
| `at_seconds` | 同单容器：非负、不回退；缺省沿用上一步时刻；`hold` 须显式且严格晚于当前时刻 |
| `contact_allowances` | 与单容器相同的反应对时长覆盖，对两侧容器同时生效 |

### 响应要点

- `first_blocking_seq` + **`first_blocking_vessel_id`**：首次阻断所在步骤与容器；无风险时均为 `null`。
- `findings[]`：每条 finding 额外带 **`vessel_id`**（规则在该容器液相/材质界面上成立），
  `blocking_reason` 以“容器 X：……”开头；排序为 `(seq, 类别, 容器提交顺序)`，初始触发 `seq=0`。
- `steps[]`：每步含 `op` / `at_seconds` / `from_vessel` / `to_vessel` / `volume_ml`，
  以及 **`vessels[]` 两侧状态快照**（各含 `vessel_id`、`vessel_material`、`liquid_volume_ml`、`components[]`）。
- `final_states[]`：两只容器的末态快照（按提交顺序）。

完整样例见 [`examples/transfer_forbidden.json`](examples/transfer_forbidden.json)
与 [`examples/transfer_response_blocked.json`](examples/transfer_response_blocked.json)；
安全/超时样例见 `examples/transfer_safe.json`、`examples/transfer_overtime.json`。

### 转移预检的拒绝码

沿用单容器全部通用码（`DUPLICATE_STEP_SEQ` / `INVALID_SEQ` / `INVALID_VOLUME` /
`CONCENTRATION_OUT_OF_RANGE` / `UNKNOWN_REAGENT` / `UNKNOWN_MATERIAL` / `UNKNOWN_ENTITY` /
`TEMPERATURE_OUT_OF_RANGE` / `INVALID_TIME` / `NON_MONOTONIC_TIME` / `DUPLICATE_COMPONENT` /
`INVALID_STEP_FIELDS` / `INVALID_ALLOWANCE` / `DUPLICATE_ALLOWANCE` / `MALFORMED_REQUEST`），另加：

| Code | 触发条件 |
| --- | --- |
| `INVALID_VESSEL` | 容器数量不是恰好 2 只 |
| `DUPLICATE_VESSEL_ID` | 两只容器 `vessel_id` 相同 |
| `UNKNOWN_VESSEL` | transfer 的源/目标编号不在两只容器中 |
| `SAME_VESSEL` | transfer 的源容器与目标容器相同 |
| `EMPTY_SOURCE_VESSEL` | 源容器为空仍发起转移 |
| `TRANSFER_EXCEEDS_VOLUME` | 转移体积超过源容器当前液体体积 |
| `EMPTY_VESSEL_OPERATION` | 两只容器皆空时执行 hold |

---

## 实测浓度对账（本轮新增）

实验人员核对**实际配液是否沿预检轨迹运行**：提交**原流程请求**、一个**统一的有限非负浓度容差**
（百分点）及若干条实测观测，服务先**沿用原请求校验与模拟**（与 `/api/v1/precheck`、
`/api/v1/transfer-precheck` 完全相同的校验、排序与簿记），再把每条观测与
**对应步骤完成后**的预测浓度比较：

- 观测位置 `position=0` 表示**初始态**；正整数表示该 `seq` 步骤**完成后**的状态
  （步骤按 seq 执行顺序重放，乱序提交的原请求按原规则排序后再对账）；
- 单容器用 `POST /api/v1/reconcile`；双容器用 `POST /api/v1/transfer-reconcile`，
  每条观测**另须指定 `vessel_id`**；
- 每条观测指定试剂编号与 **0～100% 的实测浓度**；
  **该位置未出现的已知试剂按 0% 计**（包括从未加入、已倒空/转出或被清洗掉的试剂）；
- 比较一律使用**未四舍五入的计算值**（预检快照中 `round(...,4)` 的展示值不参与对账），
  `|实测 - 预测| ≤ 容差` 才通过（边界等值算通过）；
- 返回：**原预检风险结果**（`precheck`，与对应预检接口响应逐字段一致）、
  **逐条预测值与偏差**（按观测提交顺序）、**按步骤执行顺序的最早超差位置**
  （初始态最前；同位置按观测提交顺序，双容器同位置不区分容器先后、仍按提交下标）；
- **无观测也可调用**：此时仅返回原预检结果，`within_tolerance=true`。

> 预检发现化学风险（`blocked=true`）与对账是否沿轨迹是两件事：即使原流程有禁配风险，
> 只要实测与预测吻合，对账仍判通过；风险结果在 `precheck` 中照常给出。

### 请求体

单容器（`POST /api/v1/reconcile`）：

```json
{
  "request": {
    "vessel_material": "hdpe",
    "temperature_c": 22.0,
    "initial_liquid": [
      {"reagent_id": "nacl", "concentration": 10.0, "volume_ml": 100.0}
    ],
    "steps": [
      {"seq": 1, "op": "add", "reagent_id": "nacl",
       "concentration": 20.0, "volume_ml": 100.0},
      {"seq": 2, "op": "dilute", "volume_ml": 100.0, "at_seconds": 10}
    ],
    "contact_allowances": []
  },
  "tolerance_percentage_points": 0.2,
  "observations": [
    {"position": 0, "reagent_id": "nacl", "measured_concentration": 10.05},
    {"position": 1, "reagent_id": "nacl", "measured_concentration": 15.10},
    {"position": 2, "reagent_id": "nacl", "measured_concentration": 9.90},
    {"position": 2, "reagent_id": "koh",  "measured_concentration": 0.0}
  ]
}
```

双容器（`POST /api/v1/transfer-reconcile`）的 `request` 与 `/api/v1/transfer-precheck`
请求体完全相同；观测多一个必填的 `vessel_id`：

```json
{
  "request": {
    "vessels": [
      {"vessel_id": "V1", "vessel_material": "hdpe",
       "initial_liquid": [{"reagent_id": "nacl", "concentration": 10.0, "volume_ml": 100.0}]},
      {"vessel_id": "V2", "vessel_material": "hdpe", "initial_liquid": []}
    ],
    "temperature_c": 20.0,
    "steps": [
      {"seq": 1, "op": "transfer", "from_vessel": "V1", "to_vessel": "V2",
       "volume_ml": 40.0, "at_seconds": 30.0}
    ],
    "contact_allowances": []
  },
  "tolerance_percentage_points": 0.3,
  "observations": [
    {"position": 0, "vessel_id": "V1", "reagent_id": "nacl", "measured_concentration": 10.1},
    {"position": 1, "vessel_id": "V2", "reagent_id": "nacl", "measured_concentration": 10.2}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `request` | **原预检请求体**，结构分别与 `/api/v1/precheck`、`/api/v1/transfer-precheck` 完全一致 |
| `tolerance_percentage_points` | 统一容差（百分点）：有限、非负数字（`0` 表示必须完全一致；布尔/NaN/±∞ 拒绝） |
| `observations[].position` | `0`=初始态；正整数=该 `seq` 步骤完成后；必须是 0 或已提交步骤序号之一 |
| `observations[].reagent_id` | 已知试剂编号（见 `/catalog`）；该位置未出现的已知试剂预测浓度按 0% 计 |
| `observations[].measured_concentration` | 实测浓度，**[0, 100]** 闭区间内的有限数字 |
| `observations[].vessel_id` | **仅双容器**：必填，且必须是两只已知容器之一 |
| `observations` | 可省略或为空数组；同一位置（双容器还须同一容器）同一试剂**不得重复观测** |

### 响应要点

```json
{
  "tolerance_percentage_points": 0.5,
  "within_tolerance": false,
  "first_violation": {
    "index": 1,
    "position": 1,
    "reagent_id": "nacl",
    "measured_concentration": 14.1,
    "predicted_concentration": 15.0,
    "abs_deviation": 0.9000000000000004
  },
  "observations": [
    {"index": 0, "position": 2, "reagent_id": "nacl",
     "measured_concentration": 8.0, "predicted_concentration": 10.0,
     "deviation": -2.0, "abs_deviation": 2.0, "within_tolerance": false},
    {"index": 1, "position": 1, "reagent_id": "nacl",
     "measured_concentration": 14.1, "predicted_concentration": 15.0,
     "deviation": -0.9000000000000004, "abs_deviation": 0.9000000000000004,
     "within_tolerance": false}
  ],
  "precheck": { /* 与对应预检接口响应完全相同 */ }
}
```

- `within_tolerance`：全部观测通过（含无观测）为 `true`。
- `observations[]`：按**观测提交顺序**逐条给出未舍入的 `predicted_concentration`、
  带符号 `deviation = 实测 - 预测`、`abs_deviation` 与 `within_tolerance`；双容器条目另含 `vessel_id`。
- `first_violation`：按**步骤执行顺序**（初始态 → 各步骤）找到的最早超差观测，
  同位置取**提交下标最小**者；无超差为 `null`。
- 双容器的 `first_violation` 另含 `vessel_id`。

完整请求/响应样例：[`examples/reconcile_safe.json`](examples/reconcile_safe.json) /
[`examples/reconcile_response_safe.json`](examples/reconcile_response_safe.json)、
[`examples/reconcile_violation.json`](examples/reconcile_violation.json) /
[`examples/reconcile_response_violation.json`](examples/reconcile_response_violation.json)、
[`examples/transfer_reconcile_safe.json`](examples/transfer_reconcile_safe.json) /
[`examples/transfer_reconcile_response_safe.json`](examples/transfer_reconcile_response_safe.json)。

### 对账拒绝码（整单拒绝，不返回部分对账）

原流程请求无效时，**原样返回对应预检的全部错误码**（见上文两节），不产生任何对账字段。
对账参数本身的错误逐项收集、一次性返回：

| Code | 触发条件 |
| --- | --- |
| `INVALID_TOLERANCE` | 容差为负（NaN/±∞/布尔/类型错误按 `MALFORMED_REQUEST` 在 schema 层拒绝） |
| `INVALID_POSITION` | `position` 不是 0 或已提交步骤序号之一（或为负/非整数） |
| `UNKNOWN_REAGENT` | 观测引用未知试剂 |
| `UNKNOWN_VESSEL` | 双容器观测引用未知 `vessel_id`（缺少 `vessel_id` 为 `MALFORMED_REQUEST`） |
| `CONCENTRATION_OUT_OF_RANGE` | 实测浓度超出 [0, 100]（NaN/±∞/布尔为 `MALFORMED_REQUEST`） |
| `DUPLICATE_OBSERVATION` | 同位置同试剂重复观测（双容器按“同位置+同容器+同试剂”判重） |
| `MALFORMED_REQUEST` | JSON/类型/schema 错误（含未知字段） |

---

## 逐步状态与浓度模型

采用透明的“**纯物体积**”簿记（体积可加、充分混合假设）：

- 每个试剂记录纯物体积 `pure_ml`，当前浓度 `= pure_ml / 液体总体积 × 100%`；
- **add**：`pure_ml += concentration × volume / 100`，总体积增加；
- **dilute**：仅总体积增加（加纯水），各组分浓度等比下降；
- **pour**：按倒出比例同步缩减总体积与每个 `pure_ml`（浓度不变）；完全倒空会终止全部接触片段；
- **clean**：清空全部组分与总体积、所有接触计时归零，置 `cleaned=true`。

规则只在组分浓度**达到规则侧阈值**（默认 1%，强酸/强碱等规则更高）时生效，
痕量组分不会触发——稀释越过阈值会结束对应接触片段，重新越过则作为**新片段**重新计时。

## 操作类型

| `op` | 必填字段 | 语义 / 状态影响 |
| --- | --- | --- |
| `add` | `reagent_id`, `concentration`, `volume_ml` | 加入试剂；同一试剂多次加入会合并纯物量 |
| `dilute` | `volume_ml` | 加纯水稀释；**清洗后禁止**（旧状态已不存在） |
| `pour` | `volume_ml` | 均匀倒出，不得超过当前总体积；**清洗后/空容器禁止** |
| `clean` | — | 清洗：清空液体并重置接触计时 |
| `hold` | `at_seconds`（严格晚于当前时刻） | 静置等待，仅推进时间用于超时判定；**空容器/清洗后禁止** |

`at_seconds` 缺省沿用上一步时刻；时间只能前进，回退报 `NON_MONOTONIC_TIME`。

> 双容器转移预检（`/api/v1/transfer-precheck`）只支持 `transfer` 与 `hold` 两种操作，
> 语义见[双容器转移预检](#双容器转移预检本轮新增)。

## 规则、阈值与接触计时

三类规则（完整列表以 `GET /catalog` 为准）：

- `forbidden_mix`：两种试剂一经共存且双方都过阈值，**立即阻断**（F01–F08）；
- `limited_contact`：试剂-试剂限时接触，超时阻断（T01–T02）；
- `material_incompatible`：试剂-容器材质。无接触时长的（如 HF/玻璃）立即阻断（M01、M02）；
  带时长的（如强碱/玻璃 1800 s、酸/碳钢 600 s）超时阻断。

计时规则：

1. 接触“片段”从**双方（含材质）条件同时满足**的那一刻开始；
2. 时间在下一个操作的 `at_seconds` 推进，推进后检查 `已接触时长 > 允许时长`（严格大于）；
3. 倒空、清洗或任一 方浓度跌破阈值都会结束片段；重新满足则重新计时；
4. `contact_allowances` 按反应对（试剂-试剂 或 试剂-材质）覆盖该对所有限时规则的默认秒数；
   它**不能豁免禁配规则**，且必须为正数、两端为不同类的已知实体，重复声明会被拒绝。

## 错误码（逐项拒绝）

| Code | 触发条件 |
| --- | --- |
| `MALFORMED_REQUEST` | JSON/类型/schema 错误（含未知字段，请求模型禁止额外字段） |
| `DUPLICATE_STEP_SEQ` | 步骤序号重复 |
| `INVALID_SEQ` | 序号非正整数 |
| `UNKNOWN_OPERATION` | 未知 `op` |
| `UNKNOWN_REAGENT` / `UNKNOWN_MATERIAL` / `UNKNOWN_ENTITY` | 未知试剂/材质/allowance 实体 |
| `CONCENTRATION_OUT_OF_RANGE` | 浓度 ≤0、>100，或超过该试剂现实可出现上限（如盐酸 37%） |
| `INVALID_VOLUME` | add/dilute/pour 的体积缺失或 ≤0 |
| `INVALID_STEP_FIELDS` | 操作与字段不匹配（如 dilute 带 reagent_id、clean 带体积） |
| `DUPLICATE_COMPONENT` | 初始液体中同一试剂重复出现 |
| `TEMPERATURE_OUT_OF_RANGE` | 温度超出 [-50, 300] ℃ |
| `INVALID_TIME` / `NON_MONOTONIC_TIME` | 时刻为负、hold 未推进时间、时刻回退 |
| `POUR_EXCEEDS_VOLUME` | 倒出体积超过当前液体体积 |
| `EMPTY_VESSEL_OPERATION` | 容器为空仍 pour/hold |
| `STALE_STATE_REFERENCE` | **清洗后仍对旧状态做 pour/dilute/hold** |
| `INVALID_ALLOWANCE` / `DUPLICATE_ALLOWANCE` | 接触时长声明非法（非正/同实体）或重复 |
| `INVALID_TOLERANCE` | **对账接口**：容差为负（有限非负才合法） |
| `INVALID_POSITION` | **对账接口**：观测位置不是初始态(0)或已提交步骤序号 |
| `DUPLICATE_OBSERVATION` | **对账接口**：同位置（双容器还须同容器）同试剂重复观测 |

> 双容器转移预检另用 `INVALID_VESSEL` / `DUPLICATE_VESSEL_ID` / `UNKNOWN_VESSEL` /
> `SAME_VESSEL` / `EMPTY_SOURCE_VESSEL` / `TRANSFER_EXCEEDS_VOLUME`，详见
> [双容器转移预检](#双容器转移预检)一节；对账接口的整单拒绝规则见
> [实测浓度对账](#实测浓度对账本轮新增)一节。

## 内置试剂 / 材质 / 规则

- 试剂 21 种：`h2so4, hcl, hno3, hf, acoh, naoh, koh, nh3, nacl, nabr, naocl, h2o2,
  kmno4, k2cr2o7, nh4cl, nh4no3, na2s, ethanol, acetone, formaldehyde, water`
- 材质 7 种：`glass, hdpe, ptfe, natural_rubber, aluminum, carbon_steel, stainless_steel`
- 规则 22 条（F01–F08 禁配、T01–T02 限时、M01–M12 材质），均以分类标签匹配，
  规则文本与版本号见 `app/rules.py` / `GET /catalog`。

## 调用示例（curl / Python）

```bash
# 安全示例：限时接触 + allowance 覆盖
curl -s http://localhost:8000/api/v1/precheck \
  -H 'Content-Type: application/json' \
  --data @examples/allowance_safe.json | python -m json.tool

# 双容器转移（禁配：盐酸从 acid-bottle 转入漂白水 bleach-tank → 氯气）
curl -s http://localhost:8000/api/v1/transfer-precheck \
  -H 'Content-Type: application/json' \
  --data @examples/transfer_forbidden.json | python -m json.tool

# 实测浓度对账（单容器；无观测时也可只传 observations: [] 取原预检结果）
curl -s http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  --data @examples/reconcile_safe.json | python -m json.tool

# 实测浓度对账（双容器，观测带 vessel_id）
curl -s http://localhost:8000/api/v1/transfer-reconcile \
  -H 'Content-Type: application/json' \
  --data @examples/transfer_reconcile_safe.json | python -m json.tool
```

```python
import requests

payload = {
    "vessel_material": "glass",
    "temperature_c": 22.0,
    "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
    "steps": [
        {"seq": 1, "op": "add", "reagent_id": "hcl",
         "concentration": 30, "volume_ml": 50, "at_seconds": 30}
    ],
    "contact_allowances": [],
}
r = requests.post("http://localhost:8000/api/v1/precheck", json=payload, timeout=10)
r.raise_for_status()
result = r.json()
if result["blocked"]:
    f = result["findings"][0]
    print(f"阻断于步骤 {f['seq']}：{f['blocking_reason']}")
    print("涉及组分浓度：", f["concentrations"])
```

双容器转移预检（Python）：

```python
import requests

payload = {
    "vessels": [
        {"vessel_id": "A", "vessel_material": "hdpe",
         "initial_liquid": [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]},
        {"vessel_id": "B", "vessel_material": "hdpe",
         "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]},
    ],
    "temperature_c": 22.0,
    "steps": [
        {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
         "volume_ml": 50, "at_seconds": 30}
    ],
    "contact_allowances": [],
}
r = requests.post("http://localhost:8000/api/v1/transfer-precheck", json=payload, timeout=10)
r.raise_for_status()
result = r.json()
if result["blocked"]:
    f = result["findings"][0]
    print(f"阻断于容器 {f['vessel_id']} 步骤 {f['seq']}：{f['blocking_reason']}")
else:
    for v in result["final_states"]:
        print(v["vessel_id"], v["liquid_volume_ml"], "mL")
```

实测浓度对账（单容器，Python）：

```python
import requests

original_request = {
    "vessel_material": "hdpe",
    "temperature_c": 22.0,
    "initial_liquid": [{"reagent_id": "nacl", "concentration": 10.0, "volume_ml": 100.0}],
    "steps": [
        {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 20.0, "volume_ml": 100.0},
        {"seq": 2, "op": "dilute", "volume_ml": 100.0, "at_seconds": 10},
    ],
    "contact_allowances": [],
}
payload = {
    "request": original_request,
    "tolerance_percentage_points": 0.2,   # 统一容差（百分点，有限非负）
    "observations": [
        {"position": 0, "reagent_id": "nacl", "measured_concentration": 10.05},
        {"position": 1, "reagent_id": "nacl", "measured_concentration": 15.10},
        {"position": 2, "reagent_id": "nacl", "measured_concentration": 9.90},
        {"position": 2, "reagent_id": "koh",  "measured_concentration": 0.0},  # 该位置未出现→按 0 计
    ],
}
r = requests.post("http://localhost:8000/api/v1/reconcile", json=payload, timeout=10)
r.raise_for_status()
result = r.json()
print("原预检是否阻断：", result["precheck"]["blocked"])
for o in result["observations"]:
    flag = "OK " if o["within_tolerance"] else "OUT"
    print(flag, f"位置{o['position']}", o["reagent_id"],
          f"实测{o['measured_concentration']} vs 预测{o['predicted_concentration']:.6f}",
          f"偏差{o['deviation']:+.6f}")
if result["first_violation"]:
    fv = result["first_violation"]
    print("最早超差：位置", fv["position"], fv["reagent_id"], "偏差", fv["abs_deviation"])
```

双容器实测浓度对账（Python）：观测多带一个 `vessel_id`，端点换为
`/api/v1/transfer-reconcile`，响应的 `precheck` 与双容器预检响应一致。

也可直接查看 [`examples/`](examples/) 下的请求/响应样例
（含 `reconcile_*` 与 `transfer_reconcile_*` 共六个对账样例）。

## 测试

```bash
pip install pytest httpx
python3 -m pytest -q
```

覆盖：禁配识别与估算浓度、初始液体触发（seq=0）、浓度阈值与稀释、超时接触与 allowance
覆盖、倒空/稀释对接触片段的重置、HF/玻璃等材质规则、清洗后旧状态引用拒绝、全部输入拒绝码、
乱序 seq 的稳定处理（单容器 35 个用例）；双容器转移另覆盖：比例扣减/计入簿记、源转空结束片段、
转入侧新片段计时、既有片段跨转移延续、材质/禁配在转入侧触发、初始状态两侧检查、
同刻按 seq 执行，以及全部转移拒绝码（`tests/test_transfer_api.py`，39 个用例）。
实测对账另覆盖：未四舍五入值参与比较、未出现试剂按零计、容差边界等值通过、
最早超差按执行顺序+提交顺序定位、无观测仅返预检、有风险流程仍可对账、
未知步骤/容器/试剂、重复观测、浓度越界、非法容差、布尔/NaN/±∞ 拒绝及原流程无效整单拒绝
（`tests/test_reconcile_api.py`，41 个用例）。

## 项目结构

```
app/
  catalog.py            # 试剂 / 材质目录与标签
  rules.py              # 相容性规则库（禁配 / 限时 / 材质）与版本
  models.py             # 请求响应 Pydantic 模型（预检 + 转移 + 对账）
  errors.py             # 错误码
  validator.py          # 单容器逐项校验 + 归一化执行计划
  transfer_validator.py # 双容器逐项校验（容器/转移/超量/空源/时序）+ 执行计划
  runtime.py            # 单只容器的状态机：浓度簿记 + 接触片段对账与超时
  engine.py             # 单容器预检：重放计划、事件转 findings、快照
  transfer_engine.py    # 双容器预检：两侧 runtime + transfer/hold 重放
  reconcile.py          # 实测浓度对账：未舍入浓度重放、观测校验、偏差与最早超差
  main.py               # FastAPI 路由与异常映射
examples/                 # curl 可用的请求/响应样例（含 transfer_* 与 reconcile_*）
tests/test_api.py
tests/test_transfer_api.py
tests/test_reconcile_api.py
Dockerfile
docker-compose.yml
```
