# IRA Native｜核心数据与核验契约

> **v0.2 严格复核稿｜2026-11-23｜已合入正式仓库、未实现**。本文件约束语义，不是已发布的 API 或 ORM 设计。以 `PRD.md` 的产品边界为前提；修改本文件中的状态含义、主键语义或比较规则，须同步更新测试参考答案。

## 1. 最大修正：严格区分三种“比较”

1. **ConstraintRelation（要求↔要求）**：两份技术约束在同一对象、属性、范围、工况和有效版本下，是否 `equivalent / compatible / contradictory / not_comparable / unknown`；还应输出方向（`left_stricter / right_stricter / same / unknown`），不能把相容误当满足。
2. **RequirementEvaluation（要求↔声明值/实际记录）**：一条已确认适用的技术要求与一条*声明值*或测量值之间，是否 `satisfied / violated / not_applicable / undetermined`。一份供应/采购清单只是 **declared**，不是现场实测。此处 `satisfied` 表示“提供的声明在这条规则下满足要求”，绝不代表工程验收合格或设备真实符合。
3. **FindingDisposition（给审核人的处置）**：`no_issue / issue / informational / review_required / blocked`。它由证据质量、要求效力、适用性、上述比较结果和来源角色共同计算，不能让 LLM 直接填充。

**禁止用一个 `relation=compatible` 同时表达“≥8 和 ≥10 相容”以及“≥8 被声明值 9 满足”。** 对于 `min 8` + `declared 9`，正确结果是 `evaluation=satisfied`；对于 `min 8` + `min 10`，则是 `constraint_relation=compatible, right_stricter`。

## 2. 来源对象及主键

| 对象 | 必需概念 | 严格约束 |
|---|---|---|
| `SourceAsset` | `asset_id`, `sha256`, `media_type`, `source_kind`, `access_policy`, `locator_internal` | PDF、CSV、XLSX 均为字节资产；`locator_internal` 不能发给 Agent/导出 |
| `Document` | `document_id`, `logical_title?` | 逻辑作品，不能单凭文件名自动归并 |
| `DocumentRevision` | `revision_id`, `document_id`, `asset_id`, `revision_label?`, `effective_from?`, `scope?`, `relation_evidence[]` | 修订、补遗、适用优先级都是**独立显式关系**；缺失=未知 |
| `ParseRun` | `parse_run_id`, `source_asset_id`, `artifact_asset_id`, `adapter_version`, `parser_version?`, `parser_config?`, `binding_state`, `binding_basis` | 解析次数 ≠ 文档修订。相同文件+产物+适配版本幂等；不同解析产物不得覆盖旧证据 |
| `Evidence` | `evidence_id`, `parse_run_id`, `source_pointer`, `page_index?`, `raw_content`, `kind`, `native_bbox?`, `coord_system?`, `precision`, `risk` | PDF 证据引用 `page_index`，清单行则是 `sheet!row` / CSV 行；不伪造单元格 bbox |
| `FieldEvidence` | `field_name`, `evidence_id`, `raw_span?`, `support_state` | `value/op/unit/subject/scope/strength` **逐字段**对应来源；块级证据不能自动证明每一列正确 |
| `RequirementCandidate` | `candidate_id`, `subject`, `attribute`, `expression`, `strength`, `condition`, `scope`, `field_evidence[]` | 解析器/LLM 输出仅是候选，不能自行认证 |
| `DeclarationItem` | `item_id`, `source_asset_id`, `sheet_or_section`, `row_id`, `equipment_type`, `manufacturer_model?`, `configuration`, `quantity`, `scope`, `evidence_refs[]` | 清单“申报条目”不是实测值；公式单元格/空行保留异常 |
| `EntityBinding` | `left_ref`, `right_ref`, `match_level`, `confidence_basis`, `reviewed_by?` | 类别、配置变体、厂家型号、工程位置、需求槽位分别判定 |
| `VerificationRun` | `run_id`, `source_hashes[]`, `ruleset_version`, `applicability_snapshot`, `result_ids[]` | 不覆盖旧运行；输入修订后旧结论需标记过期 |
| `EvaluationRecord` | `evaluation_id`, `run_id`, `kind`, `preconditions`, `computed_result`, `rule_id`, `evidence_refs[]`, `reason_codes[]` | 完整保存两侧来源、规则、门槛与未知项 |
| `Finding` | `finding_id`, `evaluation_id`, `disposition`, `summary`, `review_state` | 仅为辅助核验意见，不能等同正式验收 |
| `ReviewRecord` | `review_id`, `finding_id`, `reviewer`, `decision`, `scope`, `source_snapshot`, `time` | 明确人工操作追加，不能由模型或工具伪造，审查不会篡改原来源 |
| `EvidenceGap` / `TaskEvent` | 缺口类别、受影响对象、尝试次数、停止原因 / 调用事件、哈希、费用与状态 | Agent 自主补查的边界与可审计运行记录 |

