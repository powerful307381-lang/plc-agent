# -*- coding: utf-8 -*-
"""阶段2闭环虚拟调试、CAx预检、监控强制表、MCP安全门禁与单工位MVP回归测试"""

from pathlib import Path
import openpyxl

from conftest import default_m_tags
from cax_hardware_builder import inspect_aml_hardware
from eplan_parser import parse_eplan_plc_table
from lad_builder import (
    build_fc_xml,
    build_manual_auto_interlocked_actuator_network,
    build_ob1_xml,
    build_or_branches_series_network,
    build_simple_series_network,
    build_step_latch_network,
    build_timer_db_xml,
    build_ton_timer_network,
    write_xml_file,
)
from parse_engineering_drawings import (
    extract_process_ir_from_drawings,
    run_local_safety_critic,
)
from plcsim_closed_loop_runner import (
    detect_simulation_backend,
    run_closed_loop_evaluation,
)
from sim_feedback_builder import (
    build_sim_feedback_fc_xml,
    infer_actuator_sensor_pairs,
)
from tag_builder import _write_tag_table_xml
import tia_connect
from validate_lad_xml import validate_project_xmls, validate_single_xml
from watch_table_builder import build_station_watch_tables_from_tags


def _build_mvp_dual_cylinder_project(base_dir: Path, include_b3_interlock: bool = True):
    """构建阶段 0 设计的 OP10_双气缸压装工位（单工位双气缸 + 安全门联锁）完整 MVP 工程"""
    plc_p = base_dir / "op10_io.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["PLC地址", "信号类型", "元器件代号", "元器件端子/线号", "页码.列号", "NO/NC", "中文功能", "是否备用"])
    ws.append(["%I0.0", "DI", "-SB0", "11-12/101", "2.1", "NC", "急停按钮", "否"])
    ws.append(["%I0.1", "DI", "-SQ1", "21-22/102", "2.2", "NC", "安全门联锁开关", "否"])
    ws.append(["%I0.2", "DI", "-SA1", "13-14/103", "2.3", "NO", "手自动选择", "否"])
    ws.append(["%I0.3", "DI", "-SB1", "13-14/104", "2.4", "NO", "自动启动按钮", "否"])
    ws.append(["%I0.4", "DI", "-SB2", "13-14/105", "2.5", "NO", "故障复位按钮", "否"])
    ws.append(["%I0.5", "DI", "-B1", "BK/106", "3.1", "NO", "1号送料气缸缩回到位", "否"])
    ws.append(["%I0.6", "DI", "-B2", "BK/107", "3.2", "NO", "1号送料气缸伸出到位", "否"])
    ws.append(["%I0.7", "DI", "-B3", "BK/108", "3.3", "NO", "2号压装气缸上到位", "否"])
    ws.append(["%I1.0", "DI", "-B4", "BK/109", "3.4", "NO", "2号压装气缸下到位", "否"])
    ws.append(["%I1.1", "DI", "-SB3", "13-14/110", "2.6", "NO", "手动送料点动", "否"])
    ws.append(["%Q0.0", "DO", "-YV1", "A1/201", "4.1", "电磁阀", "1号送料气缸伸出", "否"])
    ws.append(["%Q0.1", "DO", "-YV2", "A1/202", "4.2", "电磁阀", "2号压装气缸下压", "否"])
    ws.append(["%Q0.2", "DO", "-YV3", "A1/203", "4.3", "电磁阀", "2号压装气缸回升", "否"])
    ws.append(["%Q0.3", "DO", "-HL1", "X1/204", "4.4", "指示灯", "声光报警指示灯", "否"])
    wb.save(plc_p)

    matrix_p = base_dir / "op10_safety_matrix.md"
    matrix_p.write_text(
        "# OP10 安全联锁矩阵\n\n"
        "| 序号 | 被保护执行器 (%Q 变量名) | 必须满足的允许条件 | 必须断开的互斥线圈 | 防呆理由说明 |\n"
        "|---|---|---|---|---|\n"
        "| 1 | `YV1_1号送料气缸伸出` | `M_安全条件满足`, `B3_2号压装气缸上到位` | — | 压头未回顶部上到位严禁送料伸出 |\n"
        "| 2 | `YV2_2号压装气缸下压` | `M_安全条件满足`, `B1_1号送料气缸缩回到位` | `YV3_2号压装气缸回升` | 送料未退出模具区严禁下压且双电控互斥 |\n"
        "| 3 | `YV3_2号压装气缸回升` | `M_安全条件满足` | `YV2_2号压装气缸下压` | 双电控互斥 |\n",
        encoding="utf-8",
    )

    ir = extract_process_ir_from_drawings(
        xlsx_io_path=str(plc_p),
        safety_matrix_path=str(matrix_p),
    )

    io_tags = parse_eplan_plc_table(str(plc_p))
    pairs = infer_actuator_sensor_pairs(io_tags, ir_data=ir, default_travel_time="T#1S")
    assert len(pairs) == 2

    tag_dir = base_dir / "02_变量表"
    xml_dir = base_dir / "03_梯形图XML"
    tag_dir.mkdir(parents=True, exist_ok=True)
    xml_dir.mkdir(parents=True, exist_ok=True)

    fc99_xml, sim_timers, sim_m_tags = build_sim_feedback_fc_xml(
        pairs,
        timer_db_name="全局定时器",
        sim_enable_tag="M_仿真模式激活",
        out_xml_path=str(xml_dir / "FC99_仿真自反馈.xml"),
    )
    assert fc99_xml

    m_tags = default_m_tags() + sim_m_tags + [
        {"name": "M_自动模式", "data_type": "Bool", "logical_address": "%M10.2", "comment": "[FC1/NW3] 自动模式"},
        {"name": "M_手动模式", "data_type": "Bool", "logical_address": "%M10.3", "comment": "[FC1/NW4] 手动模式"},
        {"name": "M_步序_S10_送料伸出", "data_type": "Bool", "logical_address": "%M30.0", "comment": "[FC2/NW1] S10送料伸出"},
        {"name": "M_步序_S20_送料缩回", "data_type": "Bool", "logical_address": "%M30.1", "comment": "[FC2/NW2] S20送料缩回"},
        {"name": "M_步序_S30_压装下压", "data_type": "Bool", "logical_address": "%M30.2", "comment": "[FC2/NW3] S30压装下压"},
        {"name": "M_步序_S40_压装回升", "data_type": "Bool", "logical_address": "%M30.3", "comment": "[FC2/NW4] S40压装回升"},
        {"name": "M_报警_压装下压超时", "data_type": "Bool", "logical_address": "%M100.0", "comment": "[FC2/NW5] 压装下压超时报警"},
        {"name": "M_报警汇总", "data_type": "Bool", "logical_address": "%M119.7", "comment": "[FC90/NW1] 报警汇总"},
    ]

    _write_tag_table_xml("IO变量表", io_tags, str(tag_dir / "TagTable_IO变量表.xml"))
    _write_tag_table_xml("M辅助继电器表", m_tags, str(tag_dir / "TagTable_M辅助继电器表.xml"))

    fc1_nws = [
        build_simple_series_network(1, "NW1 急停", "【功能】急停扫描", [{"name": "SB0_急停按钮", "negated": False}], "M_急停正常"),
        build_simple_series_network(
            2,
            "NW2 安全门与急停汇总",
            "【功能】急停与安全门共同满足",
            [{"name": "M_急停正常", "negated": False}, {"name": "SQ1_安全门联锁开关", "negated": False}],
            "M_安全条件满足",
        ),
        build_simple_series_network(3, "NW3 自动模式", "【功能】SA1=1自动", [{"name": "SA1_手自动选择", "negated": False}], "M_自动模式"),
        build_simple_series_network(4, "NW4 手动模式", "【功能】SA1=0手动", [{"name": "SA1_手自动选择", "negated": True}], "M_手动模式"),
    ]
    write_xml_file(str(xml_dir / "FC1_安全与模式.xml"), build_fc_xml("FC1_安全与模式", 1, fc1_nws))

    fc2_nws = [
        build_step_latch_network(
            1,
            "NW1 S10 送料伸出",
            "【功能】在原点按启动进入S10",
            step_var="M_步序_S10_送料伸出",
            trigger_contacts=[
                {"name": "SB1_自动启动按钮", "negated": False},
                {"name": "B1_1号送料气缸缩回到位", "negated": False},
                {"name": "B3_2号压装气缸上到位", "negated": False},
            ],
            next_step_vars=["M_步序_S20_送料缩回", "M_步序_S30_压装下压", "M_步序_S40_压装回升"],
        ),
        build_step_latch_network(
            2,
            "NW2 S20 送料缩回",
            "【功能】送料伸出到位B2=1转入S20退回送料杆",
            step_var="M_步序_S20_送料缩回",
            trigger_contacts=[
                {"name": "M_步序_S10_送料伸出", "negated": False},
                {"name": "B2_1号送料气缸伸出到位", "negated": False},
            ],
            next_step_vars=["M_步序_S30_压装下压", "M_步序_S40_压装回升"],
        ),
        build_step_latch_network(
            3,
            "NW3 S30 压装下压",
            "【功能】送料安全缩回B1=1转入S30压装下压",
            step_var="M_步序_S30_压装下压",
            trigger_contacts=[
                {"name": "M_步序_S20_送料缩回", "negated": False},
                {"name": "B1_1号送料气缸缩回到位", "negated": False},
            ],
            next_step_vars=["M_步序_S40_压装回升"],
        ),
        build_step_latch_network(
            4,
            "NW4 S40 压装回升",
            "【功能】压装下到位B4=1转入S40回升至上到位B3=1结束循环",
            step_var="M_步序_S40_压装回升",
            trigger_contacts=[
                {"name": "M_步序_S30_压装下压", "negated": False},
                {"name": "B4_2号压装气缸下到位", "negated": False},
            ],
            next_step_vars=[],
            extra_hold_contacts=[{"name": "B3_2号压装气缸上到位", "negated": True}],
        ),
        build_ton_timer_network(
            5,
            "NW5 压装下压超时报警",
            "【功能】S30下压超过3秒B4未到位触发超时报警",
            in_contacts=[
                {"name": "M_步序_S30_压装下压", "negated": False},
                {"name": "B4_2号压装气缸下到位", "negated": True},
            ],
            timer_db_member="T1_压装下压超时",
            time_pt="T#3S",
            coil_var="M_报警_压装下压超时",
            coil_type="SCoil",
        ),
        build_simple_series_network(
            6,
            "NW6 压装超时故障复位",
            "【功能】按SB2故障复位按钮清除超时报警",
            contacts=[{"name": "SB2_故障复位按钮", "negated": False}],
            coil_var="M_报警_压装下压超时",
            coil_type="RCoil",
        ),
    ]
    write_xml_file(str(xml_dir / "FC2_产线步序与超时.xml"), build_fc_xml("FC2_产线步序与超时", 2, fc2_nws))

    yv1_interlocks = [{"name": "B3_2号压装气缸上到位", "negated": False}] if include_b3_interlock else []
    fc3_nws = [
        build_manual_auto_interlocked_actuator_network(
            1,
            "NW1 送料气缸伸出控制",
            "【功能】手自动驱动YV1，串入安全门与压装上到位B3防撞",
            coil_var="YV1_1号送料气缸伸出",
            manual_trigger_var="SB3_手动送料点动",
            auto_request_var="M_步序_S10_送料伸出",
            interlock_contacts=yv1_interlocks,
        ),
        build_manual_auto_interlocked_actuator_network(
            2,
            "NW2 压装气缸下压控制",
            "【功能】自动驱动YV2，串入送料缩回B1与回升互锁",
            coil_var="YV2_2号压装气缸下压",
            auto_request_var="M_步序_S30_压装下压",
            interlock_contacts=[
                {"name": "B1_1号送料气缸缩回到位", "negated": False},
                {"name": "YV3_2号压装气缸回升", "negated": True},
            ],
        ),
        build_manual_auto_interlocked_actuator_network(
            3,
            "NW3 压装气缸回升控制",
            "【功能】自动驱动YV3，与下压互斥互锁",
            coil_var="YV3_2号压装气缸回升",
            auto_request_var="M_步序_S40_压装回升",
            interlock_contacts=[{"name": "YV2_2号压装气缸下压", "negated": True}],
        ),
    ]
    write_xml_file(str(xml_dir / "FC3_执行器防撞控制.xml"), build_fc_xml("FC3_执行器防撞控制", 3, fc3_nws))

    fc90_nws = [
        build_or_branches_series_network(
            1,
            "NW1 报警汇总",
            "【功能】汇总超时报警与安全链断开状态",
            branches=[
                [{"name": "M_报警_压装下压超时", "negated": False}],
                [{"name": "M_安全条件满足", "negated": True}],
            ],
            series_after=[],
            coil_var="M_报警汇总",
        )
    ]
    write_xml_file(str(xml_dir / "FC90_报警汇总.xml"), build_fc_xml("FC90_报警汇总", 90, fc90_nws))

    fc91_nws = [
        build_simple_series_network(
            1,
            "NW1 声光报警指示灯",
            "【功能】报警汇总时点亮HL1指示灯",
            [{"name": "M_报警汇总", "negated": False}],
            "HL1_声光报警指示灯",
        )
    ]
    write_xml_file(str(xml_dir / "FC91_指示灯输出.xml"), build_fc_xml("FC91_指示灯输出", 91, fc91_nws))

    all_timers = ["T1_压装下压超时"] + sim_timers
    write_xml_file(str(xml_dir / "DB_全局定时器.xml"), build_timer_db_xml("全局定时器", all_timers))
    write_xml_file(
        str(xml_dir / "OB1_Main.xml"),
        build_ob1_xml(["FC1_安全与模式", "FC2_产线步序与超时", "FC3_执行器防撞控制", "FC90_报警汇总", "FC91_指示灯输出", "FC99_仿真自反馈"]),
    )
    return xml_dir, ir, io_tags, all_timers


