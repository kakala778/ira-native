# TASK-002A 评测输入、评分及复现

本评测只使用经授权的合成开发数据。DATA-R1 的 50 案及派生变形与旧 26 条逻辑种子分别报告。它们已用于研发回归；独立集须使用后续独立编写、核对的来源和答案。此轮没有独立样本验收结果。

## 接入协议

- DATA-R1 路径显式传入。读取 `cases.jsonl`、`gold.jsonl`、`metamorphic-cases.jsonl` 和派生 JSONL；按 `case_id` / `meta_id` 唯一配对。源层标识 `synthetic_public`。
- `materialize(case)` 只用源段落、表列、源行、页与清单表头。生成真实合成 PDF、手写格式模拟 JSON、实际 CSV/XLSX 字节以及来源坐标侧表。表格按源列保留，不补表头或字段。CSV/XLSX 按显式测试映射读入。来源坐标侧表从源编号生成，不匹配输出的业务值，不为字段创建虚假单元格框。
- 测试负责人可直接构造 `Inputs`，供 `observe(inputs, database)` 使用。字段包括 PDF/解析 JSON/清单字节、清单格式与用户映射、工作表、原生 Pointer + 块内行到来源锚点、生成清单物理行到来源锚点、原始表头的列号。独立 E2E 可替换真实格式的合成文件与独立标注侧表，继续用 `score(observation, gold, columns)`，而不依赖 DATA-R1 的生成方法。独立 Gold 需明确是否可比、适用范围、期望证据及无法裁决的原因。
- 输入与 Gold 分开。输出实际要求/清单以来源锚点配对，Gold 对象通过作者给出的设备类别字段定位选锚，缺唯一锚时显式未配对。不能用被测提取值、排序或同名实体当作身份对齐依据。
- PDF 来源锚为来源 ID、物理页、表 ID、原行 ID，或来源 ID、物理页、段标识。清单锚为来源 ID、工作表、源行 ID。变形时源行 ID 与生成文件的新物理行分开记录。几何定位仅验证页/父块回查，未评估单元格 bbox。

## 分层评分

| 层 | 口径 |
|---|---|
| 物料和对象 | 所有基础案例报告最先阻断的阶段及原因。对象按作者锚点统计期望、命中、缺失、意外。上传的字节样本实际进入 EvidenceService 与 VerificationWorkflow。 |
| 字段 | 仅契约矩阵与 `FIELDS` 显式映射列纳入该轮字段矩阵。期望值从独立 Gold 读取，严格相等，无模糊归一。未实现字段仍记入总分母并单列。没有观测到对象时计 `not_observed`。 |
| 直接字段证据 | 每条 Gold 引文是一个单位，核对原值、字段、表头/列、源对象、原生块/工作表和行。找不到支持映射或缺例为 `unsupported`，缺对象为 `not_observed`。字段派生与上下文 G3 核验不借该分数认证。 |
| 候选集合 | 运行现有 `_related`，按 Gold 的候选同槽位及两类歧义标签核对集合。独立记录要求↔要求关系不支持、无法锚定边和意外边。候选歧义不等于已确认绑定。 |
| 歧义、未知与信任 | 一对多样本检查是否真的保留多个候选，其他未知按 Gold 明示未知的映射字段核对。Workflow 实际运行，行得通时勾选最强自述，验收 `not_verified`、实体未确认及没有确定处置。空结果列为 `empty`，不算通过。 |
| Conditional Evaluation | 离线用作者假定的结构化 Gold 和显式前提调用规则。仅支持单条、同明确版本、无分支的设备数量标量。有前提缺口、多个结果或非此属性均单列。仅比较满足性与处置标签，记录实际原因码。其结论不用于 Workflow Expectation 判分。 |
| 变形 | 先用作者脚本重放 15 条变换并自校验。可表达的派生输入走真实工作流。只对列/行重排运行部分锚定不变量，数值逻辑、派生字段逐条 Gold 或完整变形预期未在本轮签收。 |

`numerator` 是 `match`，`denominator` 包含各分项全部状态；另给排除结构不支持及尚未实现的 `supported_denominator`，缺对象不会从后者剔除。报告记录每案失败阶段、逐层计数及现有结构诊断。不同层、不同语义以及独立集分数不能合并为单一准确率。

## 复现

在 TASK-002 工作树执行。用实际 DATA-R1 交接路径替换变量。新输出路径应不存在。

```powershell
$set = Read-Host 'DATA-R1 交接目录'
New-Item -ItemType Directory -Force data/task-002a
uv run --locked python "$set/apply_metamorphic.py" --out data/task-002a/metamorphic-derived-cases.jsonl
uv run --locked python "$set/validate_dataset.py" --self-test --derived data/task-002a/metamorphic-derived-cases.jsonl
$env:PYTHONPATH = 'tests'
uv run --locked python -m evaluation.run_task002a --dataset $set --derived data/task-002a/metamorphic-derived-cases.jsonl --out data/task-002a/baseline-metrics.json
Remove-Item Env:PYTHONPATH
$env:IRA_RUN_LIVE_AGENT_TESTS = '0'
uv run --locked pytest -q -rs --tb=short
```

评测不改输入数据。只记录输入文件摘要、合成计数及诊断。临时库创建于系统临时目录并随运行释放。此路径无模型调用。

## R2 口径

R2 读取 Workflow 的真实 `candidate_edges`，按参考明确边与歧义边独立记录召回、误边、缺边、标签差异、端点未观测和一对多完整性。标签允许与未审核 Gold 有据可查地不同。字段同时给出 `full_contract`、`implemented`、`observed_and_implemented`。值状态、未实现、未观测与不匹配不会合并。

`safe_review_state` 和 `expected_stage_match` 分开。后者要求预期要求对象有自己的复核结果；候选阶段还必须匹配该对象的一条参考正边，空结果或无关 Finding 不能计正例。未覆盖的续表无候选和要求间关系阶段单列为不可比。历史宽松阶段分数不再使用。原始 413a051 输出可用 `--before` 重算 R2 分母，原数据保存在输出的 `original_measurement` 内。

所有 R2 指标带 `measurement_status=development_regression_not_acceptance` 和 `gold_review_state=unreviewed`。
