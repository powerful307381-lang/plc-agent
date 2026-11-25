# -*- coding: utf-8 -*-
"""全面回归测试：覆盖 P0 安全门禁、工位交接死锁修复、NCoil 仿真、FB 穿透审计、Excel 公式与统一 CLI"""

from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys

import openpyxl

from common_utils import infer_plc_family_label
from conftest import SKILL_ROOT, default_io_tags, default_m_tags
from delivery_builder import create_delivery_tables
from lad_builder import (
    build_fb_call_network,
    build_fc_xml,
    build_instance_db_xml,
    build_line_handover_networks,
    build_ob1_xml,
    build_simple_series_network,
    build_timer_db_xml,
    write_xml_file,
)
from parse_engineering_drawings import run_local_safety_critic
from plcsim_closed_loop_runner import LadScanCycleSimulator
from tag_builder import _write_tag_table_xml
import tia_connect
from validate_lad_xml import validate_project_xmls


def test_line_handover_completes_multiple_cycles_without_deadlock(tmp_path: Path):
    """P0 回归: 跨工位交接握手在下游撤除就绪位后必须释放 TransferDone，支持连续多工件循环"""
    tag_dir = tmp_path / "02_变量表"
    xml_dir = tmp_path / "03_梯形图XML"
    tag_dir.mkdir()
    xml_dir.mkdir()

    io_tags = [
        {"name": "B1_下游进料到位", "data_type": "Bool", "logical_address": "%I0.0", "comment": "[1.1|-B1|BK] 进料(NO)"},
        {"name": "YV1_移栽传输阀", "data_type": "Bool", "logical_address": "%Q0.0", "comment": "[2.1|-YV1|21] 移栽阀"},
    ]
    m_tags = default_m_tags() + [
        {"name": "M_上游加工完成", "data_type": "Bool", "logical_address": "%M11.0", "comment": "[FC10] 上游完成"},
        {"name": "M_下游就绪", "data_type": "Bool", "logical_address": "%M11.1", "comment": "[FC10] 下游就绪"},
        {"name": "M_交接请求输出", "data_type": "Bool", "logical_address": "%M11.2", "comment": "[FC10] 交接输出"},
        {"name": "M_报警汇总", "data_type": "Bool", "logical_address": "%M119.7", "comment": "[FC90] 报警汇总"},
    ]
    _write_tag_table_xml("IO变量表", io_tags, str(tag_dir / "TagTable_IO变量表.xml"))
    _write_tag_table_xml("M辅助继电器表", m_tags, str(tag_dir / "TagTable_M辅助继电器表.xml"))

    shutil.copy2(SKILL_ROOT / "templates" / "UDT_LineHandover.xml", xml_dir / "UDT_LineHandover.xml")
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
    write_xml_file(str(xml_dir / "FC10_工位交接.xml"), build_fc_xml("FC10_工位交接", 10, ho_nws, "交接握手"))
    write_xml_file(str(xml_dir / "OB1_Main.xml"), build_ob1_xml(["FC10_工位交接"]))

    sim = LadScanCycleSimulator(str(xml_dir))
    sim.reset({"M_安全条件满足": True, "M_报警汇总": False, "DB_工位交接.OP10_OP20.FaultStop": False})

    # 第 1 个工件：发起请求 -> 传输激活 -> 到位确认
    for _ in range(2):
        st = sim.step_cycle(0.1, {"M_上游加工完成": True, "M_下游就绪": True, "B1_下游进料到位": False})
    assert st["DB_工位交接.OP10_OP20.TransferActive"] is True
    assert st["M_交接请求输出"] is True

    for _ in range(2):
        st = sim.step_cycle(0.1, {"M_上游加工完成": False, "M_下游就绪": True, "B1_下游进料到位": True})
    assert st["DB_工位交接.OP10_OP20.TransferDone"] is True

    # 下游收到 TransferDone 后撤除 M_下游就绪=False，TransferDone 必须自动清零解锁
    for _ in range(2):
        st = sim.step_cycle(0.1, {"M_上游加工完成": False, "M_下游就绪": False, "B1_下游进料到位": False})
    assert st["DB_工位交接.OP10_OP20.TransferDone"] is False
    assert st["DB_工位交接.OP10_OP20.ReqToNext"] is False
    assert st["DB_工位交接.OP10_OP20.TransferActive"] is False

    # 第 2 个工件：再次就绪请求，必须能够再次激活传输（无永久死锁）
    for _ in range(2):
        st = sim.step_cycle(0.1, {"M_上游加工完成": True, "M_下游就绪": True, "B1_下游进料到位": False})
    assert st["DB_工位交接.OP10_OP20.TransferActive"] is True
    assert st["M_交接请求输出"] is True


