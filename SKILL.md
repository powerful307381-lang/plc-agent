---
name: eplan-to-tia-lad
description: >-
  Use when converting EPLAN exports (.elk, .aml, .xlsx/.csv, .pdf) and process specs into Siemens TIA Portal
  (V16-V20, S7-1200/S7-1500) pure Ladder Logic (LAD) projects, automating Openness commissioning, or generating
  Chinese PLC tag tables and field delivery workbooks (EPLAN点表转博途梯形图/变量表/调试三表).
  DO NOT trigger for SCL/ST/Graph code, generic C# Add-Ins without PLC logic (use tia-portal-openness),
  or silent %Q forcing / bypassing F-PLC manual first download.
---

# EPLAN → 西门子博途全产线 PLC 闭环调试技能

> **路径约定**：下文中的 `<SKILL_DIR>` 指本 `SKILL.md` 所在根目录；执行脚本时请统一使用 `"<SKILL_DIR>/scripts/..."`，勿假设当前工作目录等于技能目录。

## 0. 适用对象、兼容环境与硬性规则（最高优先级）

面向**电气与自动化调试工程师**，将静态 EPLAN 导出包与工艺文件升级为可编译、可仿真、可闭环验证的产线级 TIA Portal 纯梯形图（LAD）工程。
- **运行环境与版本边界**：Windows、Siemens TIA Portal **V16–V20**（SimaticML XML 样板与实机基准为 V16 PublicAPI `1600.0.3102.1`，Openness DLL 探测与 `.zap<Ver>` 归档/校验器动态兼容 V16–V20；注册表 `HKLM\SOFTWARE\Siemens\Automation\_InstalledSW\TIAP*\Global` 或默认 `PublicAPI\Vxx\Siemens.Engineering.dll`）、当前用户属于 `Siemens TIA Openness` 组、Python 3.8+ (64-bit)。

| 编号 | 工程铁律（任何情况下不得违反） |
|---|---|
| R1 | **全中文交付**：对话汇报、变量中文功能段、程序段标题/注释、监控表备注及交付三表一律用简体中文；英文报错必须转译并附中文排查建议。 |
| R2 | **严禁 SCL**：不得生成任何 `.scl` 文件或 `ProgrammingLanguage=SCL` 块；`OB/FC/FB` 强制 `ProgrammingLanguage=LAD`。 |
| R3 | **标准 SimaticML 全块类型**：支持 `OB`、`FC`、`FB`（含多重背景）、`GlobalDB`/`InstanceDB`、`UDT (PlcStruct)`、`PlcTagTable`、`PlcWatchTable`、`PlcForceTable`，以 [templates/](templates/) 本机 V16 样板为唯一 Schema 基准。 |
| R4 | **脚本优先于生成（低自由度执行）与微功能闭环合段**：严禁大模型手拼裸 XML 字节。变量表、LAD 程序段、监控/强制表、CAx AML 预检必须优先通过 `"<SKILL_DIR>/scripts/cli.py"` 或确定性构建脚本组装生成（参数速查见 [references/12_API与CLI速查手册.md](references/12_API与CLI速查手册.md)）。**严禁“一线圈一碎段”及硬造同块一次性过渡 `%M` 桥接位**：成对 `S/R`、超时 `TON`+报警锁存、手自动模式组、单步+本步延时、同设备正反转/双电控伸缩、指示灯组必须通过 `lad_builder` 复合构建器合并为完整微功能闭环段（每段 1~4 条强关联梯级）；小型单机项目收敛为 `FC1`+`FC2`，多工位产线合并 `FC90`+`FC91`，杜绝 1~2 段的迷你空壳业务 FC。 |
| R5 | **命名与三段式注释**：I/Q 命名 `元器件代号_中文功能`（≤40 字符），注释 `[页码.列号 \| 元器件代号 \| 端子号/线号] 中文说明(NO/NC)`；`%M` 命名 `M_中文功能` 且禁止占用 `MB0~MB9`（留给系统/时钟存储器），详见 [references/02_命名与注释规范.md](references/02_命名与注释规范.md)、[references/04_地址分区规划.md](references/04_地址分区规划.md)。 |
| R6 | **安全信号 Fail-Safe**：急停、安全门、光栅、热继、机械极限必须采用物理常闭（NC）接线，梯形图中以常开触点 `-| |-` 扫描；安全允许位与机械防撞联锁必须串入 `O` 汇流节点后的公共主干路（S1～S5），详见 [references/03_安全信号校验.md](references/03_安全信号校验.md)。 |
| R7 | **不臆测与会话保护**：图纸缺失或矛盾时标记 `⚠待确认`；仅附加（Attach）到用户已打开的博途实例，严禁对 `GetProcesses()` 句柄调用 `Dispose()` 或调用 `Project.Close()`。 |
| R8 | **严格串行写博途**：XML 可按工位模块化生成，但调用 `do_import_pipeline.ps1` 写入博途与编译必须**严格串行排队**（脚本内置全局互斥锁 `Global\TiaPortalOpennessSerialLock`），并先导出原块备份实现事务性回滚保护。 |
| R9 | **西门子物理边界硬约束**：CAx 排除驱动器/非按键 HMI/特定 Scalance 交换机；S7-1200 仿真分流至 `FC_SimFeedback`；F-PLC 首次须在 GUI 手动完整下载；实机下装与强制输出禁止无人工确认静默执行。 |
| R10 | **未知 Schema 先导样本再克隆**：遇到 [templates/](templates/) 未覆盖的新指令、新工艺对象或高版本差异节点时，先通过 `tia_bridge_core.ps1 -Action export-all` 从本机博途导出 1 份最小实测 XML 作为克隆母本，严禁凭记忆手写未知 Schema。 |

