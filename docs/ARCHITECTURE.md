# IRA Native｜v1.0 总体架构与工程设计

> **v0.3 严格复核稿｜2026-11-23｜设计已主动选型、尚无集成实测**。产品边界以 `PRD.md` 为准；业务枚举以 `CONTRACTS.md` 为准；项目真实状态以 `../README.md` 为准。**目标是直接建设一套正式 v1.0，而不是多次推倒重来；开发过程仍需回归测试。**

## 1. 架构决策总览

**锁定**：Python 3.12 模块化单体、FastAPI、Typer、Jinja2+PDF.js 本地审查界面、SQLite/SQLAlchemy/Alembic、FTS5+精确检索、现有 MinerU 4.2 风格解析适配、pypdfium2、Pydantic v2、Decimal、Pint（仅受控单位）、**PydanticAI 单 Agent**、pytest、uv。

**不锁定**：具体外部大模型提供商和真实工业数据对外传输权。前者受成本/可用性/私有部署条件影响；后者是授权事实，不是框架选择。无外部模型时固定核验路径必须可运行。

**复用边界**：不自研 OCR、PDF 渲染器、SQLite 检索引擎、表格读取器或 Agent 主循环。**自己研发**原件/解析/证据身份、字段级来源、设备变体/范围绑定、条款效力与适用性、核验状态/规则、人审和证据驱动补查。

**不引入**：RAGFlow/Dify 作为常驻底座、图数据库、向量数据库、Z3、微服务、多 Agent 产品、Pi 第二 Harness、MCP Server、复杂前端 SPA、通用任务持久调度。保留接入边界；不得因为“有开源”而堆栈。

## 2. 可交付系统结构

```text
SourceAsset(PDF/JSON/CSV/XLSX) + AccessPolicy + SHA256
                  │
        Ingestion / Binding / Schema Adapter
        ├─ PDF + MinerU 4.2-style → ParseRun/Evidence
        └─ Sheet/CSV → DeclarationItem + row evidence
                  │
    EvidenceStore / FTS5 / Scope-filter Retrieval
                  │
    RequirementCandidate + FieldEvidence + TableContext
                  │
 EntityBinder (class/model/variant/slot/site/group)
                  │
 ApplicabilityGate (revision/strength/scope/count-role)
                  │
   RequirementEvaluator / ConstraintComparator
        ├─ EvaluationRecord + Finding + EvidenceGap
        └─ Gap→PolicyGate→PydanticAI Agent(read-only tools)
                               → new source candidates
                               → rerun deterministic evaluator
                  │
        ReviewRecord / EventLedger / Exports
                  │
     Application Services shared by CLI/API/Web
                  │
         FastAPI/Jinja2/PDF.js / Typer
```

模块代码方向建议：`domain/` 只依赖 Python 类型及可测纯函数，`application/` 编排用例，`adapters/` 处理 PDF/MinerU/清单/数据库/LLM，`agents/` 处理 Agent Runtime 与工具策略，`interfaces/` 放 CLI/API/Web。任何端都只能通过 Application Service 写业务记录。

```text
src/ira_native/
  domain/{sources,evidence,equipment,requirements,verification,review}/
  application/{ingest,search,extract,match,verify,agent,review}/
  adapters/{mineru,pdf,spreadsheet,repository,retrieval,model}/
  agents/evidence_agent/
  interfaces/{cli,api,web}/
```

这是**模块建议**，不是要求 Codex 为每个概念创建一层空文件。实际代码以可维护、可测试为准。

## 3. 来源登记和 MinerU 适配（高风险边界）

### 3.1 原文件、修订、解析运行三者分开

- `SourceAsset` 是 PDF/CSV/XLSX/JSON 的原始字节及不可变 SHA。`Document` 是逻辑作品；`DocumentRevision` 表示正式修订/补遗/生效范围；`ParseRun` 是解析产物及运行环境。**重新解析不会产生新的文档修订。**
- 历史 `structured_content.json` 的 `metadata.producer.version` 只标识解析器版本，不能证明来源 PDF 的 SHA 或某次运行的精确命令/参数；即使原 PDF 与 JSON 都是72页也不能声称配对已认证。
- 适配器按**已观测的产物形状**做版本门槛：`pages[*].page_idx`、`blocks[*].type/content/bbox/continues_prev`、`metadata.producer`、`extensions`；保留原生 JSON 与未知字段。不能将旧 MinerU 4.2 风格 JSON 硬标记成未来标准 schema。未来其他版本必须注册新 adapter，不能静默按新版本语义解析。
- 维护 `binding_state` 与 `binding_basis`：用户声明绑定 ≠ 按导入流水线记录绑定 ≠ 部分人工/程序原文核对；后两者也不能相互替代。任何 `mismatch` 立即阻断。
- 原件存在仓库外允许目录，Web/Agent 接口仅接受已登记资产 ID，禁止任意绝对路径、URL、路径穿越和将内部文件位置写进公开报告。