def test_ncoil_falling_edge_and_negated_coil_simulation(tmp_path: Path):
    """P0 回归: NCoil 为下降沿检测 -(N)- 单周期脉冲而非取反线圈；Coil+Negated 才是取反输出"""
    tag_dir = tmp_path / "02_变量表"
    xml_dir = tmp_path / "03_梯形图XML"
    tag_dir.mkdir()
    xml_dir.mkdir()

    io_tags = [
        {"name": "SB1_脉冲触发", "data_type": "Bool", "logical_address": "%I0.0", "comment": "[1.1|-SB1|13-14] 触发(NO)"},
    ]
    m_tags = [
        {"name": "M_边沿_下降沿", "data_type": "Bool", "logical_address": "%M20.0", "comment": "[FC2/NW1] 下降沿存储位"},
        {"name": "M_下降沿输出", "data_type": "Bool", "logical_address": "%M30.0", "comment": "[FC2/NW1] 下降沿单周期脉冲"},
        {"name": "M_取反输出", "data_type": "Bool", "logical_address": "%M30.1", "comment": "[FC2/NW2] 取反线圈输出"},
    ]
    _write_tag_table_xml("IO变量表", io_tags, str(tag_dir / "TagTable_IO变量表.xml"))
    _write_tag_table_xml("M辅助继电器表", m_tags, str(tag_dir / "TagTable_M辅助继电器表.xml"))

    fc_xml = """<?xml version="1.0" encoding="utf-8"?>
<Document>
  <Engineering version="V16" />
  <SW.Blocks.FC ID="0">
    <AttributeList>
      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v4">
        <Section Name="Input" /><Section Name="Output" /><Section Name="InOut" />
        <Section Name="Temp" /><Section Name="Constant" />
        <Section Name="Return"><Member Name="Ret_Val" Datatype="Void" Accessibility="Public" /></Section>
      </Sections></Interface>
      <MemoryLayout>Optimized</MemoryLayout>
      <Name>FC2_边沿与取反</Name>
      <Number>2</Number>
      <ProgrammingLanguage>LAD</ProgrammingLanguage>
    </AttributeList>
    <ObjectList>
      <MultilingualText ID="1" CompositionName="Comment"><ObjectList><MultilingualTextItem ID="2" CompositionName="Items"><AttributeList><Culture>zh-CN</Culture><Text>NCoil与取反线圈</Text></AttributeList></MultilingualTextItem></ObjectList></MultilingualText>
      <SW.Blocks.CompileUnit ID="3" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">
  <Parts>
    <Access Scope="GlobalVariable" UId="21"><Symbol><Component Name="SB1_脉冲触发" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="22"><Symbol><Component Name="M_边沿_下降沿" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="23"><Symbol><Component Name="M_下降沿输出" /></Symbol></Access>
    <Part Name="Contact" UId="24" />
    <Part Name="NCoil" UId="25" />
  </Parts>
  <Wires>
    <Wire UId="26"><Powerrail /><NameCon UId="24" Name="in" /></Wire>
    <Wire UId="27"><IdentCon UId="21" /><NameCon UId="24" Name="operand" /></Wire>
    <Wire UId="28"><NameCon UId="24" Name="out" /><NameCon UId="25" Name="in" /></Wire>
    <Wire UId="29"><IdentCon UId="22" /><NameCon UId="25" Name="bit" /></Wire>
    <Wire UId="30"><IdentCon UId="23" /><NameCon UId="25" Name="operand" /></Wire>
  </Wires>
</FlgNet></NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
        <ObjectList>
          <MultilingualText ID="4" CompositionName="Comment"><ObjectList><MultilingualTextItem ID="5" CompositionName="Items"><AttributeList><Culture>zh-CN</Culture><Text>下降沿测试</Text></AttributeList></MultilingualTextItem></ObjectList></MultilingualText>
          <MultilingualText ID="6" CompositionName="Title"><ObjectList><MultilingualTextItem ID="7" CompositionName="Items"><AttributeList><Culture>zh-CN</Culture><Text>NW1 下降沿线圈</Text></AttributeList></MultilingualTextItem></ObjectList></MultilingualText>
        </ObjectList>
      </SW.Blocks.CompileUnit>
      <SW.Blocks.CompileUnit ID="8" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">
  <Parts>
    <Access Scope="GlobalVariable" UId="21"><Symbol><Component Name="SB1_脉冲触发" /></Symbol></Access>
    <Access Scope="GlobalVariable" UId="22"><Symbol><Component Name="M_取反输出" /></Symbol></Access>
    <Part Name="Contact" UId="23" />
    <Part Name="Coil" UId="24"><Negated Name="operand" /></Part>
  </Parts>
  <Wires>
    <Wire UId="25"><Powerrail /><NameCon UId="23" Name="in" /></Wire>
    <Wire UId="26"><IdentCon UId="21" /><NameCon UId="23" Name="operand" /></Wire>
    <Wire UId="27"><NameCon UId="23" Name="out" /><NameCon UId="24" Name="in" /></Wire>
    <Wire UId="28"><IdentCon UId="22" /><NameCon UId="24" Name="operand" /></Wire>
  </Wires>
</FlgNet></NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
        <ObjectList>
          <MultilingualText ID="9" CompositionName="Comment"><ObjectList><MultilingualTextItem ID="A" CompositionName="Items"><AttributeList><Culture>zh-CN</Culture><Text>取反线圈测试</Text></AttributeList></MultilingualTextItem></ObjectList></MultilingualText>
          <MultilingualText ID="B" CompositionName="Title"><ObjectList><MultilingualTextItem ID="C" CompositionName="Items"><AttributeList><Culture>zh-CN</Culture><Text>NW2 取反线圈</Text></AttributeList></MultilingualTextItem></ObjectList></MultilingualText>
        </ObjectList>
      </SW.Blocks.CompileUnit>
    </ObjectList>
  </SW.Blocks.FC>
</Document>"""
    write_xml_file(str(xml_dir / "FC2_边沿与取反.xml"), fc_xml)
    write_xml_file(str(xml_dir / "OB1_Main.xml"), build_ob1_xml(["FC2_边沿与取反"]))

    res = validate_project_xmls(str(xml_dir))
    assert res.ok, f"NCoil 静态校验失败: {res.errors}"

    sim = LadScanCycleSimulator(str(xml_dir))
    sim.reset()
    # 周期 1: 持续为 0 -> NCoil 必须为 False，取反 Coil 为 True
    st1 = sim.step_cycle(0.1, {"SB1_脉冲触发": False})
    assert st1["M_下降沿输出"] is False
    assert st1["M_取反输出"] is True
    # 周期 2: 上升至 1 -> NCoil 为 False，取反 Coil 为 False
    st2 = sim.step_cycle(0.1, {"SB1_脉冲触发": True})
    assert st2["M_下降沿输出"] is False
    assert st2["M_取反输出"] is False
    # 周期 3: 1 -> 0 下降沿 -> NCoil 产生单周期 True 脉冲
    st3 = sim.step_cycle(0.1, {"SB1_脉冲触发": False})
    assert st3["M_下降沿输出"] is True
    assert st3["M_取反输出"] is True
    # 周期 4: 保持 0 -> NCoil 脉冲结束回到 False
    st4 = sim.step_cycle(0.1, {"SB1_脉冲触发": False})
    assert st4["M_下降沿输出"] is False


