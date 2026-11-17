# -*- coding: utf-8 -*-
"""
模块名称: sim_feedback_builder
功能说明: 根据 EPLAN I/O 点表与工艺状态机 IR 自动配对单/双电控执行器与原位/工作位传感器，
         基于 templates/FC_SimFeedback.xml 范式确定性生成可在博途与仿真器运行的纯 LAD 自反馈块 (FC99_仿真自反馈.xml)。
符合规范: references/11_Commissioning_SOP_Gates.md (S7-1200 虚拟调试分流)
"""

import re
from typing import Any, Dict, List, Optional, Tuple

try:
    from lad_builder import (
        LadNetworkBuilder,
        build_fc_xml,
        render_network_xml,
        write_xml_file,
    )
except ImportError:
    from scripts.lad_builder import (  # type: ignore
        LadNetworkBuilder,
        build_fc_xml,
        render_network_xml,
        write_xml_file,
    )


_HOME_KWS = ("缩回", "上到位", "原位", "后退", "松开", "退回", "复位到位")
_WORK_KWS = ("伸出", "下到位", "下压", "前进", "夹紧", "顶升", "压紧", "工作位")
_RETURN_ACT_KWS = ("缩回", "回升", "后退", "松开", "退回", "复位")
_EXTEND_ACT_KWS = ("伸出", "下压", "前进", "夹紧", "顶升", "压紧")


def _extract_cylinder_group(var_name: str) -> Optional[str]:
    """从变量名中提取所属执行器/气缸主体标识（如 '1号送料气缸', '2号压装气缸', '推料气缸'）"""
    if "_" in var_name:
        body = var_name.split("_", 1)[1]
    else:
        body = var_name
    m = re.search(r"(\d*号?[\u4e00-\u9fa5A-Za-z0-9]*气缸)", body)
    if m:
        return m.group(1)
    m2 = re.search(r"(\d*号?[\u4e00-\u9fa5A-Za-z0-9]*油缸)", body)
    if m2:
        return m2.group(1)
    return None


def infer_actuator_sensor_pairs(
    io_tags: List[Dict[str, Any]],
    ir_data: Optional[Dict[str, Any]] = None,
    default_travel_time: str = "T#1S",
) -> List[Dict[str, Any]]:
    """
    自动从 I/O 点表与工艺 IR 配对执行器线圈（单/双电控）与原位/工作位传感器：
    返回列表元素结构:
      - actuator_group: str
      - control_mode: 'single_acting' | 'double_acting'
      - work_coil: str
      - home_coil: Optional[str]
      - home_sensor: Optional[str]
      - work_sensor: Optional[str]
      - travel_time: str
    """
    groups: Dict[str, Dict[str, Any]] = {}

    for tag in io_tags:
        name = str(tag.get("name", "")).strip()
        addr = str(tag.get("logical_address") or tag.get("address") or "").strip().upper()
        if not name or "备用" in name:
            continue
        grp_name = _extract_cylinder_group(name)
        if not grp_name:
            continue

        bucket = groups.setdefault(
            grp_name,
            {"coils": [], "sensors": []},
        )
        if addr.startswith("%Q"):
            bucket["coils"].append(name)
        elif addr.startswith("%I"):
            bucket["sensors"].append(name)

    valve_types = (ir_data or {}).get("valve_center_types") or {}
    pairs: List[Dict[str, Any]] = []

    for grp_name, bucket in sorted(groups.items()):
        coils: List[str] = bucket["coils"]
        sensors: List[str] = bucket["sensors"]
        if not coils or not sensors:
            continue

        home_sensor = next((s for s in sensors if any(k in s for k in _HOME_KWS)), None)
        work_sensor = next((s for s in sensors if any(k in s for k in _WORK_KWS) and s != home_sensor), None)

        home_coil = None
        work_coil = None
        if len(coils) >= 2:
            work_coil = next((c for c in coils if any(k in c for k in _EXTEND_ACT_KWS)), coils[0])
            home_coil = next((c for c in coils if c != work_coil and any(k in c for k in _RETURN_ACT_KWS)), None)
            if home_coil is None:
                others = [c for c in coils if c != work_coil]
                home_coil = others[0] if others else None
            control_mode = "double_acting"
        else:
            work_coil = coils[0]
            vtype = str(valve_types.get(work_coil, ""))
            control_mode = "double_acting" if "双电控" in vtype else "single_acting"

        pairs.append({
            "actuator_group": grp_name,
            "control_mode": control_mode,
            "work_coil": work_coil,
            "home_coil": home_coil,
            "home_sensor": home_sensor,
            "work_sensor": work_sensor,
            "travel_time": default_travel_time,
        })

    return pairs


