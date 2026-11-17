# -*- coding: utf-8 -*-
"""block_fingerprint 语义指纹比对回归测试"""

import re
from pathlib import Path
from block_fingerprint import compare
from lad_builder import (
    build_fc_xml,
    build_latch_network,
    build_ob1_xml,
    build_simple_series_network,
    build_timer_db_xml,
    write_xml_file,
)
from tag_builder import _write_tag_table_xml


def _shift_uids(xml_text: str, offset: int = 100) -> str:
    return re.sub(r'UId="(\d+)"', lambda m: f'UId="{int(m.group(1)) + offset}"', xml_text)


def test_uid_renumbering_has_zero_diff(tmp_path: Path):
    """仅 UId 重新编号（如博途导入再导出）不应触发改动误报"""
    nw = build_latch_network(
        1,
        "NW1 正转回路",
        "【功能】测试UId重排",
        start_var="SB1_启动",
        latch_var="KM1_正转",
        stop_contacts=[{"name": "KM2_反转", "negated": True}, {"name": "M_安全条件满足", "negated": False}],
        coil_var="KM1_正转",
    )
    xml_old = build_fc_xml("FC2_电机", 2, [nw], "测试")
    xml_new = _shift_uids(xml_old, 100)
    p1, p2 = tmp_path / "old.xml", tmp_path / "new.xml"
    write_xml_file(str(p1), xml_old)
    write_xml_file(str(p2), xml_new)
    assert compare(str(p1), str(p2)) == []


def test_contact_no_to_nc_detected(tmp_path: Path):
    """常开触点改为常闭触点（总数量不变）必须被检出"""
    nw_a = build_simple_series_network(
        1,
        "NW1 触点极性",
        "【功能】测试常开改常闭",
        [{"name": "SB1_启动", "negated": False}, {"name": "M_安全条件满足", "negated": False}],
        "KM1_正转",
    )
    nw_b = build_simple_series_network(
        1,
        "NW1 触点极性",
        "【功能】测试常开改常闭",
        [{"name": "SB1_启动", "negated": True}, {"name": "M_安全条件满足", "negated": False}],
        "KM1_正转",
    )
    p1, p2 = tmp_path / "a.xml", tmp_path / "b.xml"
    write_xml_file(str(p1), build_fc_xml("FC2_电机", 2, [nw_a], "测试"))
    write_xml_file(str(p2), build_fc_xml("FC2_电机", 2, [nw_b], "测试"))
    diffs = compare(str(p1), str(p2))
    assert diffs and any("程序段 1" in d for d in diffs)


def test_interlock_operand_change_detected(tmp_path: Path):
    """触点数量不变但互锁变量名被替换必须被检出"""
    nw_a = build_simple_series_network(
        1,
        "NW1 互锁替换",
        "【功能】测试操作数变更",
        [{"name": "KM2_反转", "negated": True}, {"name": "M_安全条件满足", "negated": False}],
        "KM1_正转",
    )
    nw_b = build_simple_series_network(
        1,
        "NW1 互锁替换",
        "【功能】测试操作数变更",
        [{"name": "KM3_备用", "negated": True}, {"name": "M_安全条件满足", "negated": False}],
        "KM1_正转",
    )
    p1, p2 = tmp_path / "a.xml", tmp_path / "b.xml"
    write_xml_file(str(p1), build_fc_xml("FC2_电机", 2, [nw_a], "测试"))
    write_xml_file(str(p2), build_fc_xml("FC2_电机", 2, [nw_b], "测试"))
    diffs = compare(str(p1), str(p2))
    assert diffs and any("KM2_反转" in d or "KM3_备用" in d for d in diffs)


def test_tag_table_diff_detected(tmp_path: Path):
    """变量表地址或注释变化必须被检出"""
    p1, p2 = tmp_path / "t1.xml", tmp_path / "t2.xml"
    _write_tag_table_xml(
        "IO变量表",
        [{"name": "SB1_启动", "data_type": "Bool", "logical_address": "%I0.0", "comment": "[1.1|-SB1|13-14] 启动(NO)"}],
        str(p1),
    )
    _write_tag_table_xml(
        "IO变量表",
        [{"name": "SB1_启动", "data_type": "Bool", "logical_address": "%I0.5", "comment": "[1.1|-SB1|13-14] 启动(NO)"}],
        str(p2),
    )
    diffs = compare(str(p1), str(p2))
    assert diffs and any("SB1_启动" in d for d in diffs)


def test_tia_roundtrip_timer_db_and_ob1_zero_diff(tmp_path: Path):
    """博途导出时自动展开 IEC_TIMER 内部 Section(None) 及 OB1 系统 Informative 字段不应误报改动"""
    db_gen = build_timer_db_xml("全局定时器", [{"name": "T1_星三角切换"}])
    db_tia = db_gen.replace(
        "<Member Name=\"T1_星三角切换\" Datatype=\"IEC_TIMER\" Version=\"1.0\" Remanence=\"NonRetain\" Accessibility=\"Public\"><AttributeList><BooleanAttribute Name=\"ExternalAccessible\" SystemDefined=\"true\">true</BooleanAttribute><BooleanAttribute Name=\"ExternalVisible\" SystemDefined=\"true\">true</BooleanAttribute><BooleanAttribute Name=\"ExternalWritable\" SystemDefined=\"true\">true</BooleanAttribute><BooleanAttribute Name=\"SetPoint\" SystemDefined=\"true\">false</BooleanAttribute></AttributeList></Member>",
        "<Member Name=\"T1_星三角切换\" Datatype=\"IEC_TIMER\" Version=\"1.0\" Remanence=\"NonRetain\" Accessibility=\"Public\"><AttributeList><BooleanAttribute Name=\"ExternalAccessible\" SystemDefined=\"true\">true</BooleanAttribute><BooleanAttribute Name=\"ExternalVisible\" SystemDefined=\"true\">true</BooleanAttribute><BooleanAttribute Name=\"ExternalWritable\" SystemDefined=\"true\">true</BooleanAttribute><BooleanAttribute Name=\"SetPoint\" SystemDefined=\"true\">false</BooleanAttribute></AttributeList><Sections><Section Name=\"None\"><Member Name=\"PT\" Datatype=\"Time\" /><Member Name=\"IN\" Datatype=\"Bool\" /><Member Name=\"ET\" Datatype=\"Time\" /><Member Name=\"Q\" Datatype=\"Bool\" /></Section></Sections></Member>",
    )
    p_db1, p_db2 = tmp_path / "db_gen.xml", tmp_path / "db_tia.xml"
    write_xml_file(str(p_db1), db_gen)
    write_xml_file(str(p_db2), db_tia)
    assert compare(str(p_db1), str(p_db2)) == []

    ob_gen = build_ob1_xml(["FC1_安全与模式"])
    ob_tia = ob_gen.replace(
        "<Section Name=\"Input\" />",
        "<Section Name=\"Input\"><Member Name=\"Initial_Call\" Datatype=\"Bool\" Accessibility=\"Public\" Informative=\"true\" /><Member Name=\"Remanence\" Datatype=\"Bool\" Accessibility=\"Public\" Informative=\"true\" /></Section>",
    )
    p_ob1, p_ob2 = tmp_path / "ob_gen.xml", tmp_path / "ob_tia.xml"
    write_xml_file(str(p_ob1), ob_gen)
    write_xml_file(str(p_ob2), ob_tia)
    assert compare(str(p_ob1), str(p_ob2)) == []
