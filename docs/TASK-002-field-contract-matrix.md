# TASK-002A｜字段契约矩阵

以 [CONTRACTS §2–4](CONTRACTS.md) 和冻结提交为准。这里描述生产字段与差距。DATA-R1 的 `field_role`、`field_support.state` 和 `match_label` 仅用于评测标签。

## 对象与来源

| 正式对象 | 实际实现与类型 | 原始值、规范值及来源 | 当前缺口和测试 |
|---|---|---|---|
| RequirementCandidate | `application/candidates.py::extract_requirements` 输出 dict，Workflow 加 `candidate_id: str`；含 `property`、`expr`、`strength`、`condition`、`scope` 等 | `raw: dict` 存扫描字段，规范值由 `normalize` 提供；Evidence ID、物理页、JSON Pointer、块内行是定位基础。缺字段通常为 `None` 或 `unknown`。 | 缺正式强类型候选边界；诊断的缺失或不支持原因未逐字段传给候选。现有 `test_explicit_records_remain_candidates`、`test_unsupported_table_shapes_report_why_candidate_rows_were_not_scanned` 覆盖识别边界。 |
| DeclarationItem | `application/workflow.py::_register_sheet` 中的 dict；`item_id`、`source_asset_id`、`sheet_or_section`、`row_id`、`raw`、`original_cells`、`field_evidence` | 原始字节资产、列映射和物理行可定位；规范表达式来自同一 `normalize`。工作表名可空（CSV）；公式/错误保留风险，缓存只记可用性。 | 缺独立的多属性声明结构及精确的类型、未知状态；XLSX 多表/缓存/导出未独立端到端验收。现有 `test_csv_multiline_raw_mapping_and_duplicate_rows`、`test_xlsx_formula_kept_and_not_executed`。 |
| FieldEvidence | 要求侧 dict 含 `field_name/evidence_id/raw_span/raw_heading/row_in_block/support_state`；清单侧含列名、列号、工作表、行、资产及缓存标志 | 表字段可回查父块与块内行，清单字段可回查行列。定位层、字段忠实度、适用性分别判断；不存在可靠 cell bbox 时保留页/块。 | 缺跨块派生的证据链及可信字段升级入口。原文与规范值、字段状态及派生步骤需成对保存。现有 `test_annotation_contradiction_stays_a_risk_through_draft_verify_compare` 和 `test_independent_registry_and_native_provenance`。 |
| EntityBinding | R2 `domain/candidate_matching.py::assess/generate_candidates` 为草案产生候选边，附逐维状态、关系拓扑、未决、绑定资格和比较阻断 | 类别、显式身份配置、范围及站点冲突阻断召回；属性用于字段配对，型号冲突单列。不存在已证明的范围覆盖关系时不推断覆盖。 | 可信实体确认与范围关系待实现。人工选择只作自述，系统确认恒为 false。见 `test_task002a_r2_candidates.py` 与既有信任门回归。 |

## 业务字段、角色与验证缺口

以下规范值描述当前代码；用「待核验」标出的量须进入规则比较，不能当作实体身份排除条件。并非每个产品标签都能从正文自动派生。