**绑定状态分层**：`declared`（用户声称 PDF↔JSON 属于一组）、`manifest_attested`（可信导入流水线记录原 PDF SHA 与产物 SHA）、`spot_checked`（局部原文人工/程序复核）、`mismatch`（已发现不符）、`unknown`。`spot_checked` 只证明已核查片段，不能宣布整个文档正确；历史 JSON 没有原件 SHA 时，不得只凭文件名/页数授予 `manifest_attested`。

`Evidence ID` 推荐用不可变 `parse_run_id + native pointer + adapter version` 的确定性哈希；页面/块序号可以参与 ID，但**不能宣称不同解析产物间 ID 稳定**。文档、解析、位置、字段的核验状态需要独立记录。

## 3. GPON 领域对象：型号、配置、口径和适用性

- `equipment_class`：OLT/ONU 是设备**类别**，不自动等于厂家型号、料号或唯一设备实体。
- `manufacturer_model`：明确厂商/型号时才填写；文件只有“ONU”时保留 `null`，不据此判“缺型号”或“不符型号”。
- `configuration_variant`：例如 ONU 不同 POTS 数、不同接口组合，必须是不同变体；至少 `equipment_class + configuration + project_scope + site_group` 参与候选匹配。同一类别不得跨变体聚合。
- `requirement_slot`：对某范围要求交付的“设备槽位”，不同厂家型号仍可满足同一槽位，除非**条款明确要求**固定型号、兼容性或既有设备型号一致。
- `count_basis`：`per_device / per_site / per_site_group / per_contract_scope / total / unknown`；`unit_original` 保留 `端/套/台/块/个` 等原字。`端` 与 `台` 不能默认互换，数量比较必须有经定义且可审计的计数口径变换。
- `site_or_scope`：公司/标段/代建/枢纽/站点/合同计价范围须分别记录；同一设备在报价范围内、参考范围内、代建范围内，不得直接累加。
- `requirement_strength`：`mandatory / reference / informative / unknown`；同时增加 `quantity_role`（`minimum / exact / estimate / quotation_only / unknown`）及 `inclusion_status`（`in_scope / reference_out_of_scope / excluded / unknown`）。数量参考性不能简单从章节标题推定，必须追溯适用的注释和总则。
- `constraint_kind`：`eq/ge/gt/le/lt/range/enum/configuration/conditional/presence/unknown`；数值用字符串序列化的 `Decimal`；`Pint` 只用于已知物理量和单位白名单。`GE(O)`、`GE(0)` 类易混字符必须保留原文和异常状态，不能未经核对擅自更正。
- `value_role`：`requirement / declaration / observed / another_requirement`，由来源文档类型和字段定义确定。**不能把供应清单的声明值视为验收实测值**。

## 4. 四层质量门与判定顺序

| Gate | 查什么 | 不通过如何处理 |
|---|---|---|
| G1 来源/权限 | 字节哈希、资产对、授权、是否允许外传、是否有来源错配 | 错配/权限拒绝阻断；历史配对未认证仅候选，不授予确定来源支持 |
| G2 定位 | PDF 物理页 (`page_index+1`)、坐标类型、PageBox/旋转、表格续接和可解引用；CSV/XLSX 行号 | 越界拒绝；框不可信降至页级；无单元格坐标不得伪造 |
| G3 字段忠实度 | 字段是否确实属于正确表头/行/设备/单位/条件/脚注；原文字形混淆 | 有风险标记 `needs_review`，禁止未经证实的确定性违规结论 |
| G4 业务可比性 | 设备槽位/型号/配置/站点/计数口径/版本效力/要求等级/角色 | 明确不适用 → `not_applicable`；未知 → `undetermined`，不能自动宣布违规 |

判定顺序：来源及权限 → 字段来源 → 文档/版本效力 → 设备与配置绑定 → 范围及数量口径 → 要求等级及值角色 → 规则计算 → 证据支持检查 → Finding → 人审。

