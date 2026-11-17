# -*- coding: utf-8 -*-
"""SimaticML 结构、拓扑、禁令与现有样板/工程回归测试"""

from pathlib import Path
from conftest import SKILL_ROOT
from lad_builder import build_fc_xml, build_latch_network, build_simple_series_network, write_xml_file
from tia_connect import compare_eplan_with_tia_hw
from validate_lad_xml import validate_project_xmls


def test_templates_directory_passes():
    """内置 templates/ 目录必须 0 错误通过静态校验"""
    res = validate_project_xmls(str(SKILL_ROOT / "templates"))
    assert res.ok, f"templates 校验失败: {res.errors}"


def test_existing_workspace_projects_pass():
    """工作区 ../项目/ 下的既有交付项目在升级校验后必须继续 0 错误通过"""
    projects_root = SKILL_ROOT.parent / "项目"
    if not projects_root.is_dir():
        return
    for proj in sorted(projects_root.iterdir()):
        xml_dir = proj / "03_梯形图XML"
        if xml_dir.is_dir():
            res = validate_project_xmls(str(xml_dir))
            assert res.ok, f"项目 {proj.name} 校验失败: {res.errors}"


def test_multiple_coil_rejected(make_project):
    """同一变量被普通 Coil 驱动两次必须报双线圈错误"""
    nw1 = build_simple_series_network(
        1,
        "NW1 第一次驱动",
        "【功能】正转1",
        [{"name": "SB1_1号电机正转启动", "negated": False}, {"name": "M_安全条件满足", "negated": False}],
        "KM1_1号电机正转",
    )
    nw2 = build_simple_series_network(
        2,
        "NW2 第二次驱动",
        "【功能】正转2（违规双线圈）",
        [{"name": "SB2_1号电机反转启动", "negated": False}, {"name": "M_安全条件满足", "negated": False}],
        "KM1_1号电机正转",
    )
    xml_dir = make_project(fc2_networks=[nw1, nw2])
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("双线圈" in e for e in res.errors)


def test_uncalled_fc_rejected(make_project):
    """生成了 FC 但未在 OB1 调用必须报调用完整性错误"""
    nw = build_simple_series_network(
        1,
        "NW1 未调用块内逻辑",
        "【功能】测试漏调",
        [{"name": "SB1_1号电机正转启动", "negated": False}, {"name": "M_安全条件满足", "negated": False}],
        "KM2_1号电机反转",
    )
    extra_fc = build_fc_xml("FC3_未调用块", 3, [nw], "未调用测试块")
    xml_dir = make_project(extra_fcs={"FC3_未调用块.xml": extra_fc})
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("FC3_未调用块" in e and "调用完整性" in e for e in res.errors)


def test_ton_missing_et_opencon_rejected(make_project):
    """TON 定时器缺少 ET->OpenCon 连线必须报错"""
    xml_dir = make_project()
    bad_ton_fc = """<?xml version="1.0" encoding="utf-8"?>
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
      <Name>FC2_测试设备</Name>
      <Number>2</Number>
      <ProgrammingLanguage>LAD</ProgrammingLanguage>
    </AttributeList>
    <ObjectList>
      <SW.Blocks.CompileUnit ID="8" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">
  <Parts>
    <Access Scope="GlobalVariable" UId="21"><Symbol><Component Name="SB1_1号电机正转启动" /></Symbol></Access>
    <Access Scope="TypedConstant" UId="22"><Constant><ConstantValue>T#5S</ConstantValue></Constant></Access>
    <Access Scope="GlobalVariable" UId="23"><Symbol><Component Name="M_安全条件满足" /></Symbol></Access>
    <Part Name="Contact" UId="24" />
    <Part Name="TON" Version="1.0" UId="25">
      <Instance Scope="GlobalVariable" UId="26"><Component Name="全局定时器" /><Component Name="T1_延时" /></Instance>
      <TemplateValue Name="time_type" Type="Type">Time</TemplateValue>
    </Part>
  </Parts>
  <Wires>
    <Wire UId="27"><Powerrail /><NameCon UId="24" Name="in" /></Wire>
    <Wire UId="28"><IdentCon UId="21" /><NameCon UId="24" Name="operand" /></Wire>
    <Wire UId="29"><NameCon UId="24" Name="out" /><NameCon UId="25" Name="IN" /></Wire>
    <Wire UId="30"><IdentCon UId="22" /><NameCon UId="25" Name="PT" /></Wire>
  </Wires>
</FlgNet></NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
        <ObjectList>
          <MultilingualText ID="81" CompositionName="Comment"><ObjectList><MultilingualTextItem ID="82" CompositionName="Items"><AttributeList><Culture>zh-CN</Culture><Text>缺ET测试</Text></AttributeList></MultilingualTextItem></ObjectList></MultilingualText>
          <MultilingualText ID="83" CompositionName="Title"><ObjectList><MultilingualTextItem ID="84" CompositionName="Items"><AttributeList><Culture>zh-CN</Culture><Text>NW1 缺ET</Text></AttributeList></MultilingualTextItem></ObjectList></MultilingualText>
        </ObjectList>
      </SW.Blocks.CompileUnit>
    </ObjectList>
  </SW.Blocks.FC>
</Document>"""
    write_xml_file(str(xml_dir / "FC2_测试设备.xml"), bad_ton_fc)
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("ET" in e for e in res.errors)


