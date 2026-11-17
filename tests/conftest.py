# -*- coding: utf-8 -*-
"""pytest 公共夹具与测试项目构造辅助函数"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from tag_builder import _write_tag_table_xml
from lad_builder import (
    LadNetworkBuilder,
    build_fc_xml,
    build_ob1_xml,
    build_simple_series_network,
    build_timer_db_xml,
    render_network_xml,
    write_xml_file,
)


def default_io_tags() -> List[Dict[str, str]]:
    return [
        {
            "name": "SB0_急停按钮",
            "data_type": "Bool",
            "logical_address": "%I0.0",
            "comment": "[1.1 | -SB0 | 11-12/101] 急停按钮(NC)",
        },
        {
            "name": "FR1_1号电机热继",
            "data_type": "Bool",
            "logical_address": "%I0.1",
            "comment": "[1.2 | -FR1 | 95-96/102] 1号电机热继(NC)",
        },
        {
            "name": "SB1_1号电机正转启动",
            "data_type": "Bool",
            "logical_address": "%I0.2",
            "comment": "[1.3 | -SB1 | 13-14/103] 1号电机正转启动(NO)",
        },
        {
            "name": "SB2_1号电机反转启动",
            "data_type": "Bool",
            "logical_address": "%I0.3",
            "comment": "[1.4 | -SB2 | 13-14/104] 1号电机反转启动(NO)",
        },
        {
            "name": "SB3_1号电机停止",
            "data_type": "Bool",
            "logical_address": "%I0.4",
            "comment": "[1.5 | -SB3 | 11-12/105] 1号电机停止(NC)",
        },
        {
            "name": "KM1_1号电机正转",
            "data_type": "Bool",
            "logical_address": "%Q0.0",
            "comment": "[2.1 | -KM1 | Q0.0/201] 1号电机正转接触器(NO)",
        },
        {
            "name": "KM2_1号电机反转",
            "data_type": "Bool",
            "logical_address": "%Q0.1",
            "comment": "[2.2 | -KM2 | Q0.1/202] 1号电机反转接触器(NO)",
        },
    ]


def default_m_tags() -> List[Dict[str, str]]:
    return [
        {
            "name": "M_急停正常",
            "data_type": "Bool",
            "logical_address": "%M10.0",
            "comment": "[FC1/NW1] 急停状态正常",
        },
        {
            "name": "M_安全条件满足",
            "data_type": "Bool",
            "logical_address": "%M10.1",
            "comment": "[FC1/NW2] 全局安全允许条件",
        },
    ]


def build_parallel_then_series_network(
    nw_index: int,
    title: str,
    comment: str,
    branch_a: Dict[str, Any],
    branch_b: Dict[str, Any],
    series_after: List[Dict[str, Any]],
    coil_var: str,
    coil_type: str = "Coil",
) -> str:
    """构建 (branch_a ∥ branch_b) -> series_after -> coil_var 程序段"""
    b = LadNetworkBuilder(nw_index, title, comment)
    a_acc = b.add_access(branch_a["name"])
    b_acc = b.add_access(branch_b["name"])
    a_part = b.alloc_uid()
    b_part = b.alloc_uid()
    b.parts.append(
        f'    <Part Name="Contact" UId="{a_part}">'
        + ('<Negated Name="operand" />' if branch_a.get("negated") else "")
        + "</Part>"
    )
    b.parts.append(
        f'    <Part Name="Contact" UId="{b_part}">'
        + ('<Negated Name="operand" />' if branch_b.get("negated") else "")
        + "</Part>"
    )
    or_uid = b.alloc_uid()
    b.parts.append(
        f'    <Part Name="O" UId="{or_uid}"><TemplateValue Name="Card" Type="Cardinality">2</TemplateValue></Part>'
    )

    s_acc_uids, s_part_uids = [], []
    for c in series_after:
        acc = b.add_access(c["name"])
        part = b.alloc_uid()
        s_acc_uids.append(acc)
        s_part_uids.append(part)
        if c.get("negated"):
            b.parts.append(f'    <Part Name="Contact" UId="{part}"><Negated Name="operand" /></Part>')
        else:
            b.parts.append(f'    <Part Name="Contact" UId="{part}" />')

    coil_acc = b.add_access(coil_var)
    coil_uid = b.alloc_uid()
    b.parts.append(f'    <Part Name="{coil_type}" UId="{coil_uid}" />')

    b.wires.append(f'    <Wire UId="{b.alloc_uid()}"><Powerrail /><NameCon UId="{a_part}" Name="in" /><NameCon UId="{b_part}" Name="in" /></Wire>')
    b.wires.append(f'    <Wire UId="{b.alloc_uid()}"><IdentCon UId="{a_acc}" /><NameCon UId="{a_part}" Name="operand" /></Wire>')
    b.wires.append(f'    <Wire UId="{b.alloc_uid()}"><NameCon UId="{a_part}" Name="out" /><NameCon UId="{or_uid}" Name="in1" /></Wire>')
    b.wires.append(f'    <Wire UId="{b.alloc_uid()}"><IdentCon UId="{b_acc}" /><NameCon UId="{b_part}" Name="operand" /></Wire>')
    b.wires.append(f'    <Wire UId="{b.alloc_uid()}"><NameCon UId="{b_part}" Name="out" /><NameCon UId="{or_uid}" Name="in2" /></Wire>')

    prev_uid, prev_pin = or_uid, "out"
    for p_uid, acc_uid in zip(s_part_uids, s_acc_uids):
        b.wires.append(f'    <Wire UId="{b.alloc_uid()}"><NameCon UId="{prev_uid}" Name="{prev_pin}" /><NameCon UId="{p_uid}" Name="in" /></Wire>')
        b.wires.append(f'    <Wire UId="{b.alloc_uid()}"><IdentCon UId="{acc_uid}" /><NameCon UId="{p_uid}" Name="operand" /></Wire>')
        prev_uid, prev_pin = p_uid, "out"

    b.wires.append(f'    <Wire UId="{b.alloc_uid()}"><NameCon UId="{prev_uid}" Name="{prev_pin}" /><NameCon UId="{coil_uid}" Name="in" /></Wire>')
    b.wires.append(f'    <Wire UId="{b.alloc_uid()}"><IdentCon UId="{coil_acc}" /><NameCon UId="{coil_uid}" Name="operand" /></Wire>')
    return render_network_xml(b)


@pytest.fixture
def make_project(tmp_path: Path):
    def _factory(
        fc2_networks: Optional[List[str]] = None,
        fc1_networks: Optional[List[str]] = None,
        io_tags: Optional[List[Dict[str, str]]] = None,
        m_tags: Optional[List[Dict[str, str]]] = None,
        extra_fcs: Optional[Dict[str, str]] = None,
        ob1_calls: Optional[List[str]] = None,
        timer_members: Optional[List[str]] = None,
    ) -> Path:
        proj_dir = tmp_path / "proj"
        tag_dir = proj_dir / "02_变量表"
        xml_dir = proj_dir / "03_梯形图XML"
        tag_dir.mkdir(parents=True, exist_ok=True)
        xml_dir.mkdir(parents=True, exist_ok=True)

        _write_tag_table_xml("IO变量表", io_tags if io_tags is not None else default_io_tags(), str(tag_dir / "TagTable_IO变量表.xml"))
        _write_tag_table_xml("M辅助继电器表", m_tags if m_tags is not None else default_m_tags(), str(tag_dir / "TagTable_M辅助继电器表.xml"))

        write_xml_file(str(xml_dir / "DB_全局定时器.xml"), build_timer_db_xml("全局定时器", timer_members or ["T1_延时"]))

        if fc1_networks is None:
            fc1_networks = [
                build_simple_series_network(
                    1,
                    "NW1 急停扫描",
                    "【功能】急停常开触点扫描",
                    [{"name": "SB0_急停按钮", "negated": False}],
                    "M_急停正常",
                ),
                build_simple_series_network(
                    2,
                    "NW2 全局安全条件",
                    "【功能】汇总急停与热继正常条件",
                    [{"name": "M_急停正常", "negated": False}, {"name": "FR1_1号电机热继", "negated": False}],
                    "M_安全条件满足",
                ),
            ]
        write_xml_file(str(xml_dir / "FC1_安全与模式.xml"), build_fc_xml("FC1_安全与模式", 1, fc1_networks, "安全模块"))

        if fc2_networks is None:
            fc2_networks = [
                build_simple_series_network(
                    1,
                    "NW1 1号电机正转",
                    "【功能】默认安全控制回路",
                    [
                        {"name": "SB1_1号电机正转启动", "negated": False},
                        {"name": "SB3_1号电机停止", "negated": False},
                        {"name": "M_安全条件满足", "negated": False},
                    ],
                    "KM1_1号电机正转",
                )
            ]
        write_xml_file(str(xml_dir / "FC2_测试设备.xml"), build_fc_xml("FC2_测试设备", 2, fc2_networks, "测试设备"))

        calls = ["FC1_安全与模式", "FC2_测试设备"] if ob1_calls is None else ob1_calls
        if extra_fcs:
            for fname, xml_str in extra_fcs.items():
                write_xml_file(str(xml_dir / fname), xml_str)

        write_xml_file(str(xml_dir / "OB1_Main.xml"), build_ob1_xml(calls))
        return xml_dir

    return _factory
