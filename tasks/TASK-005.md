# TASK-005｜本机 GPON MVP

## 启动与操作

在当前开发工作树使用一个新的本地 SQLite 路径。浏览器访问 `http://127.0.0.1:8000`。

```powershell
uv sync --locked --group dev
$env:IRA_DB = 'D:/IRA-local/session.sqlite'
$env:IRA_ENABLE_UPLOADS = '1'
uv run --locked ira serve --port 8000
```

1. 登记原始 PDF 和已有解析 JSON，再登记设备清单。核对物理页、解析版本、来源配对状态、工作表和字段映射。可填物理页范围，比如 `2-4`。
2. 打开草案，查看候选、配对字段、原始行与未决项。手动选择对应清单行后可生成条件化计算。结果页提供返回草案和继续核验入口。
3. 可在核验事项附近启动本地 Agent。后台运行期间可返回原核验。模型不可用时仍可核对原件、执行确定性核验和人工复核。
4. 在配对页分别记录字段、来源、实体、适用性。重算后查看正式处置。首页列出近期草案及运行，历史失效结果保留。
5. 打开报告。汇总 HTML 可用浏览器打印，汇总 JSON 使用同一白名单，保留状态与证据、行列和快照引用。原始字段、审查说明和详细轨迹只在授权本机明细。报告中的引用 ID 和页号也要按项目授权分享。

本机 Agent 需要运行预装的 Ollama 并确认服务为环回、离线。可按 [TASK-004 使用说明](TASK-004.md) 显式启用。撤销许可可关闭 Agent 环境开关并重启工作台。任务不会恢复停机前的执行授权。

## 本机真实资料复验

先挑选获授权的 PDF、对应已有解析 JSON 和清单，将 `$pdf`、`$parsed`、`$sheet`、`$newDb`、`$newSummary`、`$physicalPages` 设置为本机凭据。设置独立数据库，普通协作只交经人工检查的统计摘要。复杂 Markdown、续表或脚注证据不足时保留当前拒判。

```powershell
uv run --locked ira verify offline --help
uv run --locked ira verify offline --pdf $pdf --parsed $parsed --spreadsheet $sheet `
  --database $newDb --pages $physicalPages --summary $newSummary
```

自有列名加 `--mapping-json`，XLSX 多工作表加 `--sheet`。本地详情用 `--local-details`，含原值，仅保留在授权机器上。使用工作台核查 10–20 条关键要求的来源、页码、条款适用性和清单行。应用处置是辅助核验结论，实际工程验收另行记录。

没有清单时，只做解析及要求候选诊断。

```powershell
$env:IRA_DB = $newDb
$env:IRA_ALLOWED_ROOTS = (@((Split-Path -Parent $pdf), (Split-Path -Parent $parsed)) -join [IO.Path]::PathSeparator)
$result = uv run --locked ira ingest pdf --pdf $pdf --parsed $parsed | ConvertFrom-Json
uv run --locked ira verify diagnose $result.parse_run.parse_run_id
```

诊断输出含内部 Evidence ID 与原因码。分享前检查或仅保留计数，不发送真实资料原文、解析结果、数据库、详情或敏感日志。

## 系统回放

开发回放使用合成或明确公开的输入。B0 为固定授权上下文的一次本地模型结构化请求，B1 为确定性流，B2 为 B1 加受控 Agent。三组共用任务、授权页范围与参考，Agent 证据建议不会更新人工核验。未执行人工核验的 B1/B2 正式结果按保守状态评分。B0 无正式处置端点。

```powershell
$env:PYTHONPATH = 'tests'
$env:IRA_ENABLE_LOCAL_AGENT = '1'
uv run --locked python -m evaluation.run_mvp --demo --output $newOutput
uv run --locked python -m evaluation.run_mvp --tasks $tasks --gold $gold --output $newOutput2
```

任务 JSONL 使用 `ira.mvp-task.v1`。各条提供任务 ID、目标、任务文件目录下的 PDF、解析产物、清单、物理页范围，可选工作表及列映射。独立 Gold 按任务 ID 对齐，需提供 `evidence_pointers`，可提供用于条件判断的 `semantic`，以及当前确认条件下的 `formal`。输入任务和 Gold 分开读取，参考答案只在预测产生后读入评分。输出保留实际证据范围、引用、调用、失败原因、耗时和度量。它是开发基线，独立与真实业务验收另行执行。