| 契约字段 / 角色 | 运行时代码与类型 | 原始与规范值依据、未知及支持 | 待实现 / 测试 |
|---|---|---|---|
| 设备类别 · 身份 | `equipment_class: Optional[str]` | 显式表/条文或已映射清单列，裁剪空白；要求提取只接收 OLT/ONU。 | 中文类别别名或缺失待审核；已有类别拒绝 Gold。 |
| 厂家型号 · 条件式身份 | `raw.manufacturer_model → model: Optional[str]` | 显式字段，空值为 `None`，破折号等未获统一语义。左侧给值即纳入相等匹配。 | 区分左侧未约束、未知、固定型号/兼容性，保留异型号同槽位的业务可能；SYN-011/012 是纯逻辑。 |
| 配置变体 · 身份 | `raw.configuration_variant → config_variant: Optional[str]` | 使用源标签，作为严格配对键。独立的 `configuration: dict[str,int]` 不在配对键。 | 明确原生配置、类型和身份维度的关系，不能把核验端口数变化作为身份变更。SYN-024 纯逻辑及合成配置拒绝用例。 |
| 需求槽位 · 身份 | 无独立字段 | 只能从当前范围/变体组合候选近似查找。 | 定义独立槽位与可审查依据、同槽位不同型号和一对多用例。 |
| 接口配置与接口数量 · 可含身份描述及待核验量 | `raw.config`、`configuration`、`property=interface_config` 的 `expr: Optional[str]` | 有限 `数字 x 类型` 多集合；未知语法为空结构，诊断仍保留。接口计数与设备数量须分别表达。 | 逐接口证据、派生轨迹及 O/0 含义待审；端口阈值与多集合当前有纯逻辑回归。 |
| 设备数量 · 待核验 | `raw.quantity`，`property=device_quantity`，`expr: Optional[str]` | 目前共享默认属性与表达式。明确的数量角色下要求可派生比较符，声明默认生成等值表达式。无独立类型限制阻止已映射技术值占用数量表达式。 | 单独设备数量/端口/技术属性，证据和单位互不窜用。SYN-001/002/003 与 DATA-R1 反例。 |
| 数值、比较符和单位 · 待核验 | `expr, unit: Optional[str]`，原值在 `raw.quantity/expr/unit` | 表头与列逐字段保留。规则层 `Term` / Decimal／Pint 负责有限计算。提取未独立输出操作符、量值及各自来源。 | 精确分解且保留原始字形与单位，SYN-007/018/022 的规则测试不能代替提取测试。 |
| 工程范围与站点组 · 身份/适用性 | `scope, site_group: Optional[str]` | 显式表格或清单值，裁剪后比较；正文、注释不反填。 | 单独表达公司/标段/站点/合同计价/代建、报价排除及覆盖关系。SYN-005/025 和注释冲突工作流。 |
| 计数口径 · 可比性 | `count_basis: str` | 原文本保留；`per_scope` 映射 `per_contract_scope`，未给用 `unknown`，其余原字符串可透传。 | 格式扩展需经契约确认。逐项证明聚合关系和范围完整性，再计算。SYN-006/022/023。 |
| 适用条件 · 适用性 | `condition: Optional[str]` | 显式字段；空与未叙述均可能呈 `None`。 | 区分明确无附加条件与未提到、条件冲突、备注关联。SYN-013 为纯逻辑。 |
| 要求效力及数量角色 · 适用性 | `strength, quantity_role, inclusion_status: str` | 识别显式效力别名，未给为 `unknown`；不凭关键字或脚注自动覆盖。 | 支持条文、表注和章节候选效力并保存优先关系证据。已有注释保守回归；多重效力的独立 Gold 待接入。 |
| 文档与版本关系 · 可比性 | `source_revision: Optional[str]`、`DocumentRevision`、自述 `same_version` | 源字面字段不同于受审版本关系。独立版本制度可在证据服务登记；Workflow 的自述无法升级字段确认。 | 版本适用和补遗的可审核引用边界，禁止默认覆盖。SYN-015/016 仅在规则层给前提。 |

完整的字段忠实度、实体核验记录、适用性和审查身份均须独立扩展。当前工作流不因 DATA-R1 的条件化规则结果生成可信 `issue/no_issue`。

## R2 字段投影

上表部分字段的初始状态源自阶段 A 基线。R2 的实际值投影为 `field_values` 中的 `raw/normalized/state/semantic_basis`。它独立于实施状态 `field_implementation` 和证据支持状态 `field_evidence[].support_state`。原始字段与来源引用均保留，要求与声明之间不反填。

- 仅受控字段的整格占位符可视为未提供或未知；型号内部连字符保留。要求侧空白站点为 `not_stated`，只有原字段明说「不适用」或「不限定」才产生相应状态。缺量保持未观测，不填 0。
- 计数口径在明确规范码、已有别名或末尾括号内显式规范码时规范化。版本关系、数量角色及适用状态的权威语义仍未实现，已有字段原值不删除。
- 声明提供复合 `declaration_fields`。设备数须有明确计数单位，接口配置提供逐类型单台端口量，技术属性要求明确字段角色、单值及单位。派生端口回指接口配置原字段，缺目标时不跨字段比较。
- `requirement_slot` 可保留显式源字段，缺失时不推定同槽位。数值型变体只在已知同类型结构且差异位于目标接口属性内时保留为待核验差异。不同具体范围超出当前已证覆盖关系，T2A-025 因此不会被判为可召回同槽位。标签与 Gold 的差异留在开发回归报告。