def test_cax_aml_precheck_blocks_unsupported_drives_and_scalance(tmp_path):
    """契约1: 验证 CAx AML 预检识别 F-CPU 并全量拦截驱动器、非按键HMI及三款特定 Scalance 交换机"""
    aml_file = tmp_path / "full_line.aml"
    aml_file.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<CAEXFile FileName="full_line.aml" SchemaVersion="2.15" xmlns="http://www.dke.de/CAEX">
  <InstanceHierarchy Name="Hardware">
    <InternalElement Name="PLC_Main_1515F">
      <Attribute Name="OrderNumber"><Value>6ES7 515-2FM02-0AB0</Value></Attribute>
    </InternalElement>
    <InternalElement Name="Servo_S120">
      <Attribute Name="DeviceType"><Value>SINAMICS S120</Value></Attribute>
    </InternalElement>
    <InternalElement Name="HMI_TP1200">
      <Attribute Name="DeviceType"><Value>TP1200 Comfort Panel</Value></Attribute>
    </InternalElement>
    <InternalElement Name="SW_602"><Attribute Name="OrderNumber"><Value>6GK5 602-0BA10-2AA3</Value></Attribute></InternalElement>
    <InternalElement Name="SW_612"><Attribute Name="OrderNumber"><Value>6GK5 612-0BA10-2AA3</Value></Attribute></InternalElement>
    <InternalElement Name="SW_623"><Attribute Name="OrderNumber"><Value>6GK5 623-0BA10-2AA3</Value></Attribute></InternalElement>
  </InstanceHierarchy>
