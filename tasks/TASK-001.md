# TASK-001｜正式证据基础与可运行应用骨架

> **v0.3 严格复核稿｜2026-11-23｜尚未授权执行**。这是给未来 Codex 的正式代码任务，**本次审查不启动编码**。PRD、CONTRACTS、ARCHITECTURE、AGENTS 与 README 已合入正式 Git 仓库；执行前用户须明确 Codex 工作分支/Worktree 和权限。

## 1. 目标、范围与真实业务关系

为 IRA Native v1.0 实现**可被后续清单核验、Agent 与 Web 共用的证据底座**：PDF/解析产物登记 → MinerU 4.2 形态适配 → 不可变 Evidence → 精确/FTS 检索 → PDF 原始物理页与可信块级回查 → CLI/API。**不做孤立 PoC，不创建第二套项目。**

本任务**不做**供应清单匹配、GPON 属性抽取、差异判断、PydanticAI 真实调用或 Web 业务 UI。这些已在正式 v1 产品路线中分别列为后续任务；本任务需稳定接口以复用，不需要空造后续完整模块。

## 2. 修改授权边界

允许：`src/ira_native/` 中本任务需要的 `domain/application/adapters/interfaces/`；`tests/` 中合成 PDF/JSON、回归与 API 测试；`pyproject.toml`、`uv.lock`、数据库迁移；必要运行说明。`docs/ARCHITECTURE.md` 可补真正发现的已核查技术差异，不能自行更改 PRD 核心范围或 CONTRACTS 结果状态。

不得操作：总目录 `private/` 或其他 Agent 工作区、真实铁路 PDF/解析产物、GitHub 可见性/仓库历史、用户环境密钥、外部模型服务。没得到真实资料授权时全部测试用合成资产；用户不再要求四 Agent 的文件系统隔离专项测试，不应自行扩展。

## 3. 输入契约（从实际资料而非想象设计）

已检查的历史目标解析样本特征：`structured_content.json` 是顶层 `pages/metadata/extensions/is_full_document`，`pages[*].page_idx`、`blocks[*].type/content/bbox/continues_prev`；其 `metadata.producer.version` 可标识 MinerU 版本，bbox 在本批材料是 `0..1`；**无可靠 PDF source_sha256 字段**。官网最新文档介绍的结构不代表所有旧文件具有同样的 schema_version，必须有格式指纹/适配版本门槛。

输入应包括已登记 PDF 原件及 JSON 原始字节。用 source SHA/产物 SHA 生成稳定身份，但不得因两者文件名类似、页数相同就写 `binding=verified`。人工声明配对时必须输出 `binding=declared/unknown`；只有额外可核查的 manifest/原文对应检查才能升级相应受限状态。**来源配对证明、位置坐标合法、内容正确**分别保存。

合成 PDF + 形态一致 JSON 用于开发，必须标注是**人工生成的测试配对**，不得冒充真实 MinerU 的成功解析结果。历史 TX-03 可在获得明确个人项目本地使用授权后单独用作适配回归，绝不入仓库。

## 4. 具体交付

1. **工程骨架**：`src` 包、Python 3.12、uv/锁文件、pytest、Typer CLI、FastAPI 基础接口、可重复的本机配置与启动命令；模块间依赖符合 ARCHITECTURE。
2. **Source Registry**：PDF 和 JSON 字节 SHA、SourceAsset、Document/Revision（未知允许为空）、ParseRun、来源访问策略与枚举；数据库唯一约束和回滚/失败处理；重复导入幂等，同原件不同 JSON 是不同 ParseRun。
3. **Parser Adapter**：严格读取已知 `pages/page_idx/blocks`、原生原文/表格、bbox、续接标志；未知结构拒绝/警告，不静默修复；保留原 JSON pointer 与转换版本。单元格来源不得虚构。
4. **Evidence Store / Retriever**：Evidence ID 可由 `parse_run_id + source_pointer + adapter_version` 决定，重复输入稳定；结构化文件/页/内容检索；精确/LIKE 与 FTS5 trigram + 短字符串回退。每条 Evidence 关联来源许可。
5. **PDF Locator**：pypdfium2 校验文件物理页数、`page_idx`、bbox/coord_system，转换位置（考虑旋转/PageBox）并标精度；可信时块定位，不能可信时页级，不输出假高亮。原始文件只读。
6. **统一应用服务**：`ingest_source/ingest_parse_run/search_evidence/get_evidence/get_pdf_page`，CLI 与 API 调相同服务而非各自写 SQL。
7. **CLI/API**：`ira doctor`, `ira ingest pdf --pdf ... --parsed ...`, `ira search`, `ira evidence show`, `ira evidence page`；API 通过资产 ID 获取已登记文档和证据，返回结构化错误 `code`、位置精度和来源风险，**不支持任意本地路径作为读取参数**。
8. **安全与日志**：默认禁云端模型；输入路径白名单及上传体积限制（按资源可配置）；无原文/密钥的脱敏运行事件；公开合成资料测试；提交前防敏感文件检查。

