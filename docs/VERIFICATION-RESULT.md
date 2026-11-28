# 工业核验结果契约

版本为 `ira.industrial-verification/v1`。领域结构位于 `ira_native.domain.verification`，复用候选归一值、字段来源、确定性规则和人工核验快照。十进制用字符串编码，避免 JSON 浮点舍入。

## 两种读取方式

| 接口 | 输出和权限 |
| --- | --- |
| `VerificationWorkflow.observe_task(sources=..., declarations=...)` | 运行中的授权任务视图。以解析运行 ID 映射 1 起算的物理页集合，以清单导入 ID 映射实际原始行号集合。支持多份本地已登记资料。返回 scoped 原始证据、要求、清单行、候选边、类型化事实和机器条件化结果。观察 ID 用于跟踪一次执行。该调用不写人审记录或持久化正式核验。 |
| `VerificationWorkflow.domain_result(run_id)` | 按已有核验运行读取版本化结果，沿用源字节、注册修订及人审快照检查。工作台报告的本机明细中可下载 `/workbench/runs/{run_id}/verification.json`。该明细有字段原值，仅供获授权本机使用。原汇总报告保留原白名单。 |

授权页和行号在 SQLite 查询中筛选。读取到的记录还须与解析运行、原始资产、解析产物和页范围一致。该接口由调用者提供已授权的视图，不能用它给 Agent 新授权。多份资料中的同主体异值或异版保留各自来源，缺少生效、适用范围和替代依据时停止条件化计算。

## 关键字段

- `facts[]` 保存要求或声明的主体、型号、部件、属性以及源资产、页、表格数据行与证据引用。每个字段的 `raw`、`normalized`、`state` 和 `sources` 独立。声明来源保留工作表原始行列。正文保留可支持的原文字符区间。续表子项另带已定位的表头来源。
- 取值状态包括 `present`、`not_stated`、`explicit_absence`、`not_applicable`、`unconstrained`、`unknown`、`parse_failed`。空值需结合状态解释。显式否定或字面“不适用”须有可引用的原文。范围和上下界见 `measurement`，`unit` 是现有归一化的工程单位。
- `findings[].association` 记录机器计算关联和阻断原因。机器关联来自同一个注册来源的可追溯正文或表格字段与明确映射的声明行。只看候选数量不能证明关联。低风险题注仅分类为可见标签，未把它提升为已确认归属。未审查脚注、适用限制和不明表头保留阻断。
- `conditional.status` 是假设下的规则结果。`comparison_state` 区分已比较、不适用、不可比、尚待解决。`formal_status`、`formal_disposition` 和 `human_confirmation` 分别表示正式状态、处置和人审状态。原始来源或人审快照失效时，当前正式状态回到待定，历史处置保留。
- `source_versions` 保留已登记版本事实。版本号或载入顺序本身不能建立有效替代关系。本版不会自动选择有争议的修订。
- `blockers` 供工作台提示下一步核对事项。EVAL 专用字段和评分规则由独立适配器处理。

## 简例

下列片段显示已找到条件化差异但等待人工核验的状态。完整 JSON 由工作台本机明细或本地工作流导出。

```json
{
  "schema_version": "ira.industrial-verification/v1",
  "status": "observed",
  "findings": [{
    "binding_state": "machine_candidate",
    "conditional": {
      "status": "violated",
      "reason_code": "CONDITIONAL_VALUE_DEVIATION",
      "comparison_state": "compared"
    },
    "formal_status": "undetermined",
    "formal_disposition": "review_required",
    "human_confirmation": {"required": true, "state": "not_performed"}
  }]
}
```
