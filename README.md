# ⚡ plc-agent

**EPLAN Electric P8 ➔ 西门子博途 (TIA Portal V16–V20) 产线级纯梯形图 (LAD) 闭环工程与虚拟调试 Agent Skill / MCP 工具链**

[![TIA Portal](https://img.shields.io/badge/TIA_Portal-V16--V20-004C87?logo=siemens)](https://www.siemens.com/)
[![PLC Support](https://img.shields.io/badge/SIMATIC-S7--1200_%7C_S7--1500_%7C_ET200SP-009999)](https://www.siemens.com/)
[![Language](https://img.shields.io/badge/IEC_61131--3-100%25_Pure_LAD_(Zero_SCL)-success)](#)
[![Python](https://img.shields.io/badge/Python-3.8%2B_(64--bit)-3776AB?logo=python)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/FastMCP-OT_Hardened-orange)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`plc-agent`（内置 Skill：`eplan-to-tia-lad`）是一套面向**电气设计工程师、电气调试工程师与工业自动化开发者**的生产级 AI 辅助工程流水线。它彻底摒弃了让大模型“凭空手写易错 XML 或混写 SCL 代码”的玩具级做法，采用 **“EPLAN 多模态/报表解析 ➔ 实测 SimaticML 模板克隆 ➔ 有向图割点安全形式化审查 (Safety Critic) ➔ TIA Openness 串行事务导入与编译自愈 ➔ 离散扫描周期闭环虚拟调试”** 的确定性工程架构，将静态电气图纸与工艺步序表直接转化为可编译、可防撞、可现场打点的博途工程与调试交付物。

---

## ✨ 核心特性 (Why `plc-agent`?)

* **🚫 100% 纯梯形图 (Zero SCL) 与实测样板克隆**：严守产线维护直观性红线，严禁生成任何 `.scl` 代码。内置 20 份从真实博途 V16 导出的权威 SimaticML 母本（覆盖 `OB1/OB30/OB100`、`FC/FB` 多重背景、`GlobalDB/InstanceDB`、`UDT`、`PlcTagTable`、`PlcWatchTable`、`PlcForceTable` 及 17 类高频工艺指令），通过确定性 Python 构建器组装，杜绝 Schema 报错。
* **🛡️ 图论割点级 Safety Critic 形式化安全审查**：内置基于有向能流图（Directed Power-Flow Graph）割点判定算法的本地审查器 (`validate_lad_xml.py` & `run_local_safety_critic`)。在导入博途前强制核验：
  * **S1–S2 (Fail-Safe)**：急停、安全门、光栅、热继、极限开关必须采用物理常闭 (`NC`) 接线，并在使能回路中以常开触点 `-| |-` 扫描；
  * **S3–S5 (防旁路与互锁)**：安全允许位（`M_安全条件满足`）、机械防撞条件、电机热继与正反转/星三角/双电控电磁阀互斥常闭触点，**必须位于手自动并联分支 `O` 汇流节点之后的必经串联主干路上**，彻底杜绝手动调试点动撞机、双线圈冲突与死线圈。
* **🔌 CAx (AutomationML) 硬件预检与 TO 轴控引导**：自动解析 EPLAN 导出的 `.aml` 硬件拓扑，精准拦截西门子 Openness CAx 官方不支持的驱动器（G120/S120/V90）、非按键面板 HMI 及特定 Scalance 交换机（`6GK5 602/612/623-0BA10-2AA3`），并自动生成《`TO_Params.xlsx`》通过 Openness `TechnologicalObjectGroup` 接口配置定位轴。
* **🔒 串行事务导入、块级快照回滚与 3 轮编译自愈**：通过全局命名互斥锁 (`Global\TiaPortalOpennessSerialLock`) 保障单实例 COM 串行安全；导入前强制生成 `.zap16` 全工程备份与被覆盖块 XML 快照；基于去 UId 化的语义指纹 (`block_fingerprint.py`) 签发 16 位 `ConfirmToken`；若编译报错，自动递归解析 `CompilerResult` 错误树并驱动最多 3 轮自愈重导，失败则自动无损回滚。
* **🔄 跨平台闭环虚拟调试分流 (S7-1500 vs S7-1200)**：
  * **S7-1500 / ET 200SP**：自动探测并挂载 `S7-PLCSIM Advanced` (`ISimulationRuntimeManager`)；
  * **S7-1200 / 无 PLCSIM Adv 环境**：自动生成 `FC99_仿真自反馈.xml` 注入机械行程延时闭环，配合内置离散扫描周期仿真器 (`LadScanCycleSimulator`) 完成 **正常循环步序 (Task Success = 100%)**、**传感器卡阻超时报警注入 (Timeout Caught)** 与 **急停/安全门/机械干涉违规率 (Safety Violation = 0)** 三项硬核评测。
* **📊 一键生成《现场调试交付三表》**：正向构建或从任意运行中的博途进程反向提取生成带公式排版、A4 横向打印就绪的《`05_现场调试交付三表.xlsx`》（含 **I/O 静态打点表**、**单机空载调试步骤**、**联锁排障速查表**）及《`PLC变量表_中文.xlsx`》。

---

## 🏗️ 四阶闭环调试流水线与人工门禁架构

```text
[EPLAN .elk 项目 / 6类导出资料]
       │
       ▼ (可选: scripts/eplan_auto_export.ps1 调起 W3u.exe 静默导出)
┌──────────────────────────────────────────────────────────────────────────┐
│ 阶段 0：博途环境巡检 (tia_bridge_core.ps1 -Action inspect-env)           │
│   ├─ 核验 Openness DLL / Windows 用户组 / Attach 已打开工程              │
│   └─ 递归采集 CPU 型号、槽位、IoRanges (精确到 bit) 与系统/时钟存储器    │
└──────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 第一阶：多模态识图、CAx 硬件导入与四方对账                               │
│   ├─ cax_hardware_builder.py：预检 .aml 排除设备 ➔ 生成 TO_Params.xlsx   │
│   ├─ parse_engineering_drawings.py：提取工艺状态机 IR & 固化 FDS.docx    │
│   └─ eplan_parser.py：EPLAN I/O vs BOM 电压 vs 博途硬件组态四方对账      │
└──────────────────────────────────────────────────────────────────────────┘
       │
       ├──▶ 🛑【关卡 ①：人工确认《01_识图清单.md》与 ⚠待确认 项】
       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 第二阶：纯 LAD 生成、Safety Critic 拓扑审计与 Openness 事务编译闭环      │
│   ├─ tag_builder.py / lad_builder.py / watch_table_builder.py 生成 XML   │
│   ├─ validate_lad_xml.py + run_local_safety_critic() 能流割点 S1~S5 审计 │
│   ├──▶ 🛑【关卡 ②：人工确认程序结构与安全联锁审查结果】                  │
│   ├─ do_import_pipeline.ps1 -DiffOnly：语义指纹比对 & 签发 ConfirmToken  │
│   ├──▶ 🛑【关卡 ③：人工确认 Diff 变更清单并授权 ConfirmToken】           │
│   └─ .zap16 备份 ➔ 块级快照 ➔ ExclusiveAccess 事务导入 ➔ 3轮编译自愈     │
└──────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 第三阶：闭环虚拟调试 (plcsim_closed_loop_runner.py)                      │
│   ├─ S7-1500 ➔ PLCSIM Advanced API  |  S7-1200 ➔ FC99_仿真自反馈闭环     │
│   └─ 自动评测：① 步序完成度 100%  ② 超时报警捕获  ③ 安全防撞违规 = 0     │
└──────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 第四阶：物理产线冷态打点、受控下装与三表交付                             │
│   ├─ 导入工位 WatchTable 绑定 Web Server 静态打点                        │
│   ├──▶ 🛑【高危门禁：CompareToOnline + 人工确认后方可执行实机下载/强制】 │
│   └─ delivery_builder.py：产出《05_现场调试交付三表.xlsx》与自检清单     │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 🛠️ 环境要求与快速安装

### 1. 系统与软件依赖
* **操作系统**：Windows 10 / 11 (64-bit) 或 Windows Server
* **博途环境**：Siemens TIA Portal V16 – V20（脚本自动按 `V20` → `V16` 降序探测注册表与 `PublicAPI\Vxx\Siemens.Engineering.dll`，需勾选安装 **TIA Portal Openness** 组件）
* **权限配置**：当前 Windows 登录用户必须已加入 **`Siemens TIA Openness`** 本地用户组（计算机管理 ➔ 本地用户和组 ➔ 组 ➔ 添加用户后**注销并重新登录 Windows** 生效）
* **Python 环境**：Python 3.8+ (64-bit)
* **EPLAN 环境（可选）**：若需从 `.elk` 源项目一键静默导出，需本机安装 EPLAN Electric P8 (2.9 / 2022–2026)

### 2. 安装依赖与挂载 Skill
```powershell
# 1. 克隆仓库
git clone https://github.com/powerful307381-lang/plc-agent.git
cd plc-agent

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 运行离线单元测试与样板合规校验（无需打开博途即可验证核心引擎）
python -m pytest tests -v
python scripts/validate_lad_xml.py templates
```

* **在 Claude Code / Antigravity CLI (`agy`) / Codex 中作为 Skill 使用**：
  将本项目文件夹放入你的工作区 `.claude/skills/eplan-to-tia-lad`、`.agents/skills/eplan-to-tia-lad` 或全局 Skills 目录（`~/.gemini/config/skills/`），Agent 会自动识别 `SKILL.md`。注意：`agents/openai.yaml` 中的 `allow_implicit_invocation: false` 策略主要由 OpenAI Codex 等兼容运行时原生读取；在 Antigravity / Claude Code 中仍需借助 `SKILL.md` 的高危门禁红线与脚本硬拦截确保受控操作。
* **作为标准 FastMCP Server 启动**：
  ```powershell
  python scripts/tia_connect.py --mcp
  ```

---

## 🚀 使用指南：每次使用时需要准备什么文件？

每次新建产线或工位项目时，建议在 `项目/<项目名>_<日期>/00_输入图纸/` 下准备以下输入资料（详见 [`references/09_产线级EPLAN导出与工艺输入规范.md`](references/09_产线级EPLAN导出与工艺输入规范.md)）：

| 序号 | 输入资料名称 | 文件格式 | 是否必选 | 用途说明 |
| :---: | :--- | :---: | :---: | :--- |
| **①** | **《PLC 地址与通道分配表》** | `.xlsx` / `.csv` | **必选** | 提取 `%I/%Q/%IW/%QW` 地址、槽位、元器件代号(`-SB0`/`-YV1`)、端子/线号、页.列、NO/NC、中文功能 |
| **②** | **《设备 BOM 与端子连接表》** | `.xlsx` / `.csv` | 强烈推荐 | 核验接触器线圈电压（拦截 24VDC 晶体管 CPU 直驱 220VAC 线圈）、识别单/双电控电磁阀 |
| **③** | **《全套电气/气动原理图》** | 矢量 `.pdf` | **必选** | 核查硬线急停回路、接触器 21-22 硬件互锁及机械布局（无 API Key 时自动使用本地 `pdfplumber` 解析） |
| **④** | **《产线工艺动作步序表 (FDS)》** | `.md` / `.xlsx` | **必选** | 定义原点条件、一键回原位先后顺序、自动步序 (`S10➔S20...`) 及超时报警阈值（缺失时 Agent 会自动提问共创并导出 `FDS.docx`） |
| **⑤** | **《机械防呆与安全联锁矩阵》** | `.md` / `.xlsx` | **必选** | 定义每个 `%Q` 执行器必须串联的防撞允许条件与互斥线圈，供 `Safety Critic` 形式化校验 |
| **⑥** | **《CAx 硬件拓扑与轴参数》** | `.aml` / `.xlsx` | 按需提供 | EPLAN 导出的 `.aml` 用于自动生成博途硬件组态；含伺服/变频器时自动生成 `TO_Params.xlsx` |

---

### 方式 A：通过 `agy` 从 EPLAN `.elk` 项目一键全自动导出（零手动点菜单）

如果你本机装有 EPLAN Electric P8，并预先在 EPLAN 中保存了点表导出配置方案（Scheme），可直接调用内置的 `scripts/eplan_auto_export.ps1` 调起 `W3u.exe` 命令行静默导出 `.aml`、`.xlsx` 和 `.pdf`：

```powershell
# 1. 巡检本机 EPLAN CLI (W3u.exe) 路径
powershell -ExecutionPolicy Bypass -File scripts/eplan_auto_export.ps1 -Action inspect-eplan

# 2. 静默导出 AML 硬件拓扑 + PLC IO点表 + BOM表 + 可搜索原理图 PDF
powershell -ExecutionPolicy Bypass -File scripts/eplan_auto_export.ps1 `
  -Action export-all `
  -EplanProjectPath "D:\EPLAN_Projects\OP10_PressStation.elk" `
  -OutputDir "项目\OP10_压装工站_20261117\00_输入图纸" `
  -PlcScheme "PLC_IO_Excel_Export" `
  -BomScheme "BOM_Part_Excel_Export"
```

---

### 方式 B：在 `agy` / Claude Code 中驱动完整闭环流水线

打开 TIA Portal 并加载你的空白（或既有）目标项目后，在终端对 Agent 下达指令：

> **Prompt 示例**：
> *“使用 `$eplan-to-tia-lad` 技能，读取 `项目/OP10_压装工站/00_输入图纸/` 下的 EPLAN 点表、BOM、AML 拓扑和安全联锁矩阵。先与当前打开的博途项目做阶段 0 硬件对账并输出《01_识图清单.md》停在【关卡①】等我确认；确认后生成纯 LAD 程序块与监控表，跑通 Safety Critic 审查、虚拟闭环仿真和博途事务导入编译，最后生成《05_现场调试交付三表.xlsx》。”*

你也可以分步独立调用底层确定性脚本：

```powershell
# Step 1: 巡检博途环境与硬件组态快照
powershell -ExecutionPolicy Bypass -File scripts/tia_bridge_core.ps1 -Action inspect-env > tia_env_snapshot.json

# Step 2: 静态语法、位级地址重叠与 S1~S5 拓扑割点安全审计
python scripts/validate_lad_xml.py "项目/OP10_压装工站/03_梯形图XML" `
  --eplan-io "项目/OP10_压装工站/01_识图清单.md" `
  --tia-status-json tia_env_snapshot.json

# Step 3: 只读语义指纹比对预检（获取 Gate 3 ConfirmToken）
powershell -ExecutionPolicy Bypass -File scripts/do_import_pipeline.ps1 `
  -ImportDir "项目/OP10_压装工站" -DiffOnly

# Step 4: 携带 ConfirmToken 与备份目录执行串行事务导入与编译
powershell -ExecutionPolicy Bypass -File scripts/do_import_pipeline.ps1 `
  -ImportDir "项目/OP10_压装工站" `
  -ConfirmToken "<16位ConfirmToken>" `
  -BackupDir "项目/OP10_压装工站/04_导入编译日志/备份"

# Step 5: 从博途工程反向一键生成《05_现场调试交付三表.xlsx》
python scripts/reverse_to_delivery_tables.py --from-tia --out-dir "项目/OP10_压装工站/05_交付归档"
```

---

## 📂 仓库目录结构

```text
plc-agent/
├── SKILL.md                           # Agent 核心执行规范、9大工程铁律与四阶门禁定义
├── README.md                          # 项目中文开源主页与完整工程操作手册
├── LICENSE                            # MIT 开源协议
├── requirements.txt                   # Python 依赖清单 (openpyxl, pytest, fastmcp, pydantic 等)
├── agents/
│   └── openai.yaml                    # Agent 展示元数据与高危操作显式确认策略
├── references/                        # 12 份工业级领域知识库（按需加载）
│   ├── 01_识图规范.md                 # IEC 81346 代号、NO/NC 端子口诀与四方对账规则
│   ├── 02_命名与注释规范.md           # 变量命名正则与三段式 [页.列 | 代号 | 线号] 注释标准
│   ├── 03_安全信号校验.md             # Fail-Safe S1~S7 规则与有向能流图割点审计原理
│   ├── 04_地址分区规划.md             # MB0~MB9 保留区避让、%M 分区与 IEC_TIMER 规范
│   ├── 05_SimaticML_LAD防报错规范.md  # V16 FlgNet XML 语法红线（TypedConstant/PBox/Call/ET）
│   ├── 06_标准梯形图模板.md           # 启保停、正反转、星三角、单/双电控气缸、变频器标准逻辑
│   ├── 07_导入编译闭环.md             # Openness 进程句柄安全、ExclusiveAccess 事务与回滚
│   ├── 08_调试交付三表模板.md         # 现场 I/O 打点表、空载试车步骤、联锁排障速查表规范
│   ├── 09_CAx_AML_Hardware_Rules.md   # AutomationML 导出要求与 CAx 排除设备清单
│   ├── 09_产线级EPLAN导出与工艺输入规范.md # 6 类产线输入资料字段规范与 Markdown 填空模板
│   ├── 10_Line_Coordination_Patterns.md    # 产线五大控制依赖关系族 LAD 映射范式
│   ├── 11_Commissioning_SOP_Gates.md       # S7-1200/1500 仿真分流、F-PLC 边界与 OT 安全门禁
│   └── 12_API与CLI速查手册.md              # cli.py 统一命令行、PS1 参数与 Python 构建器签名速查
├── templates/                         # 本机 TIA Portal V16 实测导出的权威 SimaticML 样板库（V17–V20 向下兼容导入）
│   ├── 样板结构说明.md                # 20 份样板与 17 段高级指令精确行号索引
│   ├── FB_StationModeManager.xml      # 工位标准模式管理 FB（安全链、手自动、回原位、复位脉冲）
│   ├── FB_StandardActuator.xml        # 标准执行器 FB（手自动分流 + 公共防撞 + 超时 + 双传感粘连）
│   ├── UDT_LineHandover.xml           # 跨工位四步闭环交接握手标准结构体 UDT
│   ├── FC_SimFeedback.xml             # S7-1200 内部自反馈虚拟调试闭环样板
│   ├── WatchTable_Template.xml        # 监控表样板
│   ├── ForceTable_Template.xml        # 冷态强制表样板
│   └── ...                            # OB1 / OB30 / OB100 / FC / FB多重背景 / GlobalDB / IDB 样板
├── scripts/                           # 确定性构建、形式化审计与 Openness 桥接脚本
│   ├── common_utils.py                # 共享 XML 转义、位级地址跨度、BOM/UTF-8 写盘与 CPU 家族识别
│   ├── cli.py                         # 统一工程命令行入口（reconcile/extract-ir/inspect-aml/critic/simulate/deliver）
│   ├── tia_common.psm1                # PowerShell Openness 公共函数库与一次性在线比对票据校验模块
│   ├── eplan_auto_export.ps1          # EPLAN W3u.exe CLI 静默导出脚本 (.elk ➔ .aml/.xlsx/.pdf)
│   ├── eplan_parser.py                # EPLAN 点表/BOM/工艺规范解析与四方对账报告生成器
│   ├── cax_hardware_builder.py        # CAx .aml 排除设备预检、导入与 TO_Params.xlsx 构建器
│   ├── parse_engineering_drawings.py  # 多模态/本地降级工艺状态机 IR 提取、FDS 导出与 Safety Critic
│   ├── tag_builder.py                 # 中文变量表 Excel 与 SimaticML TagTable XML 生成器
│   ├── lad_builder.py                 # 纯 LAD 程序段、FB 调用、IDB、工位交接握手 XML 构建器
│   ├── watch_table_builder.py         # 按工位/功能自动切分生成 PlcWatchTable 与 PlcForceTable
│   ├── sim_feedback_builder.py        # 执行器-传感器自动配对与 FC99_仿真自反馈 XML 生成器
│   ├── validate_lad_xml.py            # SimaticML 全维度静态语法、内存重叠、能流割点与 FB 实参穿透校验器
│   ├── block_fingerprint.py           # 去 UId 化语义指纹提取与 Diff 对比引擎
│   ├── plcsim_closed_loop_runner.py   # PLCSIM Adv / 离散扫描周期仿真器与三项闭环评测引擎
│   ├── tia_bridge_core.ps1            # TIA Openness 底层 C#/.NET 反射桥接核心 (10 大 Action)
│   ├── do_import_pipeline.ps1         # 串行互斥锁、.zap* 备份、块级/新建对象回滚与事务导入编译流水线
│   ├── tia_connect.py                 # FastMCP Server (8 大核心工具) 与 OT 安全防注入封装
│   ├── delivery_builder.py            # 《05_现场调试交付三表.xlsx/.md》正向构建器（含 Excel 公式看板）
│   ├── reverse_to_delivery_tables.py  # 从博途 XML 或运行中博途进程反向一键生成交付三表
│   └── package_skill.py               # 纯净发布包构建脚本（自动清理缓存与剔除 .git）
├── tests/                             # Pytest 自动化回归测试套件（结构/安全/指纹/产线/仿真闭环）
└── evals/
    └── evals.json                     # 端到端基准评测用例集
```

---

## 🔌 FastMCP Server 开放工具集 (`scripts/tia_connect.py`)

当以 `python scripts/tia_connect.py --mcp` 启动时，向外暴露以下 8 个经 Pydantic 严格校验与 OT 防注入加固的工具函数：

1. `eplan_export_project_assets`：自动探测本机 EPLAN `W3u.exe` 并静默导出 `.aml` 硬件拓扑、PLC I/O 点表（`.xlsx`）、BOM 表（`.xlsx`）与原理图（`.pdf`）。
2. `tia_import_cax_aml`：预检 EPLAN `.aml` 硬件拓扑（拦截驱动器/非按键 HMI/受限 Scalance）并调用 `CaxProvider.Import` 导入博途。
3. `tia_configure_technology_objects`：解析《`TO_Params.xlsx`》并将定位轴/驱动参数写入博途 `TechnologicalObjectGroup`。
4. `tia_compile_and_diagnose`：执行 `-DiffOnly` 语义指纹预检、Gate 3 令牌核验、`.zap16` 归档备份、事务性导入与结构化编译错误树提取。
5. `tia_manage_watch_force_tables`：批量导入工位 `PlcWatchTable` / `PlcForceTable` 并可选绑定至 CPU Web Server。
6. `tia_compare_online`：执行离线工程与在线物理 PLC 的 `CompareToOnline` 比对，识别 F-PLC 并签发一次性 15 分钟有效的 `ConfirmOnlineToken`（PLC 离线或不可达时返回 `Success=False` 且不签发令牌）。
7. `tia_download_to_plc`：受三重安全门禁（`human_approved=True` + 经 `compare-online` 严格验签且防重放的一次性 `confirm_online_token` + F-PLC 首次手动下载核验）保护的受控程序下载。
8. `tia_live_data_channel`：支持 TCP 102 (`S7CommPlus` / `Snap7`) 实时映像区读取（需在博途 CPU 属性勾选 **允许来自远程对象的 PUT/GET 通信访问**，连接失败诚实报错）、离散仿真单步快照，以及受人工门禁保护的冷态强制预检（`DryRun` 模式）。

---

## ⚠️ 工业安全免责声明 (Safety Disclaimer)

1. **硬线安全不可替代**：本工具链生成的 PLC 梯形图急停、安全门与互锁逻辑仅作为**辅助功能安全控制**，在任何情况下**绝对不能替代**符合机械安全标准（如 ISO 13849 / IEC 62061）的外部物理硬接线急停回路、安全继电器或物理机械限位挡块。
2. **故障安全型控制器 (F-PLC) 边界**：针对包含安全程序（F-Blocks）的故障安全 CPU（如 `CPU 1214FC` / `CPU 1515F`），首次包含安全程序的完整下载（含安全密码与 F-CRC 确认）**必须由持证工程师在 TIA Portal GUI 界面手动完成**。
3. **现场冷态打点与试车规程**：在向物理 PLC 下载程序或使用强制表测试 `%Q` 输出前，必须严格执行上锁挂牌（LOTO），**断开三相主动力电源与气源主阀（仅保留 DC 24V 控制电源）**，确认机械运动危险区域无人后，由现场工程师人工授权执行。

---

## 📄 开源协议 (License)

本项目基于 [MIT License](LICENSE) 开源。欢迎提交 Issue 与 Pull Request 共同完善工业自动化 AI 工程生态！
