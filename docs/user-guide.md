# 安装与使用

## 从源码或 wheel 安装

源码目录执行 `uv sync --locked --python 3.12`，然后用 `uv run --locked ira` 执行下述 CLI。已获得候选包时，在独立目录执行：

```powershell
uv venv --python 3.12
uv pip install .\ira_native-0.2.0rc2-py3-none-any.whl
$env:IRA_DB = Join-Path $PWD 'data/ira.sqlite'
$env:IRA_ENABLE_UPLOADS = '1'
.\.venv\Scripts\ira.exe doctor
.\.venv\Scripts\ira.exe serve --port 8000
```

访问 `http://127.0.0.1:8000`。Doctor 和工作台首页均显示安装版本。安装需要依赖源或预先准备的离线缓存。Windows Python 3.12 已验证。

## 公开合成演示

从源码工作树运行，输出目录须为新目录。

```powershell
uv run --locked python -c "import sys; from pathlib import Path; sys.path.insert(0, 'tests'); from fixtures.synthetic.mvp_workflow import generate; generate(Path('data/ira-demo'))"
```

在工作台导入生成的 `spec.pdf`、`simulated.json` 和 `devices.xlsx` 或 `devices.csv`。PDF 为虚构资料，JSON 是模拟 MinerU 4.2 结构，示例不度量解析准确率。选择三组配对后，分别核对前两项的字段、来源、实体与适用性并重算。前两项分别显示规则满足与待核实问题，第三项因工况未确认仍待复核。

## 实际使用

使用对应同一 PDF 的 MinerU 4.2 解析 JSON，与已授权的 CSV/XLSX 一起登记。多资料任务须选择资料角色、物理页与清单原始行。确认 `source_revision` 时使用可核对依据，未知版本保留待复核。若提示选择已更新，请重新打开当前任务。

报告页提供汇总 JSON、明细 JSON/CSV、版本化结果和打印。明细含原值和审核依据，按授权范围保存。

`IRA_DB` 默认是当前目录的 `data/ira.sqlite`。Web 上传需显式开启 `IRA_ENABLE_UPLOADS=1`。CLI 按路径登记使用 `IRA_ALLOWED_ROOTS`，多个目录用系统路径分隔符连接。迁移前停止服务并备份 SQLite 与原始资料。移动按路径登记的资料后应在新位置重新登记；更新版本首次打开数据库时运行内置迁移。