def test_time_literal_constant_rejected(make_project):
    """时间常量误用 Scope='LiteralConstant' 必须报错"""
    xml_dir = make_project()
    bad_fc = (xml_dir / "FC2_测试设备.xml").read_text(encoding="utf-8-sig")
    bad_fc = bad_fc.replace(
        "<Parts>",
        '<Parts><Access Scope="LiteralConstant" UId="99"><Constant><ConstantType>Time</ConstantType><ConstantValue>T#5S</ConstantValue></Constant></Access>',
        1,
    )
    write_xml_file(str(xml_dir / "FC2_测试设备.xml"), bad_fc)
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("TypedConstant" in e for e in res.errors)


def test_part_name_call_rejected(make_project):
    """误用 <Part Name="Call"> 必须报错"""
    xml_dir = make_project()
    bad_ob1 = (xml_dir / "OB1_Main.xml").read_text(encoding="utf-8-sig").replace(
        '<Call UId="21">', '<Part Name="Call" UId="21">'
    ).replace("</Call>", "</Part>")
    write_xml_file(str(xml_dir / "OB1_Main.xml"), bad_ob1)
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any('<Part Name="Call">' in e for e in res.errors)


def test_scl_block_rejected(make_project):
    """包含 SCL 语言声明必须被 R2 拦截"""
    xml_dir = make_project()
    scl_fc = (xml_dir / "FC2_测试设备.xml").read_text(encoding="utf-8-sig").replace(
        "<ProgrammingLanguage>LAD</ProgrammingLanguage>",
        "<ProgrammingLanguage>SCL</ProgrammingLanguage>",
    )
    write_xml_file(str(xml_dir / "FC2_测试设备.xml"), scl_fc)
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("SCL" in e for e in res.errors)


def test_hw_compare_uses_bit_length_and_effective_bits():
    """硬件组态对账必须按 bit 长度与 DI/DQ 物理点数核验，避免把 Length(bits) 当成字节数"""
    mock_env = {
        "Project": {
            "Name": "测试工程",
            "HardwareModules": [
                {
                    "Name": "DI 14/DQ 10_1",
                    "PositionNumber": 1,
                    "IoRanges": [
                        {
                            "IoType": "Input",
                            "StartAddress": 0,
                            "LengthBits": 16,
                            "EffectiveBits": 14,
                            "Length": 2,
                            "EndAddress": 1,
                        },
                        {
                            "IoType": "Output",
                            "StartAddress": 0,
                            "LengthBits": 16,
                            "EffectiveBits": 10,
                            "Length": 2,
                            "EndAddress": 1,
                        },
                    ],
                },
                {
                    "Name": "AI 2_1",
                    "PositionNumber": 2,
                    "IoRanges": [
                        {
                            "IoType": "Input",
                            "StartAddress": 64,
                            "LengthBits": 32,
                            "EffectiveBits": 32,
                            "Length": 4,
                            "EndAddress": 67,
                        }
                    ],
                },
            ],
        }
    }
    res = compare_eplan_with_tia_hw(
        ["%I0.0", "%I1.5", "%I1.6", "%I2.0", "%Q0.0", "%Q1.1", "%Q1.2", "%IW64", "%IW66", "%IW68"],
        env_info=mock_env,
    )
    by_addr = {d["address"]: d["status"] for d in res["details"]}
    assert by_addr["%I0.0"] == "MATCHED"
    assert by_addr["%I1.5"] == "MATCHED"
    assert by_addr["%I1.6"] == "OUT_OF_RANGE"
    assert by_addr["%I2.0"] == "OUT_OF_RANGE"
    assert by_addr["%Q0.0"] == "MATCHED"
    assert by_addr["%Q1.1"] == "MATCHED"
    assert by_addr["%Q1.2"] == "OUT_OF_RANGE"
    assert by_addr["%IW64"] == "MATCHED"
    assert by_addr["%IW66"] == "MATCHED"
    assert by_addr["%IW68"] == "OUT_OF_RANGE"


def test_lad_builder_accepts_tuple_contacts_and_single_network_str(tmp_path, monkeypatch):
    """lad_builder 兼容 (name, negated) 元组触点、单段字符串传参及相对文件名写入"""
    nw = build_latch_network(
        1,
        "NW1 兼容测试",
        "【功能】测试元组触点",
        "SB1_启动",
        "KM1_电机",
        [("FR1_热继", False), ("M_安全条件满足", False)],
        "KM1_电机",
    )
    fc_xml = build_fc_xml("FC2_兼容测试", 2, nw)
    assert "<SW.Blocks.CompileUnit" in fc_xml
    assert '<Component Name="FR1_热继"' in fc_xml

    monkeypatch.chdir(tmp_path)
    write_xml_file("FC2_rel.xml", fc_xml)
    assert (tmp_path / "FC2_rel.xml").is_file()
