# -*- coding: utf-8 -*-
"""产线级 EPLAN 报表解析、工艺步序自锁与机械防撞联锁回归测试"""

import json
from pathlib import Path
import openpyxl

from conftest import default_m_tags
from eplan_parser import (
    generate_reconciliation_report,
    parse_eplan_bom_table,
    parse_eplan_plc_table,
    parse_process_spec,
)
from lad_builder import (
    build_fc_xml,
    build_manual_auto_interlocked_actuator_network,
    build_ob1_xml,
    build_or_branches_series_network,
    build_simple_series_network,
    build_step_latch_network,
    build_timer_db_xml,
    write_xml_file,
)
from tag_builder import _write_tag_table_xml, validate_tag_item
from validate_lad_xml import validate_project_xmls


def _create_sample_eplan_xlsx(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["PLC地址", "信号类型", "模块/槽位", "卡片端子", "元器件代号", "元器件端子/线号", "页码.列号", "NO/NC", "中文功能", "是否备用"])
    ws.append(["%I0.0", "DI", "槽位1", "DIa.0", "-SB0", "11-12/101", "2.1", "NC", "急停按钮", "否"])
    ws.append(["%I0.1", "DI", "槽位1", "DIa.1", "-FR1", "95-96/102", "2.2", "NC", "输送电机热继", "否"])
    ws.append(["%I0.2", "DI", "槽位1", "DIa.2", "-SA1", "13-14/103", "2.3", "NO", "手自动选择", "否"])
    ws.append(["%I0.3", "DI", "槽位1", "DIa.3", "-SB1", "13-14/104", "2.4", "NO", "自动启动按钮", "否"])
    ws.append(["%I0.4", "DI", "槽位1", "DIa.4", "-SB2", "13-14/105", "2.5", "NO", "手动输送点动", "否"])
    ws.append(["%I0.5", "DI", "槽位1", "DIa.5", "-B1", "BK/106", "3.1", "NO", "来料检测光电", "否"])
    ws.append(["%I0.6", "DI", "槽位1", "DIa.6", "-B2", "BK/107", "3.2", "NO", "推料缩回到位", "否"])
    ws.append(["%I0.7", "DI", "槽位1", "DIa.7", "-B4", "BK/108", "3.3", "NO", "压装上到位", "否"])
    ws.append(["%I1.5", "DI", "槽位1", "DIb.5", "", "", "", "", "备用通道", "是"])
    ws.append(["%Q0.0", "DO", "槽位1", "DQa.0", "-KM1", "A1/201", "4.1", "线圈", "上料输送电机", "否"])
    ws.append(["%Q0.1", "DO", "槽位1", "DQa.1", "-YV1", "A1/202", "4.2", "电磁阀", "推料气缸伸出", "否"])
    ws.append(["%Q0.2", "DO", "槽位1", "DQa.2", "-YV2", "A1/203", "4.3", "电磁阀", "压装气缸下压", "否"])
    ws.append(["%Q0.3", "DO", "槽位1", "DQa.3", "-YV3", "A1/204", "4.4", "电磁阀", "压装气缸回升", "否"])
    wb.save(path)


def test_parse_eplan_plc_table_xlsx_and_csv(tmp_path):
    xlsx_p = tmp_path / "plc.xlsx"
    _create_sample_eplan_xlsx(xlsx_p)
    items = parse_eplan_plc_table(str(xlsx_p))
    assert len(items) == 13
    for it in items:
        errs = validate_tag_item(it)
        assert not errs, f"生成的点表项未通过 tag_builder 校验: {it}, errors={errs}"

    # 校验备用点与安全信号格式
    spare = [x for x in items if x["is_spare"]]
    assert len(spare) == 1
    estop = [x for x in items if x["logical_address"] == "%I0.0"][0]
    assert estop["name"] == "SB0_急停按钮"
    assert estop["comment"].startswith("[2.1 | -SB0 | 11-12/101]")
    assert "(NC)" in estop["comment"]

    # 同时测试 CSV 解析兼容性
    csv_p = tmp_path / "plc.csv"
    csv_p.write_text(
        "PLC地址,信号类型,元器件代号,端子/线号,页码.列号,NO/NC,中文功能\n"
        "%I0.0,DI,-SB0,11-12/101,2.1,NC,急停按钮\n"
        "X9,DI,-SB9,13-14/109,2.9,NO,未知地址点\n",
        encoding="utf-8-sig",
    )
    csv_items = parse_eplan_plc_table(str(csv_p))
    assert len(csv_items) == 2
    assert csv_items[0]["needs_confirm"] is False
    assert csv_items[1]["needs_confirm"] is True


