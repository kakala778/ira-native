# 完全合成的核验逻辑种子（v0.2）

**重要：本目录不是已验证的铁路参数数据集，也不是 PDF 解析准确率测试。** 所有 `SYN-*`、虚构场景、数值、配置和虚构型号均为人工设计；与真实 TX-03 的数量、站点、型号、表格内容无映射关系。不得用这里的通过率宣称工业场景可靠。

- `cases.jsonl`：26 条 `test_scope=logic_only` 案例；每条由 `requirement`、`declaration`（可能为空或充当另一要求）、`preconditions`、`expected`、`gold_status` 构成。结果域严格区分 `requirement_evaluation` 与 `constraint_relation`；`gold_status=draft_for_human_review` 表示还没有完成专业金标审核。
- 这些案例**假设**其逻辑前提中的字段来源正确，并不真正提供 PDF、来源 Hash、bbox 或文件绑定证明；不用于 G1/G2/G3 通过率。
- CSV 两个文件是完整独立的极简虚构清单，用于未来导入与匹配演示；它们**不与 26 条 cases.jsonl 一一对应**，不能混算综合准确率。
- 首个正式端到端验收仍需由 TASK-001/TASK-002 创建对应的**合成 PDF + 解析格式 JSON + 清单 + 字段级参考答案**，并进行人工核对；不能从 JSON 文件名猜来源。
- `SYN-011` 明确假设“同需求槽位且强制固定厂家型号”，否则相同类别、不同型号并不天然构成冲突。
- `SYN-023` 明确假设存在被确认完整的记录范围与必须记录要求，否则 `no_evidence_found` 不能升级为“确认缺失”。
- `SYN-026` 是 **要求↔要求**，必须输出 `compatible/right_stricter`，而不是 `satisfied`。

运行测试之前须复核 `gold_status` 并记录数据版本、规则版本、测试命令、失败案例及审阅人；避免使用同一个 LLM 自动产生全部 Gold 再自评。
