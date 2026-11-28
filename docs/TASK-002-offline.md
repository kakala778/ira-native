# TASK-002 离线输入与复验

在 `codex/task-002` 工作树中执行。已有 PDF、MinerU 4.2 格式解析 JSON 和 CSV/XLSX 均由本机读取。命令无模型调用。请将示例变量设置为获授权的本机输入及独立输出目录。

```powershell
uv sync --locked --python 3.12
uv run --locked ira verify offline --pdf $pdf --parsed $parsed --spreadsheet $list `
  --database $db --pages '2-4' --summary $summary
```

- `--pages` 是包含端点的物理页范围，也接受单页。限制候选提取和诊断的输入页，完整的 PDF 和解析产物照常登记。
- 默认按源列名与字段 ID 一一匹配。自有表头可传 `--mapping-json $mapping`，JSON 形如 `{"equipment_class":"设备类别","configuration_variant":"配置","scope":"工程范围"}`。XLSX 多表时传 `--sheet`，CSV 可指定 `--encoding`。
- `quantity`、`unit` 可作设备数量列，`config` 提供单台接口配置。若同一行还声明技术属性，可另外映射 `property`、`technical_value`、`technical_unit`。公式和错误保留原始行列并标为未知。
- 标准输出与 `--summary` 只有统计、状态和原因码。数据库和可选 `--local-details $details` 含来源和敏感内容，仅保存在授权本机目录。上述 JSON 输出文件必须是新文件。
- 可先在 Web 工作台查看映射、原始文档和候选，或用离线命令输出本机详情再查看候选。若需离线条件化计算，将人工选择写入本机 `--confirmations` 文件，输入为 `[{"requirement_id":"本机候选 ID","declaration_id":"本机行 ID","same_version":true}]`。只有满足运算前提时才产生条件化结果。条件化结果与 `Finding.disposition` 独立，实体仍未确认，字段仍为 `not_verified`。

## 结构化表格输入

正常 MinerU HTML 表要求单表、明确 `th` 表头和与表头等宽的普通行。`rowspan`、`colspan`、缺表头和未确认续表留给诊断。表头别名不替换原始表头。单表块的可选 `structured_table` 示例为

```json
{
  "header_state": "explicit",
  "headers": ["设备类别", "配置变体", "工程范围"],
  "rows": [[
    {"text": "OLT", "native_pointer": "/pages/0/blocks/3/structured_table/rows/0/0"},
    {"text": "V1", "native_pointer": "/pages/0/blocks/3/structured_table/rows/0/1"},
    {"text": "A区", "native_pointer": "/pages/0/blocks/3/structured_table/rows/0/2"}
  ]]
}
```

指针以本解析产物中的真实数组位置为准。单元格可提供原生 `bbox`、`coord_system` 供回查，UI 仍沿用 Evidence 的已认可页/块定位。派生坐标、表头声明或附件的表关联均需核对。EXP-03 已产出的 `recovered/raw` 保留原始表头和恢复来源记录，`recovered/projection` 的实验别名不进入候选。新增格式走同一个 PDF、ParseRun 和 Evidence 导入路径。

## TASK-003 接口

`VerificationWorkflow.get_draft` 提供要求、声明、`candidate_edges`、`field_pairing`、源页与字段证据。必要时使用 `verify --conditional-preview` 查看单独的 `conditional_calculation`。可信字段核验、实体认证、版本证明、范围覆盖、复杂条件与跨表归属需另行实施。新增规则输入不得将候选、映射自述或条件化运算结果升级为可信事实。