### 3.2 页码与区域精度

- parser `page_idx=0` → **物理第1页**，不等于文件内印刷的章节页码。某些 PDF 前置封面/目录导致二者明显不同。
- 在现有测试产物中可见 `bbox=[x0,y0,x1,y1]` 为 0～1 归一化值；记录 `coord_system=normalized_page` 和原生坐标，不对所有 MinerU 版本泛化该协议。
- 从归一化区域变成 PDF.js 高亮须考虑真实 PageBox、旋转、裁剪和顶部/底部坐标系；先验证边界与实际文字，不能只凭四个数 0～1 就宣称定位正确。
- 当 block 是整张表格，且不存在可靠 cell bbox 时只能返回 **页级/块级**；`continues_prev=true` 是结构续表线索，不保证上一页的表头、公司范围或脚注已经关联正确。
- `GE(0)` 与 `GE(O)` 的0/O视觉混淆是适配/字段风险示例；应提供 `raw_content` 和 `needs_review`，不得简单用正则全局替换。按字段最小核对样例决定归一化规则。

TASK-001 的 PDFium 适配器使用原 PDF 的有效 PageBox，并将已知左上归一坐标转换为 PDF 原生左下坐标。仅在零旋转、无裁切、零页原点的页面上，经唯一文字匹配、区域提取、字符框与文字填充检查后返回块框。旋转、裁切或不可核查的块继续提供物理页与风险。PDFium 的调用在进程内串行执行。区块文字检查支持位置回查，字段忠实度与适用性仍由各自的质量门处理。

### 3.3 Excel、CSV 与公式安全

- 输入 CSV 支持编码检测或显式编码选择（UTF-8/GB18030）；保留原始字节与行号，避免把站点字段的顿号列表当成多个条目。
- `.xlsx` 使用 openpyxl 只读读取；公式需同时保留公式原文/缓存值可用性，**openpyxl 不计算公式**，不能自动信任旧缓存。无法确定值则 `undetermined`。
- CSV 导出若单元格文本以 `= + - @` 或控制字符开头，必须阻断表格公式注入（处理原始值与导出展示值分别记录）；避免在审阅 UI 中将原始字段渲染为可执行 HTML/Markdown。

## 4. 核验模型：比旧版增加一层必要的角色分离

**`requirement`（要求） vs `declaration`（采购/投标清单申报） vs `observation`（现场实测） vs `another_requirement`（另一份文件的约束）**不可混同。

- 规格书、备注、总则、技术变更共同决定要求效力，不能从一行“数量=4”直接推断强制交付4台。
- `OLT/ONU` 为设备类别。区分制造商型号、配置变体（不同 POTS/FE/GPON 组合）、工程需求槽位、公司/代建/标段/站点与报价范围；同类不同配置**不得**直接相加或覆盖。
- 单位除了物理量纲还有**工程计数语义**：`端`、`台`、`套`、`块` 不允许默认一一换算；`count_basis` 必须一致或有确认的聚合关系。
- 对可比较的 `requirement↔declaration` 使用 `RequirementEvaluator(satisfied/violated/not_applicable/undetermined)`；`requirement↔requirement` 使用 `ConstraintComparator(equivalent/compatible/contradictory/...)`；之后统一生成 Finding。
- 对不具有确定效力的参考数、被排除的报价范围、没有最终施工图的项目，**只能输出信息性差异或待复核**；不允许自动输出正式“违规”。
- `Decimal` 处理整数及十进制精度，`Pint` 只在明确量种、属性和可允许单位时换算。接口配置应解析为结构化多集合（如 `2 × GPON`），不强行当作带宽标量。

所有结构化候选应保留 `raw_value/raw_operator/raw_unit/raw_heading` 与独立 `FieldEvidence`。G1 来源、G2 位置、G3 内容、G4 适用性有独立状态；通过 Pydantic Schema 不能自动跳过 G3/G4。

## 5. 数据存储与检索