def test_safety_critic_and_s3_penetrate_fb_standard_actuator_call(tmp_path: Path):
    """P0/P2 回归: Safety Critic 与 S3 审计能够穿透 FB_StandardActuator 调用实参检测漏联锁"""
    tag_dir = tmp_path / "02_变量表"
    xml_dir = tmp_path / "03_梯形图XML"
    tag_dir.mkdir()
    xml_dir.mkdir()

    shutil.copy2(SKILL_ROOT / "templates" / "FB_StandardActuator.xml", xml_dir / "FB_StandardActuator.xml")
    io_tags = [
        {"name": "SB0_急停按钮", "data_type": "Bool", "logical_address": "%I0.0", "comment": "[1.1|-SB0|11-12] 急停(NC)"},
        {"name": "SB3_手动送料点动", "data_type": "Bool", "logical_address": "%I0.1", "comment": "[1.2|-SB3|13-14] 点动(NO)"},
        {"name": "B1_1号送料缩回到位", "data_type": "Bool", "logical_address": "%I0.2", "comment": "[2.1|-B1|BK] 缩回(NO)"},
        {"name": "B2_1号送料伸出到位", "data_type": "Bool", "logical_address": "%I0.3", "comment": "[2.2|-B2|BK] 伸出(NO)"},
        {"name": "B3_2号压装气缸上到位", "data_type": "Bool", "logical_address": "%I0.4", "comment": "[2.3|-B3|BK] 上到位(NO)"},
        {"name": "YV1_1号送料气缸伸出", "data_type": "Bool", "logical_address": "%Q0.0", "comment": "[3.1|-YV1|21] 送料电磁阀"},
    ]
    m_tags = default_m_tags() + [
        {"name": "M_手动模式", "data_type": "Bool", "logical_address": "%M10.2", "comment": "[FC1] 手动"},
        {"name": "M_自动模式", "data_type": "Bool", "logical_address": "%M10.3", "comment": "[FC1] 自动"},
        {"name": "M_故障复位", "data_type": "Bool", "logical_address": "%M10.4", "comment": "[FC1] 复位"},
        {"name": "M_步序_S10_送料", "data_type": "Bool", "logical_address": "%M30.0", "comment": "[FC1] S10"},
        {"name": "M_预留占位允许", "data_type": "Bool", "logical_address": "%M30.1", "comment": "[FC1] 错误占位"},
        {"name": "M_报警_送料超时", "data_type": "Bool", "logical_address": "%M100.0", "comment": "[OB1] 超时"},
        {"name": "M_报警_粘连故障", "data_type": "Bool", "logical_address": "%M100.1", "comment": "[OB1] 粘连"},
        {"name": "MD200_送料ET", "data_type": "Time", "logical_address": "%MD200", "comment": "[OB1] ET"},
    ]
    _write_tag_table_xml("IO变量表", io_tags, str(tag_dir / "TagTable_IO变量表.xml"))
    _write_tag_table_xml("M辅助继电器表", m_tags, str(tag_dir / "TagTable_M辅助继电器表.xml"))

    fc1_nws = [
        build_simple_series_network(1, "NW1 急停", "【功能】急停", [{"name": "SB0_急停按钮", "negated": False}], "M_急停正常"),
        build_simple_series_network(2, "NW2 安全", "【功能】安全", [{"name": "M_急停正常", "negated": False}], "M_安全条件满足"),
        build_simple_series_network(3, "NW3 占位", "【功能】模式", [{"name": "M_安全条件满足", "negated": False}], "M_手动模式"),
        build_simple_series_network(4, "NW4 自动", "【功能】自动", [{"name": "M_安全条件满足", "negated": False}], "M_自动模式"),
        build_simple_series_network(5, "NW5 复位", "【功能】复位", [{"name": "M_安全条件满足", "negated": False}], "M_故障复位"),
        build_simple_series_network(6, "NW6 步序", "【功能】步序", [{"name": "M_安全条件满足", "negated": False}], "M_步序_S10_送料"),
        build_simple_series_network(7, "NW7 占位允许", "【功能】占位", [{"name": "M_安全条件满足", "negated": False}], "M_预留占位允许"),
    ]
    write_xml_file(str(xml_dir / "FC1_安全与模式.xml"), build_fc_xml("FC1_安全与模式", 1, fc1_nws))

    idb_xml = build_instance_db_xml(
        idb_name="iDB_YV1_送料",
        idb_number=11,
        fb_name="FB_StandardActuator",
        input_members=[
            ("I_安全条件满足", "Bool"), ("I_手动模式", "Bool"), ("I_自动模式", "Bool"),
            ("I_手动伸出点动", "Bool"), ("I_自动伸出请求", "Bool"), ("I_外部防撞联锁允许", "Bool"),
            ("I_缩回到位传感器", "Bool"), ("I_伸出到位传感器", "Bool"), ("I_故障复位", "Bool"), ("I_超时设定PT", "Time"),
        ],
        output_members=[
            ("Q_伸出电磁阀驱动", "Bool"), ("Q_伸出超时报警", "Bool"), ("Q_双传感同时触发故障", "Bool"), ("Q_经过时间ET", "Time"),
        ],
        static_members=[("stat_伸出超时TON", "TON_TIME")],
    )
    write_xml_file(str(xml_dir / "iDB_YV1_送料.xml"), idb_xml)

    def _write_ob1(interlock_tag: str) -> None:
        ob1_xml = build_ob1_xml([
            "FC1_安全与模式",
            {
                "name": "FB_StandardActuator",
                "block_type": "FB",
                "instance": "iDB_YV1_送料",
                "parameters": [
                    {"name": "I_安全条件满足", "section": "Input", "type": "Bool", "value": "M_安全条件满足"},
                    {"name": "I_手动模式", "section": "Input", "type": "Bool", "value": "M_手动模式"},
                    {"name": "I_自动模式", "section": "Input", "type": "Bool", "value": "M_自动模式"},
                    {"name": "I_手动伸出点动", "section": "Input", "type": "Bool", "value": "SB3_手动送料点动"},
                    {"name": "I_自动伸出请求", "section": "Input", "type": "Bool", "value": "M_步序_S10_送料"},
                    {"name": "I_外部防撞联锁允许", "section": "Input", "type": "Bool", "value": interlock_tag},
                    {"name": "I_缩回到位传感器", "section": "Input", "type": "Bool", "value": "B1_1号送料缩回到位"},
                    {"name": "I_伸出到位传感器", "section": "Input", "type": "Bool", "value": "B2_1号送料伸出到位"},
                    {"name": "I_故障复位", "section": "Input", "type": "Bool", "value": "M_故障复位"},
                    {"name": "I_超时设定PT", "section": "Input", "type": "Time", "value": "T#3S", "scope": "TypedConstant"},
                    {"name": "Q_伸出电磁阀驱动", "section": "Output", "type": "Bool", "value": "YV1_1号送料气缸伸出"},
                    {"name": "Q_伸出超时报警", "section": "Output", "type": "Bool", "value": "M_报警_送料超时"},
                    {"name": "Q_双传感同时触发故障", "section": "Output", "type": "Bool", "value": "M_报警_粘连故障"},
                    {"name": "Q_经过时间ET", "section": "Output", "type": "Time", "value": "MD200_送料ET"},
                ],
            },
        ])
        write_xml_file(str(xml_dir / "OB1_Main.xml"), ob1_xml)

    ir = {
        "interlocks": [
            {
                "actuator": "YV1_1号送料气缸伸出",
                "required_permissives": ["M_安全条件满足", "B3_2号压装气缸上到位"],
                "mutex_coils": [],
                "reason": "压头不在上到位禁止送料",
            }
        ]
    }

    _write_ob1("M_预留占位允许")
    bad = run_local_safety_critic(str(xml_dir), ir)
    assert bad["passed"] is False
    assert any("B3_2号压装气缸上到位" in v.get("missing_permissives", []) for v in bad["violations"])

    _write_ob1("B3_2号压装气缸上到位")
    good = run_local_safety_critic(str(xml_dir), ir)
    assert good["passed"] is True, f"FB 穿透审计未通过: {good}"


