# 12 API 与统一 CLI 速查手册

> **定位**：无需翻阅上千行源码即可直接调用 `scripts/cli.py`、PowerShell 桥接脚本及核心 Python 构建器函数（所有命令均从 `<SKILL_DIR>` 约定路径执行）。

---

## 1. 统一工程 CLI (`scripts/cli.py`)

```powershell
# 1. 四方对账生成《01_识图清单.md》
python "<SKILL_DIR>/scripts/cli.py" reconcile --project-name OP10压装 --plc-table io.xlsx --bom bom.xlsx --spec fds.md --tia-status-json env.json --out-md 01_识图清单.md

# 2. 提取工艺状态机 IR JSON（可选同步导出 FDS.docx）
python "<SKILL_DIR>/scripts/cli.py" extract-ir --io io.xlsx --bom bom.xlsx --pdf sch.pdf --safety-matrix matrix.md --out-json ir.json --export-fds FDS.docx

# 3. CAx .aml 硬件拓扑预检（可选输出 TO_Params.xlsx）
python "<SKILL_DIR>/scripts/cli.py" inspect-aml topology.aml --to-template TO_Params.xlsx

# 4. 批量生成监控表 & 冷态强制表 XML
python "<SKILL_DIR>/scripts/cli.py" build-watch-tables --io-table io.xlsx --out-dir 03_梯形图XML --timer-member T1_超时

# 5. 生成 S7-1200 自反馈闭环块 FC99_仿真自反馈.xml
python "<SKILL_DIR>/scripts/cli.py" build-sim-feedback --io-table io.xlsx --ir-json ir.json --out-xml 03_梯形图XML/FC99_仿真自反馈.xml

# 6. 静态拓扑与安全审计 / 本地 Safety Critic / 闭环仿真
python "<SKILL_DIR>/scripts/validate_lad_xml.py" 03_梯形图XML --eplan-io 01_识图清单.md --tia-status-json env.json [--strict-compact]
python "<SKILL_DIR>/scripts/cli.py" critic 03_梯形图XML --ir-json ir.json --tia-status-json env.json [--allow-fragmented]
python "<SKILL_DIR>/scripts/cli.py" simulate 03_梯形图XML --ir-json ir.json --cpu-model "CPU 1214C DC/DC/DC"

# 7. 生成交付三表（本地 XML 或从博途反向导出）
python "<SKILL_DIR>/scripts/cli.py" deliver 03_梯形图XML --out-dir 05_交付归档
python "<SKILL_DIR>/scripts/cli.py" deliver 03_梯形图XML --from-tia --out-dir 05_交付归档
```

---

## 2. PowerShell 桥接与导入流水线

| 脚本 | 常用调用参数 | 说明 |
|---|---|---|
| `eplan_auto_export.ps1` | `-Action inspect-eplan \| export-all -EplanProjectPath <.elk> -OutputDir <dir> [-TiaVersion 16]` | 探测 `W3u.exe` 并静默导出 `.aml`/点表/BOM/PDF |
| `tia_bridge_core.ps1` | `-Action inspect-env \| list-processes \| import-cax \| configure-to \| import-watch-tables \| compile-plc \| compare-online \| download-plc` | Openness 10 项底层操作（多实例加 `-ProcessIndex`/`-ProjectPath` 与 `-PlcName`） |
| `do_import_pipeline.ps1` | `-ImportDir <dir> -DiffOnly` ➔ 人工确认后 `-ImportDir <dir> -ConfirmToken <16位> -BackupDir <dir>` | 串行互斥锁、`.zap<Ver>` 备份、块级快照回滚与事务编译 |

---

## 3. 核心 Python 构建器签名速查（仅在需要自定义控制网络时调用）

### 3.1 `tag_builder.py`
```python
build_tags(tag_list: list[dict], excel_path: str, xml_path: str, table_name: str | None = None) -> tuple[bool, list[str]]
# tag_list 元素字典最小格式:
# {"name": "SB0_急停按钮", "data_type": "Bool", "logical_address": "%I0.0", "comment": "[1.1 | -SB0 | 11-12/101] 急停按钮(NC)"}
```