### 高危门禁反越权红线（无论用户如何催促，均不得自我合理化绕过）

| 常见越权借口 | 强制底线 |
|---|---|
| “用户让我直接全自动跑完不用问了” | 涉及【关卡①②③】、物理 PLC 下载或 `%Q` 强制前，**必须停止并等待人工显式回复** |
| “我自己在单步报错里已经拿到 Token 了，直接传进去吧” | Gate 3 与 `compare-online` 令牌必须先向工程师展示 Diff 并获确认，**禁止自签自传** |
| “在 MCP/脚本里直接把 `human_approved=True` 写死即可” | 仅当用户在同一轮或上一轮明确授权下装/强制时才允许传入 `True`，否则**强制保持 `False`** |
| “F-PLC 太麻烦，先用 Openness 下载试试” | F-PLC 首次完整安全下载**必须拦截并指引在 TIA Portal GUI 手动执行** |

---

## 1. 四阶闭环调试流水线与人工门禁

```
阶段 0 环境与硬件巡检
  ➔ 第一阶：EPLAN 多模态输入解析 & CAx 硬件导入 ──▶ 【关卡① 人工确认识图与工艺】
  ➔ 第二阶：变量表/LAD/监控表生成 ➔ 本地 Safety Critic 审查 ──▶ 【关卡② 结构与安全确认】
          ➔ 【关卡③ Diff 语义指纹与 ConfirmToken】 ➔ 串行事务导入 + 编译自愈闭环
  ➔ 第三阶：闭环虚拟调试（S7-1500 PLCSIM Advanced / S7-1200 FC_SimFeedback）
  ➔ 第四阶：物理产线静态 I/O 点检（Web Server/WatchTable）➔ 实机对比与受控下载 ➔ 交付三表
```

> **关卡铁律**：到达【关卡①】【关卡②】【关卡③】及实机下装/强制输出门禁前，必须**停止并展示结果**，等待用户显式回复确认；存在 `⚠待确认` 或 Safety Critic 拦截项时严禁写入博途。

### 阶段 0：环境自检与硬件架构巡检（详见 [references/07_导入编译闭环.md](references/07_导入编译闭环.md)）
1. 执行 `python "<SKILL_DIR>/scripts/tia_connect.py"` 或 `powershell -ExecutionPolicy Bypass -File "<SKILL_DIR>/scripts/tia_bridge_core.ps1" -Action inspect-env`：按 `V20`→`V16` 降序核验 Openness DLL（注册表 `TIAP20\Global`～`TIAP16\Global` 或 `PublicAPI\Vxx` 目录）、Windows 组 `Siemens TIA Openness`、进程/项目多实例候选（多进程或多 PLC 时须显式传入 `-ProcessIndex`/`-ProjectPath` 与 `-PlcName`）及 `zh-CN` 语言设置。
2. 递归采集 `HardwareModules` 槽位、订货号、`IoRanges`（注意 `Address.Length` 单位为 bit）及系统/时钟存储器字节。