- SQLite + SQLAlchemy 2 + Alembic。正式资产、解析运行、Evidence、候选、核验运行、Finding、人审、Agent TaskEvent 用**不同实体或表**，加必要的唯一约束与外键。
- 一次 `VerificationRun` 记录输入资产/版本、规则版本、输出与所依赖的人审快照。来源变动或授权撤销后标记旧结果 `stale`，不是原地覆盖。
- 小数据采用字段/范围/设备硬过滤、规范化精确/子串检索，再考虑 FTS5 trigram。注意官方 FTS5 **小于3 Unicode 字符的 MATCH 无结果**，对短型号/短中文词必须有单独的精确/LIKE 路径；`detail=none` 对长 token 查询有限制。逐步测量索引大小与实际召回，不能预设“支持中文”即符合真实设备场景。
- 对结构化范围使用 SQLite 正规字段过滤，不依赖向量搜索去“猜”两个型号是不是同一设备。

## 6. Agent 已确立：PydanticAI Evidence-Guided Agent

**v1 只采用一个 Agent Runtime：PydanticAI。** 其核心价值是有类型的工具调用/输出、测试支架和调用限制，而非自动保证业务正确性。Agent 自主选择被授权的**下一项调查动作**，由领域工具提供证据；核验仍由确定性引擎负责。主流程没有可补查 Gap 或模型未授权时，不调用 Agent。

```text
Fixed verify → repairable EvidenceGap → PolicyGate → PydanticAI choose tool
            → bounded read-only tool → new Evidence/Candidate/TaskEvent
            → deterministic reverify → closed / no progress / budget / review
```

初始工具白名单：`search_evidence`（固定文档/范围）、`get_evidence`、`inspect_neighbor_context`、`get_registered_revision_relations`、`list_candidate_items`、`propose_mapping`（仅待审建议），`request_reverification`（只能调用主验证服务、追加新记录）。**禁止任意 shell、磁盘路径、外部 HTTP、SQL、PDF 修改、HumanReview 写入**。

模型外传的 `PolicyGate` 在发送任何上下文之前执行：来源是否允许外部 API、是否处于特定项目授权范围、是否含未授权邻页、工具输出大小、日志/Trace 是否会泄露原文。仅调用 `readonly` 工具**不等于无外传风险**。

预算建议默认 **6 次工具调用 / 8 次模型请求 / 单次任务 90 秒**，再配重复同一 Gap 无新证据即停止规则。这些是可配置**设计初值**，不是实际测试所得最佳配置。PydanticAI 的请求/工具 UsageLimits 可以复用，但宿主仍负责墙钟时间、强制取消、上下文授权、输出长度、失败终止与模型提供商重试上限。<https://pydantic.dev/docs/ai/core-concepts/agent/>、<https://pydantic.dev/docs/ai/core-concepts/timeouts/>。

**v1 Agent 交付不能只靠 Mock**：先用 TestModel/FunctionModel 验证权限与循环，再在**纯合成**任务上进行真实模型工具回合；如果个人环境没有可用模型，必须把“Agent 模型实测”标未完成，且不能靠固定流程冒充 Agent 已交付。

## 7. Web/API/CLI 的产品路径

- Typer CLI：`ira doctor`、`ira ingest`、`ira search`、`ira evidence show`、`ira verify`、`ira agent run`、`ira serve`、`ira export`。API/UI 共用 Application Service。
- FastAPI `/api/v1`：源资产/证据/检索/要求候选/清单映射/核验运行/人审/Agent 事件等资源；统一机器错误码。
- Jinja2+少量 JS：① 导入与范围选择；② Findings列表/要求与清单行并排展示、PDF 物理页/块定位、复核与导出。不以“通过验收”作为绿灯 UI 文案。
- PDF.js 在本地显示已授权登记 PDF；可以跳页。块高亮必须共享精确页面变换并通过真实样本校验；不可靠时只跳页，提示精度。静态资源使用固定可审查的版本，保留第三方许可说明。
- 安全：本地监听 `127.0.0.1`，用户不能通过路径参数读任意本地文件；拒绝 HTTP 路径穿越与 XSS，防 CSV 导出公式注入。

TASK-001 的 API 提供原始 PDF 下载、物理页几何数据及 PNG。路径导入由 CLI 按白名单读取，PDF 按登记 SHA 复核，原始解析 JSON 字节保存在本机 SQLite。HTTP 写入需显式启用，使用限长原字节和登记 ID。PDF.js 的视口变换属于 Web 阶段。

## 8. 技术依赖与版本/许可关口