## 5. 关键不变量

- `physical_page=page_idx+1`，是**PDF 文件物理页**，不是印刷页码。第一页、最后一页、空页、越界页均需处理。
- 同一 PDF+JSON+adapter 再次导入证据和主键不变；PDF 相同而 JSON 不同则新增 ParseRun/Evidence，历史引用不得指到新产物。
- 页数、bbox 数值合法≠与原件文字重合；内容证明与位置证明分离，未核查的产物不能声称全文已证实。
- 表格只含块级 bbox 时不提供细粒度 cell bbox；跨页 `continues_prev` 不自动认定跨页表头归属正确。
- `GE(0)` 与 `GE(O)` 等原始字符不自动互换；原文绝不被归一字段覆盖。
- 候选行、解析错误、PDF 缺失、无搜索命中、权限拒绝、来源错配分别有机器可读状态。

## 6. 必须执行的验收

| 编号 | 真实测试要求 |
|---|---|
| T01 | 合成 PDF+结构化 JSON 的导入生成独立 SourceAsset/ParseRun/Evidence，记录原字节哈希、原生来源指针、绑定状态 |
| T02 | 原件/解析产物重复导入幂等；不同解析产物生成不同 ParseRun，旧 Evidence 可解引用 |
| T03 | 原 PDF 物理第一页/末页回查正确；印刷页码不参与计算；无效页码返回机器错误 |
| T04 | 归一 bbox、旋转/裁切或未知坐标协议使用正确变换或降级，页面与块高亮不漂移冒充 cell |
| T05 | 中文、短中文词、GPON/OLT/ONU、符号/带括号设备配置检索有实际结果；FTS5 无法使用时有明确回退或报错 |
| T06 | 表格及跨页续接保留原始块与两个页的定位来源；不自动编造表头归属 |
| T07 | 格式未知 JSON、损坏 JSON、原件/产物声称错配、文件越权、`../` 路径、畸形 PDF 均按规定失败 |
| T08 | API 与 CLI 对相同 Evidence 返回相同核心字段和 ID；客户端不能任意下载 `private` 文件 |
| T09 | 导入过程不得修改源 PDF；失败/重复请求不会产生脏孤儿行或泄露原文日志 |
| T10 | 可安装、运行测试和启动 API 的命令在目标环境可复现，锁文件记录实际解析库版本/许可 |
| T11 | 合成测试明确标记“模拟格式/配对”，不因手工制造 JSON 就宣称已证明 MinerU→PDF 真实链路 |

额外建议：加入 PDF 嵌入恶意指令、CSV 路径和异常长表格字符串等输入安全案例。真实 PDF 的字段级正确率、Agent 收益与 Web 不属于本 TASK 的通过条件。

## 7. 开发与交付流程

Codex 执行时：检查当前 Git 分支与提交 → 读 README/AGENTS/PRD/CONTRACTS/ARCHITECTURE/TASK → 确定小范围接口后直接在正式项目编码、运行相关 pytest、修复缺陷并给出可重复命令；不反复问已确定的技术路线。遇数据授权、敏感原件或破坏性操作须停止取得用户批准。

交付：Git diff/commit SHA（如有），已运行的命令、测试通过数/失败数、未运行的真实性能验证与原因；标记真实问题/变更记录，不把“架构决定”写成“软件已实现”。

## 8. 后续任务接口（非本轮实现）

- TASK-002：CSV/XLSX + GPON类别/配置变体/范围/计数口径 + 要求候选和字段来源。
- TASK-003：要求效力/版本适用性 + `RequirementEvaluator`/`ConstraintComparator` + Finding。
- TASK-004：PydanticAI EGA + Web/PDF.js + 人审 + 事件日志。
- TASK-005：完整 E2E + B0/B1/B2 实测/失败集及风险说明。

**本任务文件准备到此停止，不代表用户已经授权 Codex 开始。**
