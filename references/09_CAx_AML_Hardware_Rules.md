# 09 CAx (AutomationML) 硬件组态导入与排除设备规范

> **定位**：规范 EPLAN Electric P8 导出 TIA Portal AutomationML (`.aml`) 硬件拓扑文件的工程要求，明确西门子 Openness CAx 排除设备硬性边界，并定义 `scripts/cax_hardware_builder.py` 的预检拦截与 `TO_Params.xlsx` 迁移引导机制。

## 目录 (Table of Contents)
- [1. EPLAN Electric P8 导出 TIA Portal AML 规范](#1-eplan-electric-p8-导出-tia-portal-aml-规范)
- [2. 西门子 Openness CAx 导入排除设备硬性清单](#2-西门子-openness-cax-导入排除设备硬性清单)
- [3. `cax_hardware_builder.py` 预检拦截与 `TO_Params.xlsx` 替代路线](#3-cax_hardware_builderpy-预检拦截与-to_paramsxlsx-替代路线)
- [4. TIA Portal V16 与 V19+ `CaxProvider.Import` 跨版本兼容规程](#4-tia-portal-v16-与-v19-caxproviderimport-跨版本兼容规程)
- [5. 导入后与阶段 0 `inspect-env` 联合对账要点](#5-导入后与阶段-0-inspect-env-联合对账要点)

---

## 1. EPLAN Electric P8 导出 TIA Portal AML 规范

1. **官方导出菜单路径**：在 EPLAN Electric P8 中依次点击 `Project data > PLC > Export data`，导出格式必须选择 **Siemens TIA Portal (AutomationML `.aml`)**，确保包含完整机架（Rack）、槽位（Slot）、PROFINET IO 系统子网与拓扑连接关系。
2. **订货号与固件版本对齐（ArticleNumber & Firmware）**：
   - 西门子部件订货号必须精确到标准格式（如 `6ES7 214-1AG40-0XB0` 或 `6ES7 515-2AM02-0AB0`），并携带版本号（如 `V4.4`、`V2.9`）；
   - 若 EPLAN 中未填写具体固件版本，CAx 导入可能回退至旧版固件或产生降级告警，需在日志中逐条确认。
3. **PROFINET 设备名称与 IP 地址一致性**：
   - EPLAN 导出的站点设备名（Station Name / PROFINET Device Name）与 IP 地址不得与项目内既有站冲突；
   - 对于已手动组态的博途现场工程，推荐优先使用 `CaxImportOptions.RetainTiaDevice`（保留博途侧已有设备属性），防止误覆盖现场 IP 或安全参数。

---

## 2. 西门子 Openness CAx 导入排除设备硬性清单

根据西门子官方《TIA Portal Openness 手册》明确规定的 CAx (AutomationML) 边界约束，**以下三类设备严禁依赖 `.aml` 直接完成自动化组态，必须在导入前由 `cax_hardware_builder.py` 预检拦截**：

| 排除类别 | 典型型号 / 识别特征 | 禁止/受限原因 | 替代工程方案 |
|---|---|---|---|
| **1. 变频器与伺服驱动器 (Drives)** | `SINAMICS G120`、`G110`、`S120`、`V90`、`MICROMASTER`、订货号含 `6SL3` 等 | Openness CAx 导入不支持驱动对象报文（Telegram）及 Drive-CLiQ/TO 工艺参数映射 | 引导导出/填写 **`TO_Params.xlsx`**，通过 Openness `TechnologicalObjectGroup` 与 GSD/报文配置接口完成轴组态与调参 |
| **2. 非按键面板 HMI 设备** | `Comfort Panel` (`TP700/TP900/TP1200/TP1500`)、`Basic Panel` (`KTP*`)、`Unified Panel` (`MTP*`)；仅按键面板（Key Panels 如 `KP8`/`KP32F`）例外 | CAx 不支持触摸屏/精智/统一面板画面工程及连接变量自动导入 | 仅在硬件拓扑中保留网口占位或由工程师通过 WinCC / HMI 通道独立组态，并配合浏览器/Playwright 联调画面 |
| **3. 特定型号 Scalance 工业交换机** | **`6GK5 602-0BA10-2AA3`**、**`6GK5 612-0BA10-2AA3`**、**`6GK5 623-0BA10-2AA3`** | 西门子官方 CAx 兼容性明确排除的 Scalance S/XB 安全与管理型交换机型号 | 拦截并提示在博途硬件目录中手动拖入对应 GSD/HSP，或仅在外部网络规划表登记拓扑端口 |

---

## 3. `cax_hardware_builder.py` 预检拦截与 `TO_Params.xlsx` 替代路线

在执行任何 `CaxProvider.Import` 之前，必须按以下确定性流程运行 `scripts/cax_hardware_builder.py`：

1. **解析 `.aml` XML 树**：提取 `InternalElement` 节点下的 `RoleRequirements`、`TypeIdentifier`、`OrderNumber`/`ArticleNumber`、机架槽位与 I/O 地址属性。
2. **CPU 家族与安全类型判定**：
   - 自动识别控制器为 `S7-1200`（如 `6ES7 21*`）、`S7-1500`（如 `6ES7 51*`）或 `ET 200SP`；
   - 自动检查是否包含故障安全标识（`F-CPU` / `6ES7 ...-1F*` / `FailSafe`），并向阶段三/四注入 F-PLC 首次手动下载门禁标志。
3. **CAx 排除清单硬拦截**：
   - 一旦检测到任意驱动器、非按键 HMI 或上述三款 Scalance 交换机，`inspect_aml_hardware()` 立即将 `valid` 置为 `False`（或列出 `unsupported_devices`），严禁静默强行导入；
   - 若检测到被拦截的驱动器/伺服轴，自动调用 `build_to_params_template()` 生成 `TO_Params.xlsx`（含轴名称、关联驱动器、控制模式、机械螺距、速度/加减速、软硬限位、PID 增益与报文类型列），引导用户补全工艺参数后走 `technology_objects` 通道。
4. **受控放行选项**：仅当工程确认跳过排除设备（过滤或博途侧已预建）且显式传入 `allow_filtered=True` 时，才允许继续调用 Openness CAx 导入。

---

## 4. TIA Portal V16 与 V19+ `CaxProvider.Import` 跨版本兼容规程

1. **TIA Portal V16（本机实测版本 `1600.0.3102.1`）**：
   - `CaxProvider.Import` 签名为：`Boolean Import(FileInfo importFilePath, FileInfo logFilePath, CaxImportOptions importOption)`；
   - 返回值为布尔型，详细导入告警与节点变更信息全部写入 `logFilePath` 指定的日志文件；
   - `tia_bridge_core.ps1 -Action import-cax` 在导入后自动读取并按行解析该日志文件，提炼 `Warnings`、`Errors` 与 `ImportedDevices`，封装为与高版本一致的结构化 JSON。
2. **TIA Portal V17–V20 兼容特性**：
   - 从 TIA Portal V17 起，CAx 导入不再强制校验 `Siemens TIA Openness` Windows 用户组；
   - 在 TIA Portal V19 及更高版本中，新增直接返回 `TransferResult` 对象的重载接口。PowerShell 桥接层通过返回值类型反射自动识别 `Boolean` vs `TransferResult`，确保向上无缝兼容。

---

## 5. 导入后与阶段 0 `inspect-env` 联合对账要点

1. CAx 导入成功后，必须重新执行一次 `tia_bridge_core.ps1 -Action inspect-env` 刷新博途 `HardwareModules` 快照。
2. 核对 EPLAN I/O 表中的全部 `%I`/`%Q`/`%IW`/`%QW` 地址是否均落入新生成硬件模块的 `IoRanges` 区间（注意 Openness `Address.Length` 单位为 **bit** 而非字节）。
3. 核对本机 CPU 的供电与输出类型（如 `DC/DC/DC` 24V 晶体管 vs `AC/DC/Rly`）与 EPLAN BOM 线圈电压是否匹配，不匹配项标红进入【关卡①】等待人工确认。