</CAEXFile>""",
        encoding="utf-8",
    )
    info = inspect_aml_hardware(str(aml_file))
    assert info["cpu_family"] == "S7-1500"
    assert info["is_failsafe_cpu"] is True
    assert info["valid"] is False
    assert len(info["unsupported_devices"]) == 5


def test_watch_and_force_table_xml_builder(tmp_path):
    """契约2: 验证多工位 PlcWatchTable 与 PlcForceTable XML 分组生成及 V16 合规校验"""
    tags = [
        {"name": "SB0_急停按钮", "logical_address": "%I0.0", "data_type": "Bool", "comment": "[2.1|-SB0|11] 急停(NC)"},
        {"name": "B1_送料缩回", "logical_address": "%I0.1", "data_type": "Bool", "comment": "[3.1|-B1|12] 送料(NO)"},
        {"name": "B3_压装上到位", "logical_address": "%I0.2", "data_type": "Bool", "comment": "[3.2|-B3|13] 压装(NO)"},
        {"name": "YV1_送料伸出", "logical_address": "%Q0.0", "data_type": "Bool", "comment": "[4.1|-YV1|21] 送料电磁阀"},
        {"name": "YV2_压装下压", "logical_address": "%Q0.1", "data_type": "Bool", "comment": "[4.2|-YV2|22] 压装电磁阀"},
        {"name": "M_步序_S10_送料", "logical_address": "%M30.0", "data_type": "Bool", "comment": "[FC2/NW1] S10"},
    ]
    res = build_station_watch_tables_from_tags(
        tags,
        str(tmp_path / "wf"),
        station_prefix_map={"OP10": "送料", "OP20": "压装"},
        timer_members=["T1_压装下压超时"],
    )
    assert len(res["watch_tables"]) == 5  # 安全与模式 + OP10 + OP20 + 全线公共 + 步序定时器
    assert len(res["force_tables"]) == 1
    for p in res["watch_tables"] + res["force_tables"]:
        errs, _ = validate_single_xml(p)
        assert not errs, f"校验失败 {p}: {errs}"


def test_drawing_parser_fallback_and_safety_critic_loop(tmp_path, monkeypatch):
    """契约3: 验证无 GEMINI_API_KEY 本地降级提取与 Safety Critic 闭环"""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    xml_dir, ir, _, _ = _build_mvp_dual_cylinder_project(tmp_path, include_b3_interlock=True)
    assert ir["extraction_mode"] == "local_deterministic_fallback"
    critic = run_local_safety_critic(str(xml_dir), ir)
    assert critic["passed"] is True, f"Safety Critic 报错: {critic}"


def test_s7_1200_vs_1500_simulation_routing_and_mcp_safety_gates(tmp_path):
    """契约4: 验证 S7-1200 vs S7-1500 仿真后端分流、FastMCP 下载/强制门禁与 OT 安全防注入"""
    mock_dll = tmp_path / "Siemens.Simatic.Simulation.Runtime.Api.x64.dll"
    mock_dll.write_text("mock", encoding="utf-8")

    # S7-1200 即使存在 PLCSIM Advanced DLL 也强制切换 FC_SIM_FEEDBACK_LOOP
    assert detect_simulation_backend("CPU 1214C DC/DC/DC", str(mock_dll)) == "FC_SIM_FEEDBACK_LOOP"
    # S7-1500 有 DLL 时走 PLCSIM_ADVANCED_API，无 DLL 时回退 FC_SIM_FEEDBACK_LOOP
    assert detect_simulation_backend("CPU 1515-2 PN", str(mock_dll)) == "PLCSIM_ADVANCED_API"
    assert detect_simulation_backend("CPU 1515-2 PN", str(tmp_path / "missing.dll")) == "FC_SIM_FEEDBACK_LOOP"

    # MCP 高危操作门禁拦截测试
    r1 = tia_connect.tia_download_to_plc(human_approved=False, confirm_online_token="1234567890abcdef")
    assert r1["Success"] is False and r1["BlockedBySafetyGate"] is True
    assert r1["GateReason"] == "HUMAN_APPROVAL_REQUIRED"

    r2 = tia_connect.tia_download_to_plc(human_approved=True, confirm_online_token="bad")
    assert r2["Success"] is False and r2["GateReason"] == "MISSING_COMPARE_ONLINE_TOKEN"

    r3 = tia_connect.tia_download_to_plc(
        human_approved=True,
        confirm_online_token="1234567890abcdef",
        is_failsafe_plc=True,
        first_manual_download_done=False,
    )
    assert r3["Success"] is False and r3["GateReason"] == "FAILSAFE_FIRST_MANUAL_DOWNLOAD_REQUIRED"

    r4 = tia_connect.tia_live_data_channel(action="force_output", values={"%Q0.0": True}, human_approved_force=False)
    assert r4["Success"] is False and r4["GateReason"] == "FORCE_OUTPUT_HUMAN_APPROVAL_REQUIRED"

    # OT 命令注入与路径穿越拦截 + 敏感字段脱敏测试
    r5 = tia_connect.tia_import_cax_aml(aml_path="C:\\test.aml; powershell -enc abc")
    assert r5["Success"] is False and r5.get("BlockedBySecurityGuard") is True

    r6 = tia_connect.tia_manage_watch_force_tables(watch_xml_dir="C:\\proj\\..\\..\\Windows\\System32")
    assert r6["Success"] is False and r6.get("BlockedBySecurityGuard") is True

    # EPLAN 静默导出工具防注入、路径穿越与白名单校验
    r7 = tia_connect.eplan_export_project_assets(action="export-all", eplan_project_path="C:\\p.elk & whoami", output_dir=str(tmp_path))
    assert r7["Success"] is False and r7.get("BlockedBySecurityGuard") is True

    r8 = tia_connect.eplan_export_project_assets(action="export-all", eplan_project_path=str(tmp_path / "demo.elk"), output_dir="C:\\proj\\..\\..\\Windows")
    assert r8["Success"] is False and r8.get("BlockedBySecurityGuard") is True

    r9 = tia_connect.eplan_export_project_assets(action="rm-rf", eplan_project_path=str(tmp_path / "demo.elk"), output_dir=str(tmp_path))
    assert r9["Success"] is False and r9.get("BlockedBySecurityGuard") is True

    r10 = tia_connect.eplan_export_project_assets(action="export-tables", eplan_project_path=str(tmp_path / "demo.elk"), output_dir=str(tmp_path), plc_scheme="bad/scheme")
    assert r10["Success"] is False and r10.get("BlockedBySecurityGuard") is True

    r11 = tia_connect.eplan_export_project_assets(action="inspect-eplan")
    assert r11.get("Action") == "inspect-eplan" and "W3uFound" in r11

    sanitized = tia_connect.sanitize_for_log({"ConfirmOnlineToken": "tok_99999999", "MasterSecret": "sec123", "Ok": True})
    assert sanitized["ConfirmOnlineToken"] == "***REDACTED***"
    assert sanitized["MasterSecret"] == "***REDACTED***"
    assert sanitized["Ok"] is True


def test_single_station_dual_cylinder_safety_door_mvp_closed_loop(tmp_path):
    """契约5: 验证单工位双气缸+安全门联锁 MVP 工程通过静态校验/Safety Critic 并跑通三项离散扫描闭环评测"""
    xml_dir, ir, _, _ = _build_mvp_dual_cylinder_project(tmp_path, include_b3_interlock=True)

    static_res = validate_project_xmls(str(xml_dir))
    assert static_res.ok, f"MVP 静态校验失败: {static_res.errors}"
    assert not static_res.warnings, f"MVP 存在告警: {static_res.warnings}"

    critic = run_local_safety_critic(str(xml_dir), ir)
    assert critic["passed"] is True, f"MVP Safety Critic 失败: {critic}"

    sim_res = run_closed_loop_evaluation(str(xml_dir), ir, cpu_model="CPU 1214C DC/DC/DC")
    assert sim_res["backend"] == "FC_SIM_FEEDBACK_LOOP"
    assert sim_res["task_success_rate"] == 1.0, f"步序未全跑通: {sim_res}"
    assert sim_res["returned_idle"] is True
    assert sim_res["timeout_alarm_caught"] is True, f"超时报警未触发: {sim_res}"
    assert sim_res["safety_violation_count"] == 0, f"发生安全违规: {sim_res['violations_detail']}"
    assert sim_res["passed"] is True


def test_closed_loop_simulator_genuinely_catches_injected_safety_bypass(tmp_path):
    """反作弊真实验算证明: 故意删去 YV1 主干路的 B3_2号压装气缸上到位 防撞触点，离散仿真引擎必须真实捕获安全违规"""
    xml_dir, ir, _, _ = _build_mvp_dual_cylinder_project(tmp_path, include_b3_interlock=False)
    sim_res = run_closed_loop_evaluation(str(xml_dir), ir, cpu_model="CPU 1214C DC/DC/DC")
    assert sim_res["safety_violation_count"] > 0
    assert sim_res["passed"] is False
    assert any(
        v.get("actuator") == "YV1_1号送料气缸伸出" and v.get("missing_permissive") == "B3_2号压装气缸上到位"
        for v in sim_res["violations_detail"]
    )


def test_lad_builder_fb_call_idb_and_line_handover_networks(tmp_path):
    """验证 build_fb_call_network、build_instance_db_xml 与 build_line_handover_networks 合规性"""
    import shutil
    from conftest import SKILL_ROOT
    from block_fingerprint import compare as fp_compare
    from lad_builder import (
        build_fb_call_network,
        build_instance_db_xml,
        build_line_handover_networks,
    )

    tag_dir = tmp_path / "02_变量表"
    xml_dir = tmp_path / "03_梯形图XML"
    tag_dir.mkdir(parents=True, exist_ok=True)
    xml_dir.mkdir(parents=True, exist_ok=True)

    # 复制模板中的 FB_StationModeManager.xml 与 UDT_LineHandover.xml
    shutil.copy2(SKILL_ROOT / "templates" / "FB_StationModeManager.xml", xml_dir / "FB_StationModeManager.xml")
    shutil.copy2(SKILL_ROOT / "templates" / "UDT_LineHandover.xml", xml_dir / "UDT_LineHandover.xml")

    io_tags = [
        {"name": "SB0_急停按钮", "data_type": "Bool", "logical_address": "%I0.0", "comment": "[1.1|-SB0|11] 急停(NC)"},
        {"name": "SQ1_安全门联锁", "data_type": "Bool", "logical_address": "%I0.1", "comment": "[1.2|-SQ1|21] 安全门(NC)"},
        {"name": "SA1_手自动旋钮", "data_type": "Bool", "logical_address": "%I0.2", "comment": "[1.3|-SA1|13] 手自动(NO)"},
        {"name": "SB1_自动启动", "data_type": "Bool", "logical_address": "%I0.3", "comment": "[1.4|-SB1|13] 启动(NO)"},
        {"name": "SB2_回原位", "data_type": "Bool", "logical_address": "%I0.4", "comment": "[1.5|-SB2|13] 回原位(NO)"},
        {"name": "SB3_故障复位", "data_type": "Bool", "logical_address": "%I0.5", "comment": "[1.6|-SB3|13] 复位(NO)"},
        {"name": "B1_下游进料到位", "data_type": "Bool", "logical_address": "%I0.6", "comment": "[2.1|-B1|BK] 进料到位(NO)"},
        {"name": "YV1_移栽传输阀", "data_type": "Bool", "logical_address": "%Q0.0", "comment": "[3.1|-YV1|21] 移栽阀"},
    ]
    m_tags = default_m_tags() + [
        {"name": "M_自动模式", "data_type": "Bool", "logical_address": "%M10.2", "comment": "[OB1/NW1] 自动模式"},
        {"name": "M_手动模式", "data_type": "Bool", "logical_address": "%M10.3", "comment": "[OB1/NW1] 手动模式"},
        {"name": "M_回原位请求", "data_type": "Bool", "logical_address": "%M10.4", "comment": "[OB1/NW1] 回原位请求"},
        {"name": "M_故障复位脉冲", "data_type": "Bool", "logical_address": "%M10.5", "comment": "[OB1/NW1] 故障复位脉冲"},
        {"name": "M_工位在原点", "data_type": "Bool", "logical_address": "%M10.6", "comment": "[FC10/NW1] 原点"},
        {"name": "M_上游加工完成", "data_type": "Bool", "logical_address": "%M11.0", "comment": "[FC10/NW1] 上游完成"},
        {"name": "M_下游就绪", "data_type": "Bool", "logical_address": "%M11.1", "comment": "[FC10/NW1] 下游就绪"},
        {"name": "M_交接请求输出", "data_type": "Bool", "logical_address": "%M11.2", "comment": "[FC10/NW4] 交接输出请求"},
        {"name": "M_报警汇总", "data_type": "Bool", "logical_address": "%M119.7", "comment": "[FC90/NW1] 报警汇总"},
    ]
    _write_tag_table_xml("IO变量表", io_tags, str(tag_dir / "TagTable_IO变量表.xml"))
    _write_tag_table_xml("M辅助继电器表", m_tags, str(tag_dir / "TagTable_M辅助继电器表.xml"))

    # 生成工位交接 GlobalDB (含 UDT_LineHandover 成员结构体)
    handover_db_xml = """<?xml version="1.0" encoding="utf-8"?>