def test_delivery_builder_excel_formulas_and_no_markdown_stars(tmp_path: Path):
    """P2 回归: 交付三表包含真实 Excel 公式看板、去除 ** 星号、默认日期动态"""
    out_xlsx = tmp_path / "05_现场调试交付三表.xlsx"
    out_md = tmp_path / "05_现场调试交付三表.md"
    create_delivery_tables(
        {"project_name": "公式校验工程"},
        default_io_tags(),
        str(out_xlsx),
        str(out_md),
    )
    wb = openpyxl.load_workbook(out_xlsx, data_only=False)
    ws_intro = wb["说明"]
    intro_vals = [str(ws_intro.cell(r, 2).value or "") for r in range(1, ws_intro.max_row + 1)]
    assert any(v.startswith("=COUNTA(") for v in intro_vals)
    assert any(v.startswith("=COUNTIF(") for v in intro_vals)
    assert any(v.startswith("=IFERROR(") for v in intro_vals)
    assert datetime.now().strftime("%Y-%m-%d") in intro_vals

    ws_io = wb["I_O打点表"]
    last_row = ws_io.max_row
    assert str(ws_io.cell(last_row, 2).value).startswith("=COUNTA(")
    assert str(ws_io.cell(last_row, 11).value).startswith("=COUNTIF(")
    assert str(ws_io.cell(last_row, 13).value).startswith("=IFERROR(")
    assert not ws_io.auto_filter.ref.endswith(str(last_row))

    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for val in row:
                assert "**" not in str(val or ""), f"Excel 单元格残留 Markdown 星号: {val}"