def _build_ton_feedback_network(
    nw_index: int,
    title: str,
    comment: str,
    enable_contacts: List[Dict[str, Any]],
    timer_db_name: str,
    timer_member: str,
    travel_time: str,
    set_sensor: Optional[str],
    reset_sensor: Optional[str],
) -> str:
    """构建单个带 TON 行程延时的仿真自反馈 LAD 网络：串联触点 -> TON -> SCoil(到位) / RCoil(离位)"""
    builder = LadNetworkBuilder(nw_index, title, comment)

    contact_uids: List[str] = []
    for raw_c in enable_contacts:
        acc_uid = builder.add_access(raw_c["name"], "GlobalVariable")
        part_uid = builder.alloc_uid()
        if raw_c.get("negated", False):
            builder.parts.append(f'    <Part Name="Contact" UId="{part_uid}"><Negated Name="operand" /></Part>')
        else:
            builder.parts.append(f'    <Part Name="Contact" UId="{part_uid}" />')
        contact_uids.append(part_uid)
        builder.wires.append(
            f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{acc_uid}" /><NameCon UId="{part_uid}" Name="operand" /></Wire>'
        )

    pt_uid = builder.add_typed_time_constant(travel_time)
    ton_uid = builder.alloc_uid()
    ton_inst_uid = builder.alloc_uid()
    open_et_uid = builder.alloc_uid()

    builder.parts.append(
        f'    <Part Name="TON" Version="1.0" UId="{ton_uid}">\n'
        f'      <Instance Scope="GlobalVariable" UId="{ton_inst_uid}">\n'
        f'        <Component Name="{timer_db_name}" />\n'
        f'        <Component Name="{timer_member}" />\n'
        f'      </Instance>\n'
        f'      <TemplateValue Name="time_type" Type="Type">Time</TemplateValue>\n'
        f'    </Part>'
    )

    coil_targets: List[str] = []
    if set_sensor:
        s_acc = builder.add_access(set_sensor, "GlobalVariable")
        s_part = builder.alloc_uid()
        builder.parts.append(f'    <Part Name="SCoil" UId="{s_part}" />')
        builder.wires.append(
            f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{s_acc}" /><NameCon UId="{s_part}" Name="operand" /></Wire>'
        )
        coil_targets.append(f'<NameCon UId="{s_part}" Name="in" />')

    if reset_sensor:
        r_acc = builder.add_access(reset_sensor, "GlobalVariable")
        r_part = builder.alloc_uid()
        builder.parts.append(f'    <Part Name="RCoil" UId="{r_part}" />')
        builder.wires.append(
            f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{r_acc}" /><NameCon UId="{r_part}" Name="operand" /></Wire>'
        )
        coil_targets.append(f'<NameCon UId="{r_part}" Name="in" />')

    # 连接母线与串联触点链 -> TON.IN
    builder.wires.append(
        f'    <Wire UId="{builder.alloc_uid()}"><Powerrail /><NameCon UId="{contact_uids[0]}" Name="in" /></Wire>'
    )
    for idx in range(len(contact_uids) - 1):
        builder.wires.append(
            f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{contact_uids[idx]}" Name="out" /><NameCon UId="{contact_uids[idx + 1]}" Name="in" /></Wire>'
        )
    builder.wires.append(
        f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{contact_uids[-1]}" Name="out" /><NameCon UId="{ton_uid}" Name="IN" /></Wire>'
    )
    builder.wires.append(
        f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{pt_uid}" /><NameCon UId="{ton_uid}" Name="PT" /></Wire>'
    )
    builder.wires.append(
        f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{ton_uid}" Name="ET" /><OpenCon UId="{open_et_uid}" /></Wire>'
    )
    builder.wires.append(
        f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{ton_uid}" Name="Q" />{"".join(coil_targets)}</Wire>'
    )

    return render_network_xml(builder)


