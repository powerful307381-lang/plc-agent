# 11 产线级闭环调试 SOP、仿真分流边界与高危人工门禁规范

> **定位**：定义从离线代码审查到实机联调的标准化作业程序（SOP），固化西门子 S7-1200 vs S7-1500 仿真差异、F-PLC 安全程序首次下载限制、Openness 单实例串行排队规则以及实机下装/强制输出的人工确认门禁。

## 目录 (Table of Contents)
- [1. 四阶闭环调试标准作业程序（SOP）总览](#1-四阶闭环调试标准作业程序sop-总览)
- [2. S7-1200 与 S7-1500 虚拟仿真物理边界与自动分流规则](#2-s7-1200-与-s7-1500-虚拟仿真物理边界与自动分流规则)
- [3. 故障安全型 PLC（F-PLC）Openness 下载硬限制](#3-故障安全型-plcf-plc-openness-下载硬限制)
- [4. 高危操作人工确认门禁（Human-in-the-Loop Safety Gate）](#4-高危操作人工确认门禁human-in-the-loop-safety-gate)
- [5. 跨工位串行写博途约束与 OT 凭证安全规范](#5-跨工位串行写博途约束与-ot-凭证安全规范)

---

## 1. 四阶闭环调试标准作业程序（SOP）总览

严禁跳过前置阶段直接“一键下载实机”。全产线调试必须严格按以下四个阶段顺序推进：

| 阶段 | 核心目标 | 准入前置条件 | 退出放行标准（Exit Gate） |
|---|---|---|---|
| **阶段一：CAx 硬件与编译自愈** | 完成 `.aml` 硬件组态、工艺 IR 提取、LAD/监控表生成、本地 Safety Critic 审查与博途编译 | 6 类输入齐备（或经 `doc-coauthoring` 澄清固化 `FDS.docx`） | CAx 排除设备已处理；`Safety Critic` 0 违规；`pytest` 全绿；博途 `CompilerResult` 0 错误且 `Saved = true` |
| **阶段二：闭环虚拟调试** | 在无外接物理机台条件下消灭步序死锁、超时漏报与防撞缺陷 | 阶段一博途编译 0 错误 | 跑通三项闭环评测：**Task Success = 100%**、**Sensor Timeout Caught = true**、**Safety Violation = 0** |
| **阶段三：物理 I/O 冷态点检与单动** | 核对硬接线极性、安全门/急停反应及单执行器点动方向 | 阶段二虚拟闭环全通过；物理主动力电源断开仅通 24V 控制电 | 导入 `PlcWatchTable`，全线 DI/DO 冷态打点 100% 吻合（NC 信号常态=1、触发=0） |
| **阶段四：分工位空跑与整线联动** | 逐工位自动循环、激活 `UDT_LineHandover` 交接与节拍统计、交付归档 | 单动无干涉；人工确认下装授权 | 连续空跑/带料节拍稳定，输出《05_现场调试交付三表.xlsx》与 PDF《FAT/SAT 调试报告》 |

---

## 2. S7-1200 与 S7-1500 虚拟仿真物理边界与自动分流规则

根据西门子官方工具链限制，S7-1200 与 S7-1500 在自动化虚拟仿真能力上存在本质差异，智能体必须自动识别 CPU 家族并执行以下分流策略：

| 控制器家族 | 支持的仿真器 | 外部 API 自动化能力 | 本技能自动分流路线 |
|---|---|---|---|
| **SIMATIC S7-1500 / ET 200SP CPU** | S7-PLCSIM Advanced | 支持 `ISimulationRuntimeManager` / `IInstance` 编程 API（`WriteBool`、`ReadBool`、`UpdateTagList`） | 若本机检测到 `S7-1500` 且已安装 `Siemens.Simatic.Simulation.Runtime.Api.x64.dll`，走 **`PLCSIM_ADVANCED_API`** 闭环通道 |
| **SIMATIC S7-1200** | 仅标准版 S7-PLCSIM | **不支持** S7-PLCSIM Advanced API（标准版 S7-PLCSIM 无官方外部读写编程 API） | **自动切换为 `FC_SimFeedback` 自闭环引擎**：自动生成 `FC99_仿真自反馈.xml`（仅当 `M_仿真模式激活 = 1` 时运行），配合闭环步序扫描仿真器完成三项自动化评测 |

> **本机实测备注**：当运行环境（如当前仅安装标准版 `S7-PLCSIM V16` 的工作站）未检测到 PLCSIM Advanced Runtime DLL 时，即使目标为 S7-1500，也自动回退至 `FC_SimFeedback` 自闭环引擎完成逻辑级闭环校验。

---

## 3. 故障安全型 PLC（F-PLC）Openness 下载硬限制

若项目包含故障安全控制器（Fail-Safe CPU，如 `CPU 1214FC` / `CPU 1515F`，存在 `F-Blocks` 安全程序或 `PROFIsafe` 模块）：

1. **首次下载必须手动在博途 GUI 完成**：Openness API 无法绕过安全程序的首次完整安全密码校验、CRC 确认与 F-运行组初始化；首次下载前脚本必须拦截并给出中文提示，由持证工程师在 TIA Portal 界面手动点击“下载到设备”。
2. **后续仅常规程序开放自动下载**：完成首次安全下载且安全程序签名未变更后，若仅修改常规非安全程序块（Regular Program），方可通过 Openness `DownloadProvider` 执行受控自动下载。

---

## 4. 高危操作人工确认门禁（Human-in-the-Loop Safety Gate）

依据 `agents/openai.yaml` 的 `allow_implicit_invocation: false` 策略与工业功能安全准则，**以下三类操作在任何情况下严禁大模型静默自动执行**：

1. **向真实物理 PLC 下载程序（`DownloadProvider.Download()`）**：
   - 必须先执行 `PlcSoftware.CompareToOnline()` 产出在线/离线差异报告（`CompareResult`）；
   - 向现场工程师确认现场无人处于机械运动危险区域且允许停机/下装；
   - 必须收到包含 `CompareOnlineToken` 与 `human_approved=true` 的显式人工确认后才允许调用。
2. **切换物理 PLC 运行模式（CPU `RUN` ↔ `STOP`）**：
   - 禁止程序在无人值守时自动将真实产线 CPU 从 `STOP` 切至 `RUN`。
3. **操作强制表强制物理输出（`PlcForceTable` 强制 `%Q`）**：
   - 执行 `%Q` 物理输出强制打点前，必须确认主动力电源（三相 380V / 气源主阀）已按冷态调试规程隔离或由现场安全员全程监护。

---

## 5. 跨工位串行写博途约束与 OT 凭证安全规范

1. **禁止多智能体并发写入同一博途进程**：
   - 多工位产线可并行进行文档解析或离线 Python XML 构建，但所有调用 `do_import_pipeline.ps1` / `tia_bridge_core.ps1` 写入 TIA Portal 或触发编译的动作，**必须严格串行排队**；
   - `do_import_pipeline.ps1` 强制在进程级获取全局命名互斥锁 `Global\TiaPortalOpennessSerialLock`，防止 COM STA 单线程套间并发冲突引发博途崩溃。
2. **事务性块覆盖备份与回滚（Transactional Overwrites）**：
   - 每次写入覆盖既有块前，除生成全工程 `.zap16` 归档外，脚本必须逐一导出被覆盖块的原版 XML 快照；一旦导入或编译失败，自动导回原块恢复。
3. **OT 凭证与命令注入防护**：
   - 严禁在 `SKILL.md`、Python 脚本、XML 注释或日志中硬编码博途 PLC Master Secret、块专有技术保护密码（Know-How Protection）及 UMC 用户管理凭证；
   - 传入 PowerShell 桥接脚本的路径与块名称参数必须在 Python 侧做严格白名单/字符校验，禁止拼接未转义的 Shell 命令字符串。