<Document>
  <Engineering version="V16" />
  <SW.Blocks.GlobalDB ID="0">
    <AttributeList>
      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v4">
        <Section Name="Static">
          <Member Name="OP10_OP20" Datatype="&quot;UDT_LineHandover&quot;">
            <Sections><Section Name="None">
              <Member Name="ReqToNext" Datatype="Bool" />
              <Member Name="RdyFromNext" Datatype="Bool" />
              <Member Name="TransferActive" Datatype="Bool" />
              <Member Name="TransferDone" Datatype="Bool" />
              <Member Name="FaultStop" Datatype="Bool" />
            </Section></Sections>
          </Member>
        </Section>
      </Sections></Interface>
      <MemoryLayout>Optimized</MemoryLayout>
      <Name>DB_工位交接</Name>
      <Number>20</Number>
      <ProgrammingLanguage>DB</ProgrammingLanguage>
    </AttributeList>
  </SW.Blocks.GlobalDB>
</Document>"""
    write_xml_file(str(xml_dir / "DB_工位交接.xml"), handover_db_xml)

    # 1. 测试 InstanceDB 生成
    idb_xml = build_instance_db_xml(
        idb_name="iDB_OP10_Mode",
        idb_number=10,
        fb_name="FB_StationModeManager",
        comment="OP10工位模式管理背景数据块",
        input_members=[("I_急停按钮_NC", "Bool"), ("I_安全门联锁_NC", "Bool"), ("I_手自动选择", "Bool"), ("I_自动启动", "Bool"), ("I_回原位按钮", "Bool"), ("I_故障复位", "Bool"), ("I_工位在原点", "Bool"), ("I_报警汇总", "Bool")],
        output_members=[("Q_安全条件满足", "Bool"), ("Q_自动模式", "Bool"), ("Q_手动模式", "Bool"), ("Q_回原位请求", "Bool"), ("Q_故障复位脉冲", "Bool")],
        static_members=[("stat_自动运行保持", "Bool"), ("stat_复位边沿", "Bool")],
    )
    idb_path = xml_dir / "iDB_OP10_Mode.xml"
    write_xml_file(str(idb_path), idb_xml)

    # 2. 测试 build_line_handover_networks 生成四步闭环握手与执行器驱动
    ho_nws = build_line_handover_networks(
        start_nw_index=1,
        upstream_station="OP10",
        downstream_station="OP20",
        handover_prefix="DB_工位交接.OP10_OP20",
        upstream_done_var="M_上游加工完成",
        downstream_ready_var="M_下游就绪",
        downstream_arrived_var="B1_下游进料到位",
        transfer_actuator_req_var="M_交接请求输出",
    )
    assert len(ho_nws) == 4
    ho_nws.append(
        build_manual_auto_interlocked_actuator_network(
            5,
            "NW5 移栽传输阀驱动",
            "【功能】由交接握手请求驱动传输阀",
            coil_var="YV1_移栽传输阀",
            manual_trigger_var="SB2_回原位",
            auto_request_var="M_交接请求输出",
        )
    )
    ho_nws.append(build_simple_series_network(6, "NW6 占位信号", "【功能】原点与就绪占位", [{"name": "M_安全条件满足", "negated": False}], "M_工位在原点"))
    ho_nws.append(build_simple_series_network(7, "NW7 完成占位", "【功能】上游完成占位", [{"name": "M_工位在原点", "negated": False}], "M_上游加工完成"))
    ho_nws.append(build_simple_series_network(8, "NW8 下游就绪", "【功能】下游就绪占位", [{"name": "M_安全条件满足", "negated": False}], "M_下游就绪"))
    ho_nws.append(build_simple_series_network(9, "NW9 报警汇总", "【功能】安全断开报警", [{"name": "M_安全条件满足", "negated": True}], "M_报警汇总"))
    write_xml_file(str(xml_dir / "FC10_工位交接.xml"), build_fc_xml("FC10_工位交接", 10, ho_nws, "OP10至OP20交接握手"))

    # 3. 测试 build_ob1_xml 混调 FB (build_fb_call_network) + FC
    ob1_xml = build_ob1_xml([
        {
            "name": "FB_StationModeManager",
            "block_type": "FB",
            "instance": "iDB_OP10_Mode",
            "parameters": [
                {"name": "I_急停按钮_NC", "section": "Input", "type": "Bool", "value": "SB0_急停按钮"},
                {"name": "I_安全门联锁_NC", "section": "Input", "type": "Bool", "value": "SQ1_安全门联锁"},
                {"name": "I_手自动选择", "section": "Input", "type": "Bool", "value": "SA1_手自动旋钮"},
                {"name": "I_自动启动", "section": "Input", "type": "Bool", "value": "SB1_自动启动"},
                {"name": "I_回原位按钮", "section": "Input", "type": "Bool", "value": "SB2_回原位"},
                {"name": "I_故障复位", "section": "Input", "type": "Bool", "value": "SB3_故障复位"},
                {"name": "I_工位在原点", "section": "Input", "type": "Bool", "value": "M_工位在原点"},
                {"name": "I_报警汇总", "section": "Input", "type": "Bool", "value": "M_报警汇总"},
                {"name": "Q_安全条件满足", "section": "Output", "type": "Bool", "value": "M_安全条件满足"},
                {"name": "Q_自动模式", "section": "Output", "type": "Bool", "value": "M_自动模式"},
                {"name": "Q_手动模式", "section": "Output", "type": "Bool", "value": "M_手动模式"},
                {"name": "Q_回原位请求", "section": "Output", "type": "Bool", "value": "M_回原位请求"},
                {"name": "Q_故障复位脉冲", "section": "Output", "type": "Bool", "value": "M_故障复位脉冲"},
            ],
        },
        "FC10_工位交接",
    ])
    write_xml_file(str(xml_dir / "OB1_Main.xml"), ob1_xml)

    res = validate_project_xmls(str(xml_dir))
    assert res.ok, f"FB Call / IDB / 工位交接校验报错: {res.errors}"
    assert fp_compare(str(idb_path), str(idb_path)) == []


def test_to_params_parser_and_mcp_configure_to_guard(tmp_path):
    """验证 TO_Params.xlsx 往返解析校验与 tia_configure_technology_objects 防注入门禁"""
    from cax_hardware_builder import build_to_params_template, parse_to_params_xlsx

    to_xlsx = tmp_path / "TO_Params.xlsx"
    build_to_params_template(
        [{"device_name": "Servo_Axis_Z", "order_number": "6SL3210-5FE10-4UF0", "category": "Drive"}],
        str(to_xlsx),
    )
    axes = parse_to_params_xlsx(str(to_xlsx))
    assert len(axes) == 1
    assert axes[0]["axis_name"] == "Servo_Axis_Z"
    assert axes[0]["lead_pitch_mm"] == 10.0

    # MCP 防路径穿越拦截
    blocked = tia_connect.tia_configure_technology_objects(to_params_xlsx_path="C:\\proj\\..\\..\\secret.xlsx")
    assert blocked["Success"] is False and blocked.get("BlockedBySecurityGuard") is True


def test_dynamic_ir_driven_simulation_without_hardcoded_keywords(tmp_path):
    """验证完全不依赖 '送料/压装/上到位/下到位' 硬编码词根的新工位（旋转打标台）可通过 IR 动态驱动三项闭环验证"""
    plc_p = tmp_path / "rotary_io.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["PLC地址", "信号类型", "元器件代号", "元器件端子/线号", "页码.列号", "NO/NC", "中文功能", "是否备用"])
    ws.append(["%I0.0", "DI", "-SB0", "11-12/101", "2.1", "NC", "急停按钮", "否"])
    ws.append(["%I0.1", "DI", "-SQ1", "21-22/102", "2.2", "NC", "防护罩锁紧", "否"])
    ws.append(["%I0.2", "DI", "-SA1", "13-14/103", "2.3", "NO", "手自动选择", "否"])
    ws.append(["%I0.3", "DI", "-SB1", "13-14/104", "2.4", "NO", "自动启动按钮", "否"])
    ws.append(["%I0.4", "DI", "-SB2", "13-14/105", "2.5", "NO", "故障复位按钮", "否"])
    ws.append(["%I0.5", "DI", "-S1", "BK/106", "3.1", "NO", "转台零位", "否"])
    ws.append(["%I0.6", "DI", "-S2", "BK/107", "3.2", "NO", "转台工作位", "否"])
    ws.append(["%I0.7", "DI", "-S3", "BK/108", "3.3", "NO", "打标头高位", "否"])
    ws.append(["%I1.0", "DI", "-SB3", "13-14/109", "2.6", "NO", "手动转台点动", "否"])
    ws.append(["%Q0.0", "DO", "-YV1", "A1/201", "4.1", "电磁阀", "转台旋转定位", "否"])
    ws.append(["%Q0.1", "DO", "-HL1", "X1/202", "4.2", "指示灯", "故障报警指示灯", "否"])
    wb.save(plc_p)

    spec_p = tmp_path / "rotary_spec.md"
    spec_p.write_text(
        "# OP20 激光打标旋转台工艺\n\n"
        "## 1. 原点定义\n"
        "- `S1_转台零位` = 1\n"
        "- `S3_打标头高位` = 1\n\n"
        "## 2. 自动循环步序表\n"
        "| 步号 | 步名称 | 进入/触发条件 | 本步动作请求 | 转移完成条件 | 超时时间 | 超时报警变量 |\n"
        "|---|---|---|---|---|---|---|\n"
        "| S10 | 转台旋转到位 | `SB1_自动启动按钮` | `M_步序_S10_转台旋转` | `S2_转台工作位` | `T#3S` | `M_报警_转台超时` |\n\n"
        "## 3. 安全联锁矩阵\n"
        "| 序号 | 被保护执行器 (%Q 变量名) | 必须满足的允许条件 | 必须断开的互斥线圈 | 防呆理由说明 |\n"
        "|---|---|---|---|---|\n"
        "| 1 | `YV1_转台旋转定位` | `M_安全条件满足`, `S3_打标头高位` | — | 打标头未抬至高位严禁转台分度 |\n",
        encoding="utf-8",
    )

    ir = extract_process_ir_from_drawings(
        xlsx_io_path=str(plc_p),
        spec_paths=[str(spec_p)],
    )
    io_tags = parse_eplan_plc_table(str(plc_p))

    tag_dir = tmp_path / "02_变量表"
    xml_dir = tmp_path / "03_梯形图XML"
    tag_dir.mkdir()
    xml_dir.mkdir()

    m_tags = default_m_tags() + [
        {"name": "M_自动模式", "data_type": "Bool", "logical_address": "%M10.2", "comment": "[FC1/NW3] 自动模式"},
        {"name": "M_手动模式", "data_type": "Bool", "logical_address": "%M10.3", "comment": "[FC1/NW4] 手动模式"},
        {"name": "M_步序_S10_转台旋转", "data_type": "Bool", "logical_address": "%M30.0", "comment": "[FC2/NW1] S10"},
        {"name": "M_报警_转台超时", "data_type": "Bool", "logical_address": "%M100.0", "comment": "[FC2/NW2] 转台超时"},
        {"name": "M_报警汇总", "data_type": "Bool", "logical_address": "%M119.7", "comment": "[FC90/NW1] 报警汇总"},
        {"name": "M_仿真模式激活", "data_type": "Bool", "logical_address": "%M19.0", "comment": "[FC99/NW1] 仿真使能"},
    ]
    _write_tag_table_xml("IO变量表", io_tags, str(tag_dir / "TagTable_IO变量表.xml"))
    _write_tag_table_xml("M辅助继电器表", m_tags, str(tag_dir / "TagTable_M辅助继电器表.xml"))

    fc1_nws = [
        build_simple_series_network(1, "NW1 急停", "【功能】急停扫描", [{"name": "SB0_急停按钮", "negated": False}], "M_急停正常"),
        build_simple_series_network(2, "NW2 安全汇总", "【功能】急停与防护罩", [{"name": "M_急停正常", "negated": False}, {"name": "SQ1_防护罩锁紧", "negated": False}], "M_安全条件满足"),
        build_simple_series_network(3, "NW3 自动", "【功能】自动模式", [{"name": "SA1_手自动选择", "negated": False}], "M_自动模式"),
        build_simple_series_network(4, "NW4 手动", "【功能】手动模式", [{"name": "SA1_手自动选择", "negated": True}], "M_手动模式"),
    ]
    write_xml_file(str(xml_dir / "FC1_安全与模式.xml"), build_fc_xml("FC1_安全与模式", 1, fc1_nws))

    fc2_nws = [
        build_step_latch_network(
            1, "NW1 S10 转台旋转", "【功能】启动后转台旋转直到 S2 到位",
            step_var="M_步序_S10_转台旋转",
            trigger_contacts=[
                {"name": "SB1_自动启动按钮", "negated": False},
                {"name": "S1_转台零位", "negated": False},
                {"name": "S3_打标头高位", "negated": False},
            ],
            next_step_vars=[],
            extra_hold_contacts=[{"name": "S2_转台工作位", "negated": True}],
        ),
        build_ton_timer_network(
            2, "NW2 转台超时报警", "【功能】S10超过2S未到S2工作位触发报警",
            in_contacts=[{"name": "M_步序_S10_转台旋转", "negated": False}, {"name": "S2_转台工作位", "negated": True}],
            timer_db_member="T1_转台超时", time_pt="T#2S", coil_var="M_报警_转台超时", coil_type="SCoil",
        ),
        build_simple_series_network(3, "NW3 报警复位", "【功能】复位报警", [{"name": "SB2_故障复位按钮", "negated": False}], "M_报警_转台超时", coil_type="RCoil"),
    ]
    write_xml_file(str(xml_dir / "FC2_步序.xml"), build_fc_xml("FC2_步序", 2, fc2_nws))

    fc3_nws = [
        build_manual_auto_interlocked_actuator_network(
            1, "NW1 转台定位阀", "【功能】串入打标头高位 S3 防撞",
            coil_var="YV1_转台旋转定位",
            manual_trigger_var="SB3_手动转台点动",
            auto_request_var="M_步序_S10_转台旋转",
            interlock_contacts=[{"name": "S3_打标头高位", "negated": False}],
        )
    ]
    write_xml_file(str(xml_dir / "FC3_执行器.xml"), build_fc_xml("FC3_执行器", 3, fc3_nws))

    fc90_nws = [build_or_branches_series_network(1, "NW1 报警汇总", "【功能】报警汇总", [[{"name": "M_报警_转台超时", "negated": False}], [{"name": "M_安全条件满足", "negated": True}]], [], "M_报警汇总")]
    write_xml_file(str(xml_dir / "FC90_报警汇总.xml"), build_fc_xml("FC90_报警汇总", 90, fc90_nws))
    fc91_nws = [build_simple_series_network(1, "NW1 报警灯", "【功能】故障灯", [{"name": "M_报警汇总", "negated": False}], "HL1_故障报警指示灯")]
    write_xml_file(str(xml_dir / "FC91_指示灯.xml"), build_fc_xml("FC91_指示灯", 91, fc91_nws))

    # 手工构造针对旋转台的仿真自反馈网络（验证非气缸命名的通用仿真闭环）
    fc99_nws = [
        build_ton_timer_network(1, "NW1 转台旋转到达S2", "【仿真】YV1得电1S后置位S2", [{"name": "M_仿真模式激活", "negated": False}, {"name": "YV1_转台旋转定位", "negated": False}], "T_Sim_S2", "T#1S", "S2_转台工作位", coil_type="SCoil"),
        build_ton_timer_network(2, "NW2 转台回零S1", "【仿真】YV1失电1S后复位S2", [{"name": "M_仿真模式激活", "negated": False}, {"name": "YV1_转台旋转定位", "negated": True}], "T_Sim_S1", "T#1S", "S2_转台工作位", coil_type="RCoil"),
    ]
    write_xml_file(str(xml_dir / "FC99_仿真自反馈.xml"), build_fc_xml("FC99_仿真自反馈", 99, fc99_nws))
    write_xml_file(str(xml_dir / "DB_全局定时器.xml"), build_timer_db_xml("全局定时器", ["T1_转台超时", "T_Sim_S2", "T_Sim_S1"]))
    write_xml_file(str(xml_dir / "OB1_Main.xml"), build_ob1_xml(["FC1_安全与模式", "FC2_步序", "FC3_执行器", "FC90_报警汇总", "FC91_指示灯", "FC99_仿真自反馈"]))

    sim_res = run_closed_loop_evaluation(str(xml_dir), ir, cpu_model="CPU 1214C DC/DC/DC")
    assert sim_res["blocked_sensor"] == "S2_转台工作位"
    assert sim_res["interlock_block_tag"] == "S3_打标头高位"
    assert sim_res["passed"] is True, f"动态IR驱动闭环失败: {sim_res}"

    # 测试 tia_live_data_channel 对 XML 目录实时仿真一步快照的支持
    snap_res = tia_connect.tia_live_data_channel(action="read_snapshot", snapshot_file=str(xml_dir), tags=["M_安全条件满足"])
    assert snap_res["Success"] is True and snap_res["Source"] == "LadScanCycleSimulatorLiveStep"