def build_sim_feedback_fc_xml(
    pairs: List[Dict[str, Any]],
    timer_db_name: str = "全局定时器",
    sim_enable_tag: str = "M_仿真模式激活",
    fc_name: str = "FC99_仿真自反馈",
    fc_number: int = 99,
    sim_enable_addr: str = "%M19.0",
    out_xml_path: Optional[str] = None,
) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    """
    生成可在 TIA Portal V16 编译及在 S7-1200/仿真器闭环执行的 FC99_仿真自反馈.xml。
    返回: (fc_xml_str, timer_member_names, sim_m_tags)
    """
    networks: List[str] = []
    timer_members: List[str] = []
    nw_idx = 1

    for idx, p in enumerate(pairs, start=1):
        grp = p["actuator_group"]
        ctrl_mode = p["control_mode"]
        work_coil = p["work_coil"]
        home_coil = p.get("home_coil")
        work_sensor = p.get("work_sensor")
        home_sensor = p.get("home_sensor")
        travel_time = p.get("travel_time", "T#1S")

        if ctrl_mode == "single_acting":
            # 1. 得电伸出 -> 延时置位工作到位, 复位原位
            t_ext = f"T_Sim_{idx}_Work"
            timer_members.append(t_ext)
            networks.append(
                _build_ton_feedback_network(
                    nw_index=nw_idx,
                    title=f"NW{nw_idx} {grp}仿真工作位反馈(单电控)",
                    comment=f"【仿真说明】当 {sim_enable_tag}=1 且 {work_coil} 得电持续 {travel_time}，自动反馈工作位到位并复位原位。",
                    enable_contacts=[
                        {"name": sim_enable_tag, "negated": False},
                        {"name": work_coil, "negated": False},
                    ],
                    timer_db_name=timer_db_name,
                    timer_member=t_ext,
                    travel_time=travel_time,
                    set_sensor=work_sensor,
                    reset_sensor=home_sensor,
                )
            )
            nw_idx += 1

            # 2. 失电弹簧回位 -> 延时置位原位, 复位工作位
            t_ret = f"T_Sim_{idx}_Home"
            timer_members.append(t_ret)
            networks.append(
                _build_ton_feedback_network(
                    nw_index=nw_idx,
                    title=f"NW{nw_idx} {grp}仿真弹簧回原位反馈(单电控)",
                    comment=f"【仿真说明】当 {sim_enable_tag}=1 且 {work_coil} 失电持续 {travel_time}，自动弹簧复位原位传感器。",
                    enable_contacts=[
                        {"name": sim_enable_tag, "negated": False},
                        {"name": work_coil, "negated": True},
                    ],
                    timer_db_name=timer_db_name,
                    timer_member=t_ret,
                    travel_time=travel_time,
                    set_sensor=home_sensor,
                    reset_sensor=work_sensor,
                )
            )
            nw_idx += 1
        else:
            # 双电控正侧动作 -> 延时置位工作位
            t_work = f"T_Sim_{idx}_Work"
            timer_members.append(t_work)
            work_contacts = [
                {"name": sim_enable_tag, "negated": False},
                {"name": work_coil, "negated": False},
            ]
            if home_coil:
                work_contacts.append({"name": home_coil, "negated": True})
            networks.append(
                _build_ton_feedback_network(
                    nw_index=nw_idx,
                    title=f"NW{nw_idx} {grp}仿真工作位反馈(双电控)",
                    comment=f"【仿真说明】当 {sim_enable_tag}=1 且 {work_coil} 得电持续 {travel_time}，置位工作到位传感器。",
                    enable_contacts=work_contacts,
                    timer_db_name=timer_db_name,
                    timer_member=t_work,
                    travel_time=travel_time,
                    set_sensor=work_sensor,
                    reset_sensor=home_sensor,
                )
            )
            nw_idx += 1

            # 双电控回程动作 -> 延时置位原位
            if home_coil:
                t_home = f"T_Sim_{idx}_Home"
                timer_members.append(t_home)
                networks.append(
                    _build_ton_feedback_network(
                        nw_index=nw_idx,
                        title=f"NW{nw_idx} {grp}仿真回原位反馈(双电控)",
                        comment=f"【仿真说明】当 {sim_enable_tag}=1 且 {home_coil} 得电持续 {travel_time}，置位原位传感器。",
                        enable_contacts=[
                            {"name": sim_enable_tag, "negated": False},
                            {"name": home_coil, "negated": False},
                            {"name": work_coil, "negated": True},
                        ],
                        timer_db_name=timer_db_name,
                        timer_member=t_home,
                        travel_time=travel_time,
                        set_sensor=home_sensor,
                        reset_sensor=work_sensor,
                    )
                )
                nw_idx += 1

    fc_xml = build_fc_xml(
        fc_name,
        fc_number,
        networks,
        comment="虚拟调试自动闭环反馈块（仅 M_仿真模式激活=1 时模拟机械行程延时并切换传感器）",
    )
    if out_xml_path:
        write_xml_file(out_xml_path, fc_xml)

    sim_m_tags = [
        {
            "name": sim_enable_tag,
            "data_type": "Bool",
            "logical_address": sim_enable_addr,
            "comment": f"[{fc_name}/NW1] 虚拟闭环仿真自反馈总使能（实机运行严禁置1）",
        }
    ]
    return fc_xml, timer_members, sim_m_tags
