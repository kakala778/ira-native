# TASK-004｜受控本机 Agent

## 运行

启动项目已有的本机 Ollama 服务，确认模型已安装且支持工具调用。建议用离线服务配置并保持环回监听。

```powershell
# Ollama 服务侧，修改上下文配置后重启该服务
$env:OLLAMA_NO_CLOUD = '1'
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_CONTEXT_LENGTH = '4096'
ollama serve
```

```powershell
# IRA Native 工作树
$env:IRA_ENABLE_LOCAL_AGENT = '1'
$env:IRA_LOCAL_OLLAMA_MODEL = 'qwen3.5:4b'
$env:IRA_LOCAL_OLLAMA_URL = 'http://127.0.0.1:11434'
uv run --locked ira verify investigate $runId --finding-id $findingId
uv run --locked ira verify investigate $runId --goal '查找当前 OLT 条目的证据' --allow-reverification
uv run --locked ira verify agent-task $runId $taskId
```

Web 在现有核验审查页启动补查，也可勾选允许请求一次重算。任务列表和结果页提供状态、证据原页、未决门、轨迹及 JSON 导出。人工核验在原工作台入口继续。模型故障单独记为阻断或失败，原核验运行仍可用。

本机预检查询服务版本、已安装标签、摘要和工具能力。HTTP 客户端只连字面环回地址，关闭环境代理、重定向和 SDK 自动重试。默认走 EXP-00 验证的 OpenAI 兼容 Chat Completions 与 `reasoning_effort=none`。Ollama 服务由操作者管理，进程本身应保持离线配置。应用不能证明环回端口背后的第三方进程从未转发。

| 配置 | 默认值 |
|---|---|
| `IRA_AGENT_SECONDS` / `IRA_AGENT_REQUEST_SECONDS` | 整体 120 秒 / 单请求 60 秒 |
| `IRA_AGENT_ATTEMPTS` / `IRA_AGENT_EXECUTIONS` / `IRA_AGENT_REQUESTS` | 6 / 6 / 8 |
| `IRA_AGENT_TOKENS` / `IRA_AGENT_CONTEXT_TOKENS` / `IRA_AGENT_RESPONSE_TOKENS` | 总量 16000 / 单请求输入上限 4096 / 输出上限 384 |
| `IRA_AGENT_ROWS` / `IRA_AGENT_BYTES` | 返回记录 12 / 累计 UTF-8 工具反馈 6000 字节 |
| `IRA_AGENT_TEMPERATURE` / `IRA_AGENT_TOP_P` | 0 / 1 |

`OLLAMA_CONTEXT_LENGTH` 控制服务实际上下文，`IRA_AGENT_CONTEXT_TOKENS` 是宿主对服务报告输入 token 的门槛。使用 Ollama `/api/ps` 核对已加载模型的实际长度。宿主还限制每次响应和证据反馈体积。

## 授权契约

任务授权由宿主生成、冻结并绑定 PDF、解析资产、ParseRun、页范围、核验运行、清单、所选候选和预算。模型不能修改授权。任务可一次性完成、部分完成、等待人工、因无进展停止、因配额或超时停止、被阻断或失败。旧任务复查轨迹即可，不能续用其授权。

| 工具 | 数据范围与控制 |
|---|---|
| `search_evidence` | SQL 按 PDF、ParseRun 和物理页过滤，只返回有限摘要。 |
| `get_evidence` | 只读取本任务已发现的 ID，SQL 再按 ParseRun 限制。 |
| `inspect_neighbor_context` | 只接受本任务已发现的 ID，搜索受原页范围限制的相邻页。 |
| `get_verification` | 只投影当前运行和所选候选的字段缺口、条件化与正式状态、快照 ID。无整清单读取。 |
| `request_reverification` | 单独授权、最多一次、有已发现证据后才可请求。旧快照失效即阻断。只能使用现存有效输入和人工记录重算。原 Finding 与人工记录保持原状。 |

模型提交任何批次先由宿主检查工具与资源，再顺序执行。非法批次不执行。没有文件、Shell、开放网络、数据库通用查询或人工确认工具。最终 ID 按发现范围和冻结授权拒绝越权值。轨迹记录请求、工具参数摘要、拦截、执行、响应计数与终止原因。检索词仅记长度和摘要，日常轨迹不保存文档正文和思维链。对自由文本和提示注入的语义判断仍需要人工复核。

## 边界与后续

沿用现有库和本机任务记录。没有新增数据库迁移、进程间配额服务、多 Agent 或远端模型。进程结束后旧任务授权失效。后续 TASK-005 核对完整界面流程、敏感导出策略、独立 Gold 与正式工程验收。真实资料保持授权本机处理，禁止进入云端上下文。