def test_reverse_delivery_detects_s7_1500_family():
    """P2 回归: 反向交付表正确识别 S7-1500 / ET 200SP / S7-1200 CPU 家族"""
    assert infer_plc_family_label("PLC_1 (6ES7 515-2AM02-0AB0)") == "SIMATIC S7-1500"
    assert infer_plc_family_label("CPU 1510SP-1 PN (6ES7 510-1DJ01-0AB0)") == "SIMATIC ET 200SP"
    assert infer_plc_family_label("CPU 1214C DC/DC/DC (6ES7 214-1AG40-0XB0)") == "SIMATIC S7-1200"


def test_tia_connect_honest_force_dryrun_and_snap7_failure():
    """P0 回归: force_output 诚实标注 DryRun、不可达 Snap7 诚实报错、伪造非 hex Token 被拒"""
    f_res = tia_connect.tia_live_data_channel(
        action="force_output",
        values={"%Q0.0": True},
        human_approved_force=True,
    )
    assert f_res["Success"] is True
    assert f_res["DryRun"] is True
    assert f_res["ExecutedOnPlc"] is False

    s_res = tia_connect.tia_live_data_channel(
        action="read_snapshot",
        plc_ip="127.0.0.1",
        port=65432,
    )
    assert s_res["Success"] is False
    assert s_res["Connected"] is False

    d_res = tia_connect.tia_download_to_plc(
        human_approved=True,
        confirm_online_token="not_hex_token_12",
    )
    assert d_res["Success"] is False
    assert d_res["GateReason"] == "MISSING_COMPARE_ONLINE_TOKEN"