def test_reconciliation_catches_ac_coil_and_parses_process_spec(tmp_path):
    plc_p = tmp_path / "plc.xlsx"
    _create_sample_eplan_xlsx(plc_p)

    bom_p = tmp_path / "bom.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["元器件代号", "名称/功能", "型号规格", "电压等级", "控制类型", "接线方式"])
    ws.append(["-KM1", "输送电机接触器", "LC1D09M7C", "220V AC", "接触器", "交流线圈"])
    ws.append(["-YV1", "推料电磁阀", "SY5120-5LZD", "24V DC", "单电控电磁阀", "DC24V"])
    wb.save(bom_p)

    spec_p = tmp_path / "spec.md"
    spec_p.write_text(
        "# 产线工艺与联锁\n\n"
        "## 1. 原点定义\n"
        "- `B2_推料缩回到位` = 1\n"
        "- `B4_压装上到位` = 1\n\n"
        "## 2. 自动循环步序表\n"
        "| 步号 | 步名称 | 进入/触发条件 | 本步动作请求 | 转移完成条件 | 超时时间 | 超时报警变量 |\n"
        "|---|---|---|---|---|---|---|\n"
        "| S10 | 来料输送 | `SB1_自动启动按钮` | `M_请求_输送电机` | `B1_来料检测光电` | `T#8S` | `M_报警_输送超时` |\n\n"
        "## 3. 机械防呆与安全联锁矩阵\n"
        "| 序号 | 被保护执行器 (%Q 变量名) | 必须满足的允许条件 | 必须断开的互斥线圈 | 防呆理由说明 |\n"
        "|---|---|---|---|---|\n"
        "| 1 | `KM1_上料输送电机` | `M_安全条件满足`, `FR1_输送电机热继`, `B4_压装上到位` | — | 防撞压头 |\n",
        encoding="utf-8",
    )

    env_json = tmp_path / "env.json"
    env_json.write_text(
        json.dumps({
            "Project": {
                "Name": "测试产线",
                "CpuModel": "PLC_1 (OrderNumber:6ES7 214-1AG40-0XB0/V4.4)",
                "HardwareModules": [
                    {
                        "Name": "DI 14/DQ 10_1",
                        "PositionNumber": 1,
                        "IoRanges": [
                            {"IoType": "Input", "StartAddress": 0, "EffectiveBits": 14},
                            {"IoType": "Output", "StartAddress": 0, "EffectiveBits": 10},
                        ],
                    }
                ],
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    bom = parse_eplan_bom_table(str(bom_p))
    assert "KM1" in bom and bom["KM1"]["voltage"] == "220V AC"
    spec = parse_process_spec(str(spec_p))
    assert len(spec["origin_conditions"]) == 2
    assert len(spec["steps"]) == 1
    assert len(spec["interlocks"]) == 1

    rep = generate_reconciliation_report(
        project_name="测试产线",
        plc_table_path=str(plc_p),
        bom_table_path=str(bom_p),
        process_spec_paths=[str(spec_p)],
        tia_status_json_path=str(env_json),
    )
    assert rep["hw_check"]["mismatchCount"] == 0
    assert any("电气规格冲突" in a for a in rep["red_alerts"])
    assert not rep["spec_warnings"]


def test_production_line_step_and_interlock_lad_passes_validation(tmp_path):
    """完整构造步序自锁 + 手自动机械防撞联锁执行器 FC，验证通过 validate_project_xmls 0错误0告警"""
    plc_p = tmp_path / "plc.xlsx"
    _create_sample_eplan_xlsx(plc_p)
    io_items = parse_eplan_plc_table(str(plc_p))

    m_tags = default_m_tags() + [
        {"name": "M_自动模式", "data_type": "Bool", "logical_address": "%M10.2", "comment": "[FC1/NW3] 自动模式"},
        {"name": "M_手动模式", "data_type": "Bool", "logical_address": "%M10.3", "comment": "[FC1/NW4] 手动模式"},
        {"name": "M_报警_流程异常", "data_type": "Bool", "logical_address": "%M100.0", "comment": "[FC2/NW1] 报警"},
        {"name": "M_报警汇总", "data_type": "Bool", "logical_address": "%M119.7", "comment": "[FC90/NW1] 汇总"},
        {"name": "M_步序_S10_来料输送", "data_type": "Bool", "logical_address": "%M30.0", "comment": "[FC2/NW2] S10"},
        {"name": "M_步序_S20_推料进模", "data_type": "Bool", "logical_address": "%M30.1", "comment": "[FC2/NW3] S20"},
        {"name": "M_请求_推料伸出", "data_type": "Bool", "logical_address": "%M31.0", "comment": "[FC2/NW4] 请求推料"},
        {"name": "M_请求_压装下压", "data_type": "Bool", "logical_address": "%M31.1", "comment": "[FC2/NW5] 请求下压"},
        {"name": "M_请求_压装回升", "data_type": "Bool", "logical_address": "%M31.2", "comment": "[FC2/NW6] 请求回升"},
    ]

    tag_dir = tmp_path / "02_变量表"
    xml_dir = tmp_path / "03_梯形图XML"
    tag_dir.mkdir(parents=True, exist_ok=True)
    xml_dir.mkdir(parents=True, exist_ok=True)

    _write_tag_table_xml("IO变量表", io_items, str(tag_dir / "TagTable_IO变量表.xml"))
    _write_tag_table_xml("M辅助继电器表", m_tags, str(tag_dir / "TagTable_M辅助继电器表.xml"))

    fc1_nws = [
        build_simple_series_network(1, "NW1 急停", "【功能】急停扫描", [{"name": "SB0_急停按钮", "negated": False}], "M_急停正常"),
        build_simple_series_network(
            2,
            "NW2 安全汇总",
            "【功能】急停与热继汇总",
            [{"name": "M_急停正常", "negated": False}, {"name": "FR1_输送电机热继", "negated": False}],
            "M_安全条件满足",
        ),
        build_simple_series_network(3, "NW3 自动模式", "【功能】SA1=1自动", [{"name": "SA1_手自动选择", "negated": False}], "M_自动模式"),
        build_simple_series_network(4, "NW4 手动模式", "【功能】SA1=0手动", [{"name": "SA1_手自动选择", "negated": True}], "M_手动模式"),
    ]
    write_xml_file(str(xml_dir / "FC1_安全与模式.xml"), build_fc_xml("FC1_安全与模式", 1, fc1_nws))

    fc2_nws = [
        build_step_latch_network(
            1,
            "NW1 S10 来料输送步序",
            "【功能】启动后进入S10步",
            step_var="M_步序_S10_来料输送",
            trigger_contacts=[
                {"name": "SB1_自动启动按钮", "negated": False},
                {"name": "B4_压装上到位", "negated": False},
            ],
            next_step_vars=["M_步序_S20_推料进模"],
        ),
        build_step_latch_network(
            2,
            "NW2 S20 推料进模步序",
            "【功能】来料光电B1到位进入S20",
            step_var="M_步序_S20_推料进模",
            trigger_contacts=[
                {"name": "M_步序_S10_来料输送", "negated": False},
                {"name": "B1_来料检测光电", "negated": False},
            ],
            next_step_vars=[],
            extra_hold_contacts=[{"name": "B2_推料缩回到位", "negated": False}],
        ),
        build_simple_series_network(
            3,
            "NW3 推料请求",
            "【功能】S20驱动推料请求",
            [{"name": "M_步序_S20_推料进模", "negated": False}],
            "M_请求_推料伸出",
        ),
        build_simple_series_network(
            4,
            "NW4 下压请求",
            "【功能】S20完成触发下压请求",
            [{"name": "M_步序_S20_推料进模", "negated": False}, {"name": "B2_推料缩回到位", "negated": False}],
            "M_请求_压装下压",
        ),
        build_simple_series_network(
            5,
            "NW5 回升请求",
            "【功能】急停或异常测试请求",
            [{"name": "M_报警汇总", "negated": True}, {"name": "M_安全条件满足", "negated": False}],
            "M_请求_压装回升",
        ),
        build_simple_series_network(
            6,
            "NW6 故障置位",
            "【功能】热继断开报警",
            [{"name": "FR1_输送电机热继", "negated": True}],
            "M_报警_流程异常",
            coil_type="SCoil",
        ),
    ]
    write_xml_file(str(xml_dir / "FC2_产线步序.xml"), build_fc_xml("FC2_产线步序", 2, fc2_nws))

    fc3_nws = [
        build_manual_auto_interlocked_actuator_network(
            1,
            "NW1 上料输送电机控制",
            "【功能】手自动控制KM1，串入热继与压装上到位B4防撞",
            coil_var="KM1_上料输送电机",
            manual_trigger_var="SB2_手动输送点动",
            auto_request_var="M_步序_S10_来料输送",
            interlock_contacts=[
                {"name": "FR1_输送电机热继", "negated": False},
                {"name": "B4_压装上到位", "negated": False},
            ],
        ),
        build_manual_auto_interlocked_actuator_network(
            2,
            "NW2 推料气缸伸出控制",
            "【功能】手自动控制YV1，串入压装上到位B4防撞",
            coil_var="YV1_推料气缸伸出",
            manual_trigger_var="SB2_手动输送点动",
            auto_request_var="M_请求_推料伸出",
            interlock_contacts=[{"name": "B4_压装上到位", "negated": False}],
        ),
        build_manual_auto_interlocked_actuator_network(
            3,
            "NW3 压装气缸下压控制",
            "【功能】下压与回升互锁，且须推料缩回B2=1",
            coil_var="YV2_压装气缸下压",
            manual_trigger_var="SB2_手动输送点动",
            auto_request_var="M_请求_压装下压",
            interlock_contacts=[
                {"name": "B2_推料缩回到位", "negated": False},
                {"name": "YV3_压装气缸回升", "negated": True},
            ],
        ),
        build_manual_auto_interlocked_actuator_network(
            4,
            "NW4 压装气缸回升控制",
            "【功能】回升与下压互锁",
            coil_var="YV3_压装气缸回升",
            manual_trigger_var="SB2_手动输送点动",
            auto_request_var="M_请求_压装回升",
            interlock_contacts=[{"name": "YV2_压装气缸下压", "negated": True}],
        ),
    ]
    write_xml_file(str(xml_dir / "FC3_执行器防撞控制.xml"), build_fc_xml("FC3_执行器防撞控制", 3, fc3_nws))

    fc90_nws = [
        build_or_branches_series_network(
            1,
            "NW1 报警汇总",
            "【功能】汇总所有故障位",
            branches=[[{"name": "M_报警_流程异常", "negated": False}]],
            series_after=[],
            coil_var="M_报警汇总",
        )
    ]
    write_xml_file(str(xml_dir / "FC90_报警汇总.xml"), build_fc_xml("FC90_报警汇总", 90, fc90_nws))
    write_xml_file(str(xml_dir / "DB_全局定时器.xml"), build_timer_db_xml("全局定时器", ["T1_预留"]))
    write_xml_file(str(xml_dir / "OB1_Main.xml"), build_ob1_xml(["FC1_安全与模式", "FC2_产线步序", "FC3_执行器防撞控制", "FC90_报警汇总"]))

    res = validate_project_xmls(str(xml_dir))
    assert res.ok, f"产线测试项目校验失败: {res.errors}"
    assert not res.warnings, f"存在意外告警: {res.warnings}"


def test_stage1_cax_aml_precheck_and_to_params_generation(tmp_path):
    """阶段1验证: CAx AML 预检识别 S7-1200 并拦截驱动器/非按键HMI/特定Scalance交换机，自动生成 TO_Params.xlsx"""
    from cax_hardware_builder import (
        build_to_params_template,
        import_aml_to_tia,
        inspect_aml_hardware,
    )

    aml_file = tmp_path / "line_topology.aml"
    aml_file.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<CAEXFile FileName="line_topology.aml" SchemaVersion="2.15" xmlns="http://www.dke.de/CAEX">
  <InstanceHierarchy Name="Hardware">
    <InternalElement Name="PLC_OP10">
      <Attribute Name="OrderNumber"><Value>6ES7 214-1AG40-0XB0</Value></Attribute>
      <Attribute Name="PositionNumber"><Value>1</Value></Attribute>
      <RoleRequirements RefBaseRoleClassPath="AutomationMLBaseRoleClassLib/PLC" />
    </InternalElement>
    <InternalElement Name="Drive_G120_Conveyor">
      <Attribute Name="OrderNumber"><Value>6SL3210-1PE21-8UL0</Value></Attribute>
      <Attribute Name="DeviceType"><Value>SINAMICS G120</Value></Attribute>
    </InternalElement>
    <InternalElement Name="HMI_Main">
      <Attribute Name="DeviceType"><Value>TP900 Comfort Panel</Value></Attribute>
      <Attribute Name="OrderNumber"><Value>6AV2124-0JC01-0AX0</Value></Attribute>
    </InternalElement>
    <InternalElement Name="Switch_Field">
      <Attribute Name="OrderNumber"><Value>6GK5 602-0BA10-2AA3</Value></Attribute>
      <Attribute Name="DeviceType"><Value>SCALANCE S602</Value></Attribute>
    </InternalElement>
    <InternalElement Name="KeyPanel_OP10">
      <Attribute Name="DeviceType"><Value>KP8 Key Panel</Value></Attribute>
      <Attribute Name="OrderNumber"><Value>6AV3688-3AY36-0AX0</Value></Attribute>
    </InternalElement>
  </InstanceHierarchy>
</CAEXFile>""",
        encoding="utf-8",
    )

    info = inspect_aml_hardware(str(aml_file))
    assert info["cpu_family"] == "S7-1200"
    assert info["is_failsafe_cpu"] is False
    assert info["valid"] is False

    cats = {d["category"] for d in info["unsupported_devices"]}
    assert cats == {"Drive", "HMI", "ScalanceSwitch"}, f"拦截类别异常: {info['unsupported_devices']}"
    assert not any("KP8" in d["device_name"] for d in info["unsupported_devices"])

    to_xlsx = tmp_path / "TO_Params.xlsx"
    out_p = build_to_params_template(info["unsupported_devices"], str(to_xlsx))
    assert Path(out_p).is_file()
    wb = openpyxl.load_workbook(out_p)
    assert "TO_轴控与驱动参数" in wb.sheetnames
    assert wb["TO_轴控与驱动参数"].cell(row=2, column=1).value == "Drive_G120_Conveyor"

    blocked_res = import_aml_to_tia(str(aml_file))
    assert blocked_res["Success"] is False
    assert blocked_res["BlockedByCAxPrecheck"] is True
    assert Path(blocked_res["ToParamsPath"]).is_file()


def test_stage1_watch_and_force_table_builder_v16_valid(tmp_path):
    """阶段1验证: WatchTable 与 ForceTable XML 生成符合 V16 规范，通过静态校验及指纹对比"""
    from block_fingerprint import compare as fp_compare
    from validate_lad_xml import validate_single_xml
    from watch_table_builder import build_station_watch_tables_from_tags

    plc_p = tmp_path / "plc.xlsx"
    _create_sample_eplan_xlsx(plc_p)
    io_items = parse_eplan_plc_table(str(plc_p))
    tags = io_items + [
        {"name": "M_安全条件满足", "data_type": "Bool", "logical_address": "%M10.1", "comment": "[FC1/NW2] 安全条件"},
        {"name": "M_步序_S10_来料输送", "data_type": "Bool", "logical_address": "%M30.0", "comment": "[FC2/NW1] S10"},
        {"name": "M_报警_流程异常", "data_type": "Bool", "logical_address": "%M100.0", "comment": "[FC2/NW6] 报警"},
    ]

    out_dir = tmp_path / "watch_tables"
    built = build_station_watch_tables_from_tags(tags, str(out_dir), timer_members=["T1_超时"])
    assert len(built["watch_tables"]) >= 3
    assert len(built["force_tables"]) == 1

    for fp in built["watch_tables"] + built["force_tables"]:
        raw = Path(fp).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), f"缺少 UTF-8 BOM: {fp}"
        errs, audit = validate_single_xml(fp)
        assert not errs, f"监控/强制表校验报错 ({fp}): {errs}"
        assert audit["block_type"] in ("WatchTable", "ForceTable")

    force_xml = Path(built["force_tables"][0]).read_text(encoding="utf-8-sig")
    assert "%Q0.0:P" in force_xml
    assert "<ForceValue>false</ForceValue>" in force_xml
    assert fp_compare(built["force_tables"][0], built["force_tables"][0]) == []


def test_stage1_drawing_ir_fallback_and_safety_critic_catches_missing_matrix_interlock(tmp_path, monkeypatch):
    """阶段1验证: 无 GEMINI_API_KEY 自动本地降级提取 IR，生成澄清问题与 FDS.docx，Safety Critic 拦截漏联锁并验证修复"""
    from parse_engineering_drawings import (
        export_fds_docx,
        extract_process_ir_from_drawings,
        generate_clarification_questions,
        run_local_safety_critic,
    )

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    plc_p = tmp_path / "op10_io.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["PLC地址", "信号类型", "元器件代号", "元器件端子/线号", "页码.列号", "NO/NC", "中文功能", "是否备用"])
    ws.append(["%I0.0", "DI", "-SB0", "11-12/101", "2.1", "NC", "急停按钮", "否"])
    ws.append(["%I0.1", "DI", "-SQ1", "21-22/102", "2.2", "NC", "安全门联锁开关", "否"])
    ws.append(["%I0.2", "DI", "-SA1", "13-14/103", "2.3", "NO", "手自动选择", "否"])
    ws.append(["%I0.3", "DI", "-SB1", "13-14/104", "2.4", "NO", "自动启动按钮", "否"])
    ws.append(["%I0.4", "DI", "-SB2", "13-14/105", "2.5", "NO", "手动点动按钮", "否"])
    ws.append(["%I0.5", "DI", "-B1", "BK/106", "3.1", "NO", "1号送料气缸缩回到位", "否"])
    ws.append(["%I0.6", "DI", "-B2", "BK/107", "3.2", "NO", "1号送料气缸伸出到位", "否"])
    ws.append(["%I0.7", "DI", "-B3", "BK/108", "3.3", "NO", "2号压装气缸上到位", "否"])
    ws.append(["%I1.0", "DI", "-B4", "BK/109", "3.4", "NO", "2号压装气缸下到位", "否"])
    ws.append(["%Q0.0", "DO", "-YV1", "A1/201", "4.1", "电磁阀", "1号送料气缸伸出", "否"])
    ws.append(["%Q0.1", "DO", "-YV2", "A1/202", "4.2", "电磁阀", "2号压装气缸下压", "否"])
    ws.append(["%Q0.2", "DO", "-YV3", "A1/203", "4.3", "电磁阀", "2号压装气缸回升", "否"])
    wb.save(plc_p)

    matrix_p = tmp_path / "op10_safety_matrix.md"
    matrix_p.write_text(
        "# OP10 机械防呆与安全联锁矩阵\n\n"
        "| 序号 | 被保护执行器 (%Q 变量名) | 必须满足的允许条件 | 必须断开的互斥线圈 | 防呆理由说明 |\n"
        "|---|---|---|---|---|\n"
        "| 1 | `YV1_1号送料气缸伸出` | `M_安全条件满足`, `B3_2号压装气缸上到位` | — | 压头未在顶部原位禁止送料 |\n"
        "| 2 | `YV2_2号压装气缸下压` | `M_安全条件满足`, `B2_1号送料气缸伸出到位` | `YV3_2号压装气缸回升` | 送料到位才下压且双电控互斥 |\n"
        "| 3 | `YV3_2号压装气缸回升` | `M_安全条件满足` | `YV2_2号压装气缸下压` | 双电控互斥 |\n",
        encoding="utf-8",
    )

    ir = extract_process_ir_from_drawings(
        xlsx_io_path=str(plc_p),
        safety_matrix_path=str(matrix_p),
    )
    assert ir["extraction_mode"] == "local_deterministic_fallback"
    assert len(ir["safety_inputs"]) == 2
    assert len(ir["interlocks"]) == 3

    qs = generate_clarification_questions(ir)
    assert 5 <= len(qs) <= 10

    fds_path = tmp_path / "OP10_FDS.docx"
    out_docx = export_fds_docx(ir, str(fds_path), project_name="OP10双气缸压装工位")
    assert Path(out_docx).is_file() and Path(out_docx).stat().st_size > 0

    # 构建 XML 工程：故意漏掉 YV1 主干路的 B3_2号压装气缸上到位 联锁，验证 Safety Critic 精准拦截
    io_tags = parse_eplan_plc_table(str(plc_p))
    m_tags = default_m_tags() + [
        {"name": "M_自动模式", "data_type": "Bool", "logical_address": "%M10.2", "comment": "[FC1/NW3] 自动模式"},
        {"name": "M_手动模式", "data_type": "Bool", "logical_address": "%M10.3", "comment": "[FC1/NW4] 手动模式"},
        {"name": "M_步序_S10_送料伸出", "data_type": "Bool", "logical_address": "%M30.0", "comment": "[FC2/NW1] S10"},
        {"name": "M_步序_S20_压装下压", "data_type": "Bool", "logical_address": "%M30.1", "comment": "[FC2/NW2] S20"},
        {"name": "M_步序_S30_压装回升", "data_type": "Bool", "logical_address": "%M30.2", "comment": "[FC2/NW3] S30"},
        {"name": "M_报警汇总", "data_type": "Bool", "logical_address": "%M119.7", "comment": "[FC90/NW1] 汇总"},
    ]
    tag_dir = tmp_path / "02_变量表"
    xml_dir = tmp_path / "03_梯形图XML"
    tag_dir.mkdir()
    xml_dir.mkdir()
    _write_tag_table_xml("IO变量表", io_tags, str(tag_dir / "TagTable_IO变量表.xml"))
    _write_tag_table_xml("M辅助继电器表", m_tags, str(tag_dir / "TagTable_M辅助继电器表.xml"))

    fc1_nws = [
        build_simple_series_network(1, "NW1 急停", "【功能】急停扫描", [{"name": "SB0_急停按钮", "negated": False}], "M_急停正常"),
        build_simple_series_network(2, "NW2 安全汇总", "【功能】急停与安全门", [{"name": "M_急停正常", "negated": False}, {"name": "SQ1_安全门联锁开关", "negated": False}], "M_安全条件满足"),
        build_simple_series_network(3, "NW3 自动", "【功能】自动模式", [{"name": "SA1_手自动选择", "negated": False}], "M_自动模式"),
        build_simple_series_network(4, "NW4 手动", "【功能】手动模式", [{"name": "SA1_手自动选择", "negated": True}], "M_手动模式"),
    ]
    write_xml_file(str(xml_dir / "FC1_安全与模式.xml"), build_fc_xml("FC1_安全与模式", 1, fc1_nws))

    fc2_nws = [
        build_step_latch_network(1, "NW1 S10", "【功能】S10", "M_步序_S10_送料伸出", [{"name": "SB1_自动启动按钮", "negated": False}, {"name": "B1_1号送料气缸缩回到位", "negated": False}], next_step_vars=["M_步序_S20_压装下压"]),
        build_step_latch_network(2, "NW2 S20", "【功能】S20", "M_步序_S20_压装下压", [{"name": "M_步序_S10_送料伸出", "negated": False}, {"name": "B2_1号送料气缸伸出到位", "negated": False}], next_step_vars=["M_步序_S30_压装回升"]),
        build_step_latch_network(3, "NW3 S30", "【功能】S30", "M_步序_S30_压装回升", [{"name": "M_步序_S20_压装下压", "negated": False}, {"name": "B4_2号压装气缸下到位", "negated": False}], next_step_vars=[], extra_hold_contacts=[{"name": "B3_2号压装气缸上到位", "negated": True}]),
    ]
    write_xml_file(str(xml_dir / "FC2_步序.xml"), build_fc_xml("FC2_步序", 2, fc2_nws))

    def _build_fc3(include_b3_on_yv1: bool) -> str:
        yv1_interlocks = [{"name": "B3_2号压装气缸上到位", "negated": False}] if include_b3_on_yv1 else []
        nws = [
            build_manual_auto_interlocked_actuator_network(1, "NW1 送料", "【功能】YV1", "YV1_1号送料气缸伸出", manual_trigger_var="SB2_手动点动按钮", auto_request_var="M_步序_S10_送料伸出", interlock_contacts=yv1_interlocks),
            build_manual_auto_interlocked_actuator_network(2, "NW2 下压", "【功能】YV2", "YV2_2号压装气缸下压", manual_trigger_var="SB2_手动点动按钮", auto_request_var="M_步序_S20_压装下压", interlock_contacts=[{"name": "B2_1号送料气缸伸出到位", "negated": False}, {"name": "YV3_2号压装气缸回升", "negated": True}]),
            build_manual_auto_interlocked_actuator_network(3, "NW3 回升", "【功能】YV3", "YV3_2号压装气缸回升", manual_trigger_var="SB2_手动点动按钮", auto_request_var="M_步序_S30_压装回升", interlock_contacts=[{"name": "YV2_2号压装气缸下压", "negated": True}]),
        ]
        return build_fc_xml("FC3_执行器", 3, nws)

    write_xml_file(str(xml_dir / "FC3_执行器.xml"), _build_fc3(include_b3_on_yv1=False))
    fc90_nws = [build_simple_series_network(1, "NW1 报警", "【功能】报警", [{"name": "M_安全条件满足", "negated": True}], "M_报警汇总")]
    write_xml_file(str(xml_dir / "FC90_报警汇总.xml"), build_fc_xml("FC90_报警汇总", 90, fc90_nws))
    write_xml_file(str(xml_dir / "DB_全局定时器.xml"), build_timer_db_xml("全局定时器", ["T1_预留"]))
    write_xml_file(str(xml_dir / "OB1_Main.xml"), build_ob1_xml(["FC1_安全与模式", "FC2_步序", "FC3_执行器", "FC90_报警汇总"]))

    bad_critic = run_local_safety_critic(str(xml_dir), ir)
    assert bad_critic["passed"] is False
    assert any(
        v.get("actuator") == "YV1_1号送料气缸伸出" and "B3_2号压装气缸上到位" in v.get("missing_permissives", [])
        for v in bad_critic["violations"]
    ), f"未检出漏联锁违规: {bad_critic}"
    assert len(bad_critic["revision_directives"]) >= 1

    # 按 Critic 重写指令修复后重新审查
    write_xml_file(str(xml_dir / "FC3_执行器.xml"), _build_fc3(include_b3_on_yv1=True))
    good_critic = run_local_safety_critic(str(xml_dir), ir)
    assert good_critic["passed"] is True, f"修复后 Safety Critic 仍失败: {good_critic}"