### 第一阶：多模态输入包解析、工艺共创与 CAx 硬件导入
1. **输入包（6 类资料）与 EPLAN 自动静默导出**：
   - **直接喂入导出文件**：① EPLAN `.aml` 硬件拓扑；② `.xlsx/.csv` PLC I/O 清单；③ `.xlsx/.csv` BOM 部件表；④ `.pdf` 电气/气动原理图；⑤ 产线工艺步序表（FDS）；⑥ 安全联锁矩阵与轴参数表 `TO_Params.xlsx`（详见 [references/09_产线级EPLAN导出与工艺输入规范.md](references/09_产线级EPLAN导出与工艺输入规范.md)）。
   - **直接指定 `.elk` 自动导出（可选）**：若用户提供 EPLAN 工程路径（`.elk`/`.elp`），先调用 `"<SKILL_DIR>/scripts/eplan_auto_export.ps1" -Action inspect-eplan`（或 MCP `eplan_export_project_assets(action="inspect-eplan")`）探测本机 `W3u.exe`，再执行 `-Action export-all`（支持单独 `export-aml` / `export-tables` / `export-pdf`）静默调用 `XPlcExportAction`、`label`、`export` 自动生成 `.aml`、PLC I/O `.xlsx`、BOM `.xlsx` 及原理图 `.pdf` 至 `00_输入图纸\`。
2. **数据预筛与跨表对齐（中/低自由度）**：
   - 优先使用 `pandas`/`openpyxl` 对大批量 I/O 表做地址重叠、重名及漏配预筛；
   - 运行 `python "<SKILL_DIR>/scripts/cli.py" extract-ir ...`（内部调用 `extract_process_ir_from_drawings()`）：若配置 `GEMINI_API_KEY` 则调用 `google-genai`（可通过 `GEMINI_MODEL` 配置）交错提取多模态原理图与机械布局；**未配置 Key 或异常时自动降级**为本地 `pdfplumber` + `eplan_parser.py` 解析，生成统一状态机 JSON 中间表示（IR）并记录 `fallback_reason`。
   - 若缺失工艺步序或回原逻辑，调用 `generate_clarification_questions()` 向工程师提 5–10 个结构化澄清问题，并用 `export_fds_docx()`（`python-docx`）固化为标准《控制功能说明书（FDS）.docx》。
3. **CAx 硬件预检与导入（参见 [references/09_CAx_AML_Hardware_Rules.md](references/09_CAx_AML_Hardware_Rules.md)）**：
   - 若提供 `.aml`，必须先运行 `python "<SKILL_DIR>/scripts/cli.py" inspect-aml <aml_path>`：拦截 CAx 不支持的变频器/伺服驱动器（G120/S120/V90 等）、非按键面板 HMI 及 Scalance `6GK5 602-0BA10-2AA3`、`6GK5 612-0BA10-2AA3`、`6GK5 623-0BA10-2AA3`，并自动生成 `TO_Params.xlsx` 引导配置；补全参数后调用 `configure_to_in_tia()`（或 MCP `tia_configure_technology_objects`）写入博途 `TechnologicalObjectGroup`，预检通过后调用 `tia_bridge_core.ps1 -Action import-cax` 导入。
4. 运行 `python "<SKILL_DIR>/scripts/cli.py" reconcile ...` 输出《01_识图清单.md》（含 I/O vs 博途硬件组态对账差异表、电气电压匹配告警与 `⚠待确认` 清单）。👉 **到达【关卡①】**。

### 第二阶：LAD 生成、本地 Safety Critic 审查与编译自愈闭环
1. **变量表与产线 LAD 生成（低自由度脚本执行 + 紧凑合段）**：
   - 调用 `scripts/tag_builder.py` 生成中文变量表 Excel 与 `TagTable_*.xml`。
   - 调用 `scripts/lad_builder.py` 复合网络构建器（`build_composite_network`、`build_sr_latch_network`、`build_timeout_alarm_network`、`build_mode_select_network`、`build_step_with_timer_network`、`build_dual_interlocked_actuators_network`、`build_star_delta_networks`、`build_indicator_lamps_network`、`build_line_handover_networks(..., compact=True)`）与 [templates/](templates/)（含 `FB_StationModeManager.xml`、`FB_StandardActuator.xml`、`UDT_LineHandover.xml`）按 [references/06_标准梯形图模板.md](references/06_标准梯形图模板.md) 与 [references/10_Line_Coordination_Patterns.md](references/10_Line_Coordination_Patterns.md) 构建产线协调梯形图块。**分块收敛原则**：小型单机收敛为 `FC1_安全模式与报警` + `FC2_<设备步序与输出>`；多工位产线合并原 `FC90`/`FC91` 为单一公用报警指示块 `FC90_全局报警与指示灯`。
   - 运行 `python "<SKILL_DIR>/scripts/cli.py" build-watch-tables ...` 自动按工位与功能切分生成 `PlcWatchTable` 与 `PlcForceTable` XML。👉 **到达【关卡②】**。
2. **本地 Safety Critic 形式化审查（ADK Critic-Reviser 模式 + 反碎片化门禁）**：
   - 导入博途前，必须运行 `python "<SKILL_DIR>/scripts/cli.py" critic <xml_dir> --ir-json <ir.json>`（内建调用 `validate_lad_xml.py` 的能流割点拓扑审计、FB 调用实参穿透审计、S1～S5 规则 + 安全联锁矩阵逐条比对，以及**默认严格开启的反碎片化阻断审计**——拦截同块一次性过渡 `%M` 桥接位、跨段拆分 `SCoil`/`RCoil` 及单段迷你空壳业务 FC）。
   - 只要发现任何安全条件旁路、矩阵联锁漏串、死线圈、双线圈、悬空 I/O 或碎片化反模式，立即按 `revision_directives` 修复重生成，直至 `passed = True` 且 `python -m pytest "<SKILL_DIR>/tests" -v` 通过。
3. **【关卡③】语义指纹预检与事务性串行导入**：
   - 只读预检：`powershell -ExecutionPolicy Bypass -File "<SKILL_DIR>/scripts/do_import_pipeline.ps1" -ImportDir "<目录>" -DiffOnly` 获取 `PreImportDiffs` 与 16 位 `ConfirmToken`。
   - 用户确认后携带 `-ConfirmToken` 与 `-BackupDir` 串行执行导入：脚本自动完成 `.zap<Ver>` 全工程归档、**逐块导出备份与失败自动回滚（含新建块清理）**、`GoOffline()`、`ExclusiveAccess + Transaction` 顺序导入（`UDT -> TagTable -> GlobalDB -> FB -> InstanceDB -> FC -> OB -> WatchTable -> ForceTable`）。
   - **编译自愈环（最多 3 轮）**：解析 `CompilerResult` 结构化错误树（`ActionableCompileErrors`），自动定位修复源码并经 Safety Critic 与 `-DiffOnly` 后重导，直至编译 0 错误且 `Saved = true`。

### 第三阶：闭环虚拟调试分流（详见 [references/11_Commissioning_SOP_Gates.md](references/11_Commissioning_SOP_Gates.md)）
- **S7-1500 / ET 200SP（且已装 PLCSIM Advanced）**：走 `ISimulationRuntimeManager` 闭环仿真接口。
- **S7-1200 或未安装 PLCSIM Advanced**：运行 `cli.py build-sim-feedback` 生成 `FC99_仿真自反馈.xml`，再运行 `cli.py simulate <xml_dir> --ir-json <ir.json>` 执行三项评测：正常循环步序完成度（Task Success）、传感器阻断超时报警注入（Timeout Fault）、急停/安全门/跨气缸干涉安全违规率（Safety Violation = 0）。

### 第四阶：物理产线点检、受控下载与交付（严守安全门禁）
1. **静态 I/O 点检与 Web 巡检**：通过 `tia_bridge_core.ps1 -Action import-watch-tables -BindWebServer` 导入工位监控表并绑定到 S7-1500 Web Server，配合浏览器/Playwright 巡检监控页与诊断缓冲区（注：`ForceTable` 在 Openness V16 下为只读单例集合不支持新建命名表，已生成 XML 供手工导入）。
2. **高危操作人工确认门禁**：凡向物理 PLC 执行 `DownloadProvider.Download()`（须先通过 `compare-online` 签发一次性 15 分钟有效票据并在 `download-plc` 严格验签）、切换 CPU Run/Stop 或操作 `PlcForceTable` 强制 `%Q`，必须等待工程师显式确认；若为 F-PLC 故障安全程序，强制提示先在博途 GUI 手动完成首次完整下载。
3. **交付归档**：运行 `python "<SKILL_DIR>/scripts/cli.py" deliver <xml_dir> --out-dir <dir>` 生成带公式统计看板的《05_现场调试交付三表.xlsx》、`.md`、`PLC变量表_中文.xlsx` 及 `06_完成自检清单.md`。

---

## 2. 项目目录结构与完成自检红线

- **工作目录**：`项目\<项目名>_<YYYYMMDD>\` 下分设 `00_输入图纸\`、`01_识图清单.md`、`02_变量表\`、`03_梯形图XML\`、`04_导入编译日志\`（含 `备份\*.zap*` 与块级回滚快照）、`05_现场调试交付三表.xlsx/.md`、`06_完成自检清单.md`。
- **交付前自检红线**：全程 0 SCL；变量名/注释/MB10+ 分区合规；安全输入 NC + 串联主干路 S1～S5 通过；`validate_lad_xml.py` + `run_local_safety_critic()` + `pytest` 全绿；`.zap*` 备份与事务编译 0 错误且 `Saved = true`；`⚠待确认` 项已闭环。

---

## 3. 参考文件与工具索引（按需加载）

| 资源路径 | 职责与加载时机 |
|---|---|
| [references/01_识图规范.md](references/01_识图规范.md) ～ [04_地址分区规划.md](references/04_地址分区规划.md) | 第一阶～第二阶：识图、命名、安全信号 S1–S5 与 `%M` 分区 |
| [references/05_SimaticML_LAD防报错规范.md](references/05_SimaticML_LAD防报错规范.md) ～ [08_调试交付三表模板.md](references/08_调试交付三表模板.md) | 第二阶～第四阶：V16 XML 语法、LAD 模板、导入闭环与交付三表 |
| [references/09_产线级EPLAN导出与工艺输入规范.md](references/09_产线级EPLAN导出与工艺输入规范.md) | 第一阶：6 类产线输入资料格式与填空模板 |
| [references/09_CAx_AML_Hardware_Rules.md](references/09_CAx_AML_Hardware_Rules.md) | 第一阶：AML 导出规范、CAx 排除清单与 `TO_Params` 替代方案 |
| [references/10_Line_Coordination_Patterns.md](references/10_Line_Coordination_Patterns.md) | 第二阶：产线五大控制依赖关系族 LAD 映射范式 |
| [references/11_Commissioning_SOP_Gates.md](references/11_Commissioning_SOP_Gates.md) | 第三～四阶：S7-1200/1500 仿真分流、F-PLC 边界与高危人工门禁 |
| [references/12_API与CLI速查手册.md](references/12_API与CLI速查手册.md) | 全流程：`cli.py` 统一命令行、PowerShell 参数与 Python 构建器签名速查 |
| [templates/](templates/) & [templates/样板结构说明.md](templates/样板结构说明.md) | 全套 V16 XML 样板（含工位模式/标准执行器/交接 UDT/仿真自反馈/监控强制表） |
| `scripts/cli.py` / `common_utils.py` / `tia_common.psm1` | 统一命令行入口、跨脚本共享基础工具与 PowerShell Openness 公共/票据模块 |
| `scripts/eplan_auto_export.ps1` / `cax_hardware_builder.py` / `watch_table_builder.py` / `parse_engineering_drawings.py` | EPLAN `W3u.exe` 静默导出、CAx AML 预检导入、`TO_Params.xlsx`、监控表、图纸 IR 与 Safety Critic |
| `scripts/eplan_parser.py` / `tag_builder.py` / `lad_builder.py` / `validate_lad_xml.py` / `block_fingerprint.py` | 点表解析、变量与 LAD/FB/IDB/交接握手 XML 构建、静态/FB穿透审计与语义指纹 Diff |
| `scripts/tia_connect.py` / `tia_bridge_core.ps1` / `do_import_pipeline.ps1` | 博途进程附加、FastMCP 8 大工具、CAx/TO/监控表导入、事务回滚与编译闭环 |
| `scripts/delivery_builder.py` / `reverse_to_delivery_tables.py` / `package_skill.py` / `tests/` / `requirements.txt` | 交付三表生成、反向导出、纯净打包（`dist/eplan-to-tia-lad-release.zip`）、`pytest` 自动化验证套件 |