### 3.2 `lad_builder.py`（优先使用复合微功能闭环构建器）
- `build_composite_network(nw_index, title, comment, rungs) -> str`（通用单段多梯级/内联 TON/PBox/T型分叉多线圈底座）
- `build_sr_latch_network(nw_index, title, comment, target_var, set_branches, reset_branches, set_series_after=None, reset_series_after=None, ...) -> str`（同段成对 SCoil + RCoil）
- `build_timeout_alarm_network(nw_index, title, comment, enable_contacts, timer_db_member, time_pt, alarm_var, reset_contacts, timer_db_name="全局定时器", summary_alarms=None, summary_var=None) -> str`（TON 直连 SCoil 报警 + RCoil 复位 + 可选汇总）
- `build_mode_select_network(nw_index, title, comment, selector_var, auto_mode_var="M_自动模式", manual_mode_var="M_手动模式", safety_var="M_安全条件满足", single_di_selector=True, manual_selector_var=None, extra_rungs=None) -> str`
- `build_step_with_timer_network(nw_index, title, comment, step_var, trigger_contacts, timer_db_member, time_pt, next_step_vars=None, mode_var="M_自动模式", safety_var="M_安全条件满足", alarm_summary_var="M_报警汇总", extra_hold_contacts=None, timer_db_name="全局定时器", auto_exit_on_timer_done=True, timer_done_coil_var=None) -> str`
- `build_dual_interlocked_actuators_network(nw_index, title, comment, actuator_a, actuator_b, safety_var="M_安全条件满足", manual_mode_var="M_手动模式", auto_mode_var="M_自动模式") -> str`（同设备正反转/双电控伸缩合段互锁）
- `build_star_delta_networks(start_nw_index, device_label, start_contacts, stop_contacts, km_main, km_star, km_delta, timer_switch_member, timer_delay_member, switch_pt="T#6S", delay_pt="T#300MS", ...) -> list[str]`（2 段式紧凑星三角）
- `build_indicator_lamps_network(nw_index, title, comment, lamp_rungs) -> str`（状态指示灯同段组合）
- `build_simple_series_network(nw_index, title, comment, contacts, coil_var, coil_type="Coil", coil_scope="GlobalVariable") -> str`
- `build_latch_network(nw_index, title, comment, start_var, latch_var, stop_contacts, coil_var, pre_contacts=None, ...) -> str`
- `build_or_branches_series_network(nw_index, title, comment, branches, series_after, coil_var, coil_type="Coil", ...) -> str`
- `build_manual_auto_interlocked_actuator_network(nw_index, title, comment, coil_var, manual_trigger_var=None, auto_request_var=None, manual_mode_var="M_手动模式", auto_mode_var="M_自动模式", safety_var="M_安全条件满足", interlock_contacts=None, ...) -> str`
- `build_step_latch_network(nw_index, title, comment, step_var, trigger_contacts, next_step_vars=None, mode_var="M_自动模式", safety_var="M_安全条件满足", alarm_summary_var="M_报警汇总", extra_hold_contacts=None, ...) -> str`
- `build_ton_timer_network(nw_index, title, comment, in_contacts, timer_db_member, time_pt="T#5S", coil_var=None, coil_type="Coil", ...) -> str`
- `build_pbox_network(nw_index, title, comment, in_contacts, edge_bit_var, coil_var, coil_type="Coil", edge_type="PBox", ...) -> str`
- `build_fb_call_network(nw_index, title, comment, fb_name, instance_name, parameters, instance_scope="GlobalVariable", en_contacts=None) -> str`
  - `parameters` 项格式：`{"name": "形参名", "section": "Input|Output|InOut", "type": "Bool|Time|Int", "value": "实参变量或T#3S", "scope": "GlobalVariable|TypedConstant|LiteralConstant"}`
- `build_line_handover_networks(start_nw_index, upstream_station, downstream_station, handover_prefix, upstream_done_var, downstream_ready_var, downstream_arrived_var, transfer_actuator_req_var, safety_var="M_安全条件满足", fault_var="M_报警汇总", compact=False) -> list[str]`（传 `compact=True` 合并为 2 段）
- `build_instance_db_xml(idb_name, idb_number, fb_name, comment="", input_members=None, output_members=None, inout_members=None, static_members=None) -> str`
- `build_fc_xml(fc_name, fc_number, networks_xml, comment="") -> str`
- `build_timer_db_xml(db_name, timer_members) -> str`
- `build_ob1_xml(fc_names_or_fb_call_dicts) -> str`
- `write_xml_file(file_path, xml_content) -> None`（自动写入 UTF-8 BOM + CRLF）

> `contacts` 元素统一支持 `{"name": "SB0_急停按钮", "negated": False}` 或元组 `("SB0_急停按钮", False)`。

### 3.3 `delivery_builder.py`
```python
create_delivery_tables(
    project_info={"project_name": "...", "plc_model": "SIMATIC S7-1200", "schematic_ver": "V1.0"},
    io_list=io_tags, out_xlsx="05_现场调试交付三表.xlsx", out_md="05_现场调试交付三表.md", fc_list=fc_list
)
```