def test_online_gate_ticket_ps_verification(tmp_path: Path):
    """P0 回归: tia_common.psm1 在线下载一次性票据签发、作用域比对与防重放验签"""
    store = tmp_path / "tickets.json"
    psm1 = str(SKILL_ROOT / "scripts" / "tia_common.psm1")
    ps_script = f"""
Import-Module '{psm1}' -DisableNameChecking -Force
$t = New-CompareOnlineGateTicket -ProjectName 'LineA' -PlcName 'PLC_1' -CpuModel '1214C' -CompareState 'Equal' -IsFailSafePlc $false -TicketStorePath '{store}'
$badScope = Test-CompareOnlineGateTicket -Token $t -ProjectName 'LineA' -PlcName 'PLC_2' -TicketStorePath '{store}'
$forged = Test-CompareOnlineGateTicket -Token '1234567890abcdef' -ProjectName 'LineA' -PlcName 'PLC_1' -TicketStorePath '{store}'
$okOnce = Test-CompareOnlineGateTicket -Token $t -ProjectName 'LineA' -PlcName 'PLC_1' -Consume -TicketStorePath '{store}'
$replay = Test-CompareOnlineGateTicket -Token $t -ProjectName 'LineA' -PlcName 'PLC_1' -TicketStorePath '{store}'
[PSCustomObject]@{{
    token = $t
    badScopeReason = $badScope.Reason
    forgedReason = $forged.Reason
    okValid = $okOnce.Valid
    replayReason = $replay.Reason
}} | ConvertTo-Json -Compress
"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    data = json.loads(proc.stdout.strip())
    assert len(data["token"]) == 16
    assert data["badScopeReason"] == "ONLINE_TOKEN_SCOPE_MISMATCH"
    assert data["forgedReason"] == "ONLINE_TOKEN_NOT_FOUND"
    assert data["okValid"] is True
    assert data["replayReason"] == "ONLINE_TOKEN_ALREADY_CONSUMED"


def test_do_import_pipeline_does_not_leak_expected_token():
    """P0 回归: do_import_pipeline.ps1 报错体不得泄露 ExpectedToken"""
    ps_txt = (SKILL_ROOT / "scripts" / "do_import_pipeline.ps1").read_text(encoding="utf-8")
    assert "ExpectedToken" not in ps_txt


def test_validate_lad_xml_supports_v16_to_v20_and_strict_version(make_project):
    """P1 回归: validate_lad_xml 默认兼容 V16–V20，传入 allowed_versions 时精确拦截"""
    xml_dir = make_project()
    fc_p = xml_dir / "FC2_测试设备.xml"
    fc_p.write_text(
        fc_p.read_text(encoding="utf-8-sig").replace('<Engineering version="V16" />', '<Engineering version="V19" />'),
        encoding="utf-8-sig",
    )
    assert validate_project_xmls(str(xml_dir)).ok is True
    strict_res = validate_project_xmls(str(xml_dir), allowed_versions=("V16",))
    assert strict_res.ok is False
    assert any("V19" in e for e in strict_res.errors)


def test_unified_cli_subcommands(tmp_path: Path):
    """P1 回归: 统一入口 scripts/cli.py 子命令端到端可运行"""
    cli_py = str(SKILL_ROOT / "scripts" / "cli.py")

    help_p = subprocess.run([sys.executable, cli_py, "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert help_p.returncode == 0
    assert "reconcile" in help_p.stdout and "critic" in help_p.stdout

    aml_p = tmp_path / "topo.aml"
    aml_p.write_text(
        '<?xml version="1.0" encoding="utf-8"?><CAEXFile SchemaVersion="2.15"><InstanceHierarchy Name="H">'
        '<InternalElement Name="PLC_1"><Attribute Name="OrderNumber"><Value>6ES7 214-1AG40-0XB0</Value></Attribute></InternalElement>'
        "</InstanceHierarchy></CAEXFile>",
        encoding="utf-8",
    )
    r_aml = subprocess.run([sys.executable, cli_py, "inspect-aml", str(aml_p)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r_aml.returncode == 0
    assert json.loads(r_aml.stdout)["cpu_family"] == "S7-1200"

    io_xlsx = tmp_path / "io.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["PLC地址", "信号类型", "元器件代号", "元器件端子/线号", "页码.列号", "NO/NC", "中文功能", "是否备用"])
    ws.append(["%I0.0", "DI", "-SB0", "11-12/101", "1.1", "NC", "急停按钮", "否"])
    ws.append(["%Q0.0", "DO", "-KM1", "A1/201", "2.1", "线圈", "输送电机", "否"])
    wb.save(io_xlsx)

    ir_json = tmp_path / "ir.json"
    r_ir = subprocess.run(
        [sys.executable, cli_py, "extract-ir", "--io", str(io_xlsx), "--out-json", str(ir_json)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert r_ir.returncode == 0
    assert json.loads(r_ir.stdout)["safety_input_count"] == 1
    assert ir_json.is_file()


def test_infer_sim_defaults_iec_start_thermal_and_sim_edge_safety(tmp_path: Path):
    """实机联调回归: _infer_sim_defaults 兼容 IEC S2_启动按钮、热保护_NC/停止_NC 且不误置 SIM 边沿位"""
    from plcsim_closed_loop_runner import LadScanCycleSimulator, _infer_sim_defaults
    from tag_builder import build_tags

    tags = [
        {"name": "S0_急停按钮_NC", "data_type": "Bool", "logical_address": "%I0.2", "comment": "[1.1 | -S0 | 11-12] 急停(NC)"},
        {"name": "F1_电机热保护_NC", "data_type": "Bool", "logical_address": "%I0.3", "comment": "[2.1 | -F1 | 95-96] 电机热保护(NC)"},
        {"name": "S2_启动按钮", "data_type": "Bool", "logical_address": "%I0.4", "comment": "[2.3 | -S2 | 13-14] 启动(NO)"},
        {"name": "S1_停止按钮_NC", "data_type": "Bool", "logical_address": "%I0.5", "comment": "[2.3 | -S1 | 11-12] 停止(NC)"},
        {"name": "M_仿真使能", "data_type": "Bool", "logical_address": "%M20.0", "comment": "[FC99/NW1] 仿真使能开关"},
        {"name": "M_SIM_启动边沿", "data_type": "Bool", "logical_address": "%M20.1", "comment": "[FC99/NW1] PBox边沿记忆位"},
    ]
    build_tags(tags, str(tmp_path / "tags.xlsx"), str(tmp_path / "TagTable_Test.xml"), table_name="TagTable_Test")
    sim = LadScanCycleSimulator(str(tmp_path))
    init_ok, _, start_btn, _, _, _ = _infer_sim_defaults(sim, ir_data=None)

    assert start_btn == "S2_启动按钮"
    assert init_ok.get("S0_急停按钮_NC") is True
    assert init_ok.get("F1_电机热保护_NC") is True
    assert init_ok.get("S1_停止按钮_NC") is True
    assert init_ok.get("M_仿真使能") is True
    assert "M_SIM_启动边沿" not in init_ok


def test_eplan_auto_export_ps1_multi_drive_and_systemstartup_support():
    """实机联调回归: eplan_auto_export.ps1 兼容多盘符与 EPLAN 2.9+ SystemStartUp/LastStartedVariantDir"""
    ps_txt = (SKILL_ROOT / "scripts" / "eplan_auto_export.ps1").read_text(encoding="utf-8")
    for token in ("SystemStartUp", "SystemDirectories", "LastStartedVariantDir", "Get-PSDrive -PSProvider FileSystem"):
        assert token in ps_txt, f"eplan_auto_export.ps1 缺少探测项: {token}"