**来源不等于可信**：PDF 本身有同一文档印刷页码与物理页码差异；只有 block bbox 的解析结果不能推出单元格级证据；PDF 与解析 JSON 同页数不保证相互对应。

## 5. 状态域与原则性实例

- `source_state = verified_for_checked_fields | unverified | mismatch | denied`：与 `binding_state` 不同；本状态须有明确核查方法和范围。
- `extraction_state = candidate | supported_in_checked_scope | disputed | insufficient | human_reviewed`。人审不等于行业认可。
- `applicability = applicable | not_applicable | unknown`。
- `requirement_evaluation = satisfied | violated | not_applicable | undetermined`。`satisfied` 特指声明/记录满足已确认的该项约束，不等同交付物真实符合。
- `constraint_relation = equivalent | compatible | contradictory | not_comparable | unknown`；`strictness = left_stricter | right_stricter | same | unknown`。
- `finding_disposition = no_issue | issue | informational | review_required | blocked`。
- `review_state = unreviewed | accepted_with_scope | rejected_with_scope | needs_more_evidence`。

逻辑例子（**全部为抽象样例，不属于真实铁路标准**）：

| 左侧/要求 | 右侧/声明 | 正确的判断 |
|---|---|---|
| `>=8`（强制、按设备） | `=9`（同设备同配置声明） | `satisfied`，仅代表这条声明满足要求 |
| `>=8`（强制） | `=7`（可比且来源确证） | `violated`，可产生待人工核实的 `issue` |
| `>=8`（要求A） | `>=10`（要求B） | `compatible` + `right_stricter`，**不使用** `satisfied` |
| 参考数量 `=6` | 清单 `=4` | `informational`；不输出工程违规 |
| 类别同为 ONU，配置不同 | 数量相同 | 不能自动认定同一设备项；待复核 |
| 原件只写 ONU，无厂家型号 | 申报清单写具体型号 | 可建立类别/配置候选映射，不因为“原件无型号”直接判违规 |
| 要求 4 `端` | 清单 4 `台` | 单位/计数含义未知时 `undetermined`，不能擅自 1:1 |
| 部分文件未检索到某条 | 清单缺此行 | `no_evidence_found`，不是“已确认工程漏项” |

`source_mismatch`、`permission_denied`、`parse_failed`、`invalid_location`、`model_binding_unknown`、`unit_semantics_unknown`、`scope_mismatch`、`revision_unknown`、`requirement_strength_unknown`、`unsupported_expression`、`no_evidence_found`、`agent_budget_exhausted`、`model_disabled` 为建议机器错误码；API 需稳定映射，严禁只返回解释文字。

## 6. 权限与 Agent 结果边界

Agent 不拥有 `source_state` 的认证权、不更改事实哈希、不生成 `human_reviewed`，也不能直接提交 `FindingDisposition=issue`。模型可以提出 `mapping_proposal` 或选受控只读工具补查，随后由固定验证服务计算新结果。

受限来源的外传控制应在**进入模型请求之前**发生，包括提示词、检索原文、工具返回、错误日志、Trace 与模型供应商回退。默认无外传；“本地 PDF Viewer 可打开”与“云端 Agent 可读取”是两种独立授权。

## 7. 测试与金标范围

`tests/fixtures/synthetic/cases.jsonl` 为**人工编写的逻辑种子**，不自带原始 PDF 证明。它们测试已经明确设定前提下的决策逻辑，**不得**据此声称来源定位、OCR 或跨文件提取通过。结构包含 `requirement`、`declaration`、`preconditions`、`expected`，且应显式标注 `test_scope=logic_only`。

正式 E2E Gold 需要另外提供合成 PDF、真正对应的解析产物/可核查转换记录、合成 CSV/XLSX、字段位置与人工复核结论。模型 A/B 至少比较：B0 直接 LLM、B1 同输入的固定抽取+规则、B2 B1 + Agent 补查；同等授权、工具可用范围和预算约束，独立统计合成、授权内部和公开来源，不混算准确率。

## 8. 版本治理

合同范围/注释可覆盖表格项的效力，必须绑定**来源范围**；修订/补遗只有在明确适用范围和优先关系被登记/审核时才自动覆盖旧版。新来源、规则版本或证据复核结果改变时，旧 `EvaluationRecord` 保留，标记 `stale`，重新运行得新结果。即使没有补遗文件，条款效力也不能只由通用句式“应”推断。