| 组件 | 决定 | 需要 Codex 在集成时核查 |
|---|---|---|
| Python 3.12 / uv + lock | 必选 | WSL2/Windows 环境、锁文件能重建；不强迫 GPU OCR |
| MinerU 解析产物 | 使用既有风格 JSON Adapter | 记录 `metadata.producer.version`、未知字段/结构变更；**不强制安装完整 MinerU** |
| pypdfium2 / PDF.js | 后端 PDFium + 浏览器 PDF.js | 页面坐标/旋转/裁切、渲染差异、打包许可证/Notice |
| SQLite/FTS5 + SQLAlchemy/Alembic | 必选 | 目标 SQLite 编译是否含 FTS5/trigram，失效索引重建 |
| Pydantic v2 / Decimal / Pint | 必选 | 自定义工程单位语义、单位白名单、表达式拒判 |
| FastAPI / Jinja2 / Typer | 必选 | 依赖注入路径、安全静态资源、本机运行 |
| openpyxl / csv | 必选 | 公式/缓存值风险、编码、CSV 安全导出 |
| PydanticAI | **正式 Agent Runtime** | 同版本 TestModel/FunctionModel/UsageLimits API、模型接入许可证与成本；本地/远端策略 |
| pytest / Hypothesis（可选） | 必选 pytest，Hypothesis 按需 | 规则边界、单元/集成/安全/Agent 分层运行 |

许可说明：当前 MinerU 官方仓库使用带附加条件的 **MinerU Open Source License**，不是“全部纯 Apache2”；pypdfium2/PDFium 与 PDF.js 许可证较宽松，但打包应检查各自 NOTICE/第三方依赖。即使软件允许使用，也不能将受限铁路 PDF/衍生内容上传公开仓库。

官方参考：<https://opendatalab.github.io/MinerU/reference/output_files/>；<https://www.sqlite.org/fts5.html>；<https://pypdfium2.readthedocs.io/en/stable/python_api.html>；<https://github.com/mozilla/pdf.js/blob/master/LICENSE>；<https://github.com/opendatalab/MinerU>；<https://github.com/pydantic/pydantic-ai>。

## 9. 研发和发布质量门

- **TASK-001** 证据基础/解析适配/原 PDF 物理页回查/CLI/API。实现进度与复现命令见 README。
- **TASK-002** GPON 需求/配置/范围与清单导入，字段级候选来源、人工列映射。
- **TASK-003** 两类比较引擎 + 计数口径/版本/强制参考规则 + Finding。
- **TASK-004** PydanticAI EGA + 轻量 Web/PDF.js + 人审与审计事件。
- **TASK-005** 综合 E2E、三基线对照、安全/性能/错误处理及已知限制。

每阶段是**同一个正式工程中的增量集成**，不是临时 Demo。每个 TASK 的定义和验收与 PRD 的 P0 对齐；已有模型成果和基线不要重复开发。跑完测试才能更新 README“已实现”。

**发布不等于行业认证**：合成数据覆盖所声明的 P0 行为、原文证据可回查、关键未知态不会错误宣布违规、用户能审查；真实工程泛化与 Agent 净收益需要授权样本独立验证。模型平台/网络/资料授权不足时，报告“局部功能完成/该项未验证”，不可自行降低需求并宣称 v1.0 全部达成。

## 10. 关键 ADR（设计决定，不是现有实现）

- ADR-01：Python 单体 + 一套 Application Service；不引入多服务/第二套业务规则。
- ADR-02：Byte SourceAsset、DocumentRevision、ParseRun、Evidence 严格独立，保留可追踪源关系。
- ADR-03：ConstraintComparator 与 RequirementEvaluator 分离，Finding 是第三层业务处置。
- ADR-04：OLT/ONU 类别、厂家型号、配置变体、需求槽位、站点与合同范围分开建模。
- ADR-05：条款效力和参考数量有独立适用关系，禁止凭简单数量差异自动认定工程违规。
- ADR-06：PydanticAI 单 Agent 正式选定，固定核验始终有无模型路径；模型不得直接作最终判定。
- ADR-07：MinerU 作为历史解析适配器，不隐含来源 PDF 绑定证明；只允许证据精度逐级提升。
- ADR-08：SQLite 精确字段+FTS5 trigram、短词回退；只有实际漏召回时考虑语义向量。
- ADR-09：本地 Web + PDF.js 为 v1 目标，先保证物理页正确，再验区域高亮。
- ADR-10：所有评测分合成逻辑、合成 E2E、授权真实、公开外部四层，不混报性能。

## 11. 启动约束

真实铁路资料：个人项目使用权、第三方模型访问权、公开/比赛再分发权**仍需分别确认**。未确认时只能用公开合成数据编码/展示，不允许把研究资料中可见的文件当成已授权。目标机器的实际依赖版本、GPU/CPU 性能和 Agent 模型费用**均尚未实测**。这些不阻止开始合成数据工程，但必须限制可宣称的验证范围。
