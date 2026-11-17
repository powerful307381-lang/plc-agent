# -*- coding: utf-8 -*-
"""
模块名称: validate_lad_xml
功能说明: 对生成的 SimaticML LAD / DB / TagTable XML 进行全维度静态语法、跨表符号、内存重叠、安全合规与工业级逻辑审计。
符合规范:
  - eplan-to-tia-lad/SKILL.md 阶段 3
  - references/02_命名与注释规范.md
  - references/03_安全信号校验.md (S1~S5)
  - references/04_地址分区规划.md
  - references/05_SimaticML_LAD防报错规范.md 第 6 节
审计功能:
  1. 双线圈检测 (Multiple Coil Check)：普通输出线圈 Coil 严禁重复驱动同一变量
  2. 悬空实物 I/O 检测 (Unused I/O Check)：图纸中声明的 %I/%Q 变量（非备用）未进入逻辑告警
  3. M 变量读写闭环检测 (Read/Write Balance Check)：统计 %M 读写次数，排查只写未读/只读未写
  4. OB1 调用完整性检测 (OB1 Call Integrity Check)：检查所有 FC 块是否均在 OB1 中被调用
  5. 位级地址重叠检测 (Bit/Byte/Word/DWord Overlap Check)：防止 %M10.0 与 %MW10 等跨粒度踩踏
  6. 安全信号与互锁审计 (S1~S5)：NC 安全信号极性校验、输出安全条件串入与正反转/星三角软件互锁校验
  7. Wire 拓扑与元件必需引脚校验：检测 Contact/Coil/PBox/O/TON 漏接或单端点 Wire
"""

import argparse
from collections import defaultdict, deque
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from typing import List, Dict, Set, Tuple, Any, Optional

IQ_NAME_RE = re.compile(r"^[A-Z]{1,3}\d{1,3}[A-Z]?_[\u4e00-\u9fa5A-Za-z0-9_]+$")
M_NAME_RE = re.compile(r"^(?:M(?:B|W|D)?\d*|ALM|ERR|ST|STEP|CNT|HMI)_[\u4e00-\u9fa5A-Za-z0-9_]+$")
EPLAN_COMMENT_RE = re.compile(r"^\[\s*[^|\]]+\s*\|\s*[^|\]]+\s*\|\s*[^|\]]+\s*\]")
SAFETY_KEYWORDS = ("急停", "热继", "停止", "极限", "限位", "安全门", "光栅", "过载")
SAFETY_PERMISSIVE_TAGS = {"M_急停正常", "M_安全条件满足"}
ALARM_OR_LAMP_KEYWORDS = ("报警", "故障", "ALM", "ERR", "急停灯", "故障灯", "指示", "信号灯", "蜂鸣", "HL", "PF")
ACTUATOR_PREFIX_OR_KEYWORDS = ("KM", "YV", "MB", "接触器", "电磁阀", "变频器", "电机", "正转", "反转", "星", "角", "伸出", "缩回")

# 常见 LAD 元件必需连接的引脚集合
REQUIRED_PART_PINS: Dict[str, Set[str]] = {
    "Contact": {"in", "operand"},
    "Coil": {"in", "operand"},
    "SCoil": {"in", "operand"},
    "RCoil": {"in", "operand"},
    "NCoil": {"in", "operand"},
    "PBox": {"in", "bit", "out"},
    "NBox": {"in", "bit", "out"},
    "TON": {"IN", "PT", "ET"},
    "TOF": {"IN", "PT", "ET"},
    "TP": {"IN", "PT", "ET"},
}

# 元件内部作为数据写入目标或输出源的引脚集合
SOURCE_OUT_PINS = {"out", "out1", "out2", "q", "et", "cv", "qu", "qd", "ret_val", "eno"}
TIMER_SUBMEMBERS = {"Q", "ET", "IN", "PT", "CV", "QU", "QD"}
TEMPLATE_GALLERY_FILES = {
    "FC_样板.xml", "OB1_样板.xml", "FB_样板.xml", "DB_样板.xml", "TagTable_样板.xml",
    "UDT_样板.xml", "InstanceDB_样板.xml", "DB_数组与UDT样板.xml",
    "FC_高级指令画廊样板.xml", "FB_多重背景样板.xml", "OB100_启动样板.xml",
    "OB30_PID循环中断样板.xml", "FC_通信样板.xml", "FC_运动控制样板.xml",
    "FB_StationModeManager.xml", "FB_StandardActuator.xml", "UDT_LineHandover.xml",
    "FC_SimFeedback.xml", "WatchTable_Template.xml", "ForceTable_Template.xml",
}
# 西门子博途启用系统与时钟存储器字节时自动生成的官方保留区标准符号
SIEMENS_SYSTEM_CLOCK_TAGS = {
    "System_Byte", "FirstScan", "DiagStatusUpdate", "AlwaysTRUE", "AlwaysFALSE",
    "Clock_Byte", "Clock_10Hz", "Clock_5Hz", "Clock_2.5Hz", "Clock_2Hz",
    "Clock_1.25Hz", "Clock_1Hz", "Clock_0.625Hz", "Clock_0.5Hz",
}
TIMER_COUNTER_DATATYPES = {
    "TON_TIME", "TOF_TIME", "TP_TIME", "TONR_TIME", "IEC_TIMER",
    "CTU", "CTD", "CTUD", "IEC_COUNTER", "CTU_INT", "CTD_INT", "CTUD_INT",
}
# 西门子系统标准指令 FB（在 <CallInfo BlockType="FB"> 中调用时无需工程内存在自定义 FB XML）
SYSTEM_STANDARD_FBS = {
    "TON", "TOF", "TP", "TONR", "TON_TIME", "TOF_TIME", "TP_TIME", "TONR_TIME", "IEC_TIMER",
    "CTU", "CTD", "CTUD", "IEC_COUNTER",
    "PID_Compact", "PID_3Step", "PID_Temp",
    "MC_Power", "MC_Home", "MC_MoveAbsolute", "MC_MoveRelative", "MC_MoveVelocity", "MC_Halt", "MC_Reset",
}


def resolve_project_path(path_str: Optional[str]) -> Optional[str]:
    """智能解析项目路径（自动兼容是否带有 '项目/' 前缀）"""
    if not path_str:
        return path_str
    if os.path.exists(path_str):
        return path_str
    alt_in_proj = os.path.join("项目", path_str)
    if os.path.exists(alt_in_proj):
        return alt_in_proj
    return path_str


def parse_address_bit_span(logical_addr: str, data_type: str = "Bool") -> Optional[Tuple[str, int, int]]:
    """
    将西门子逻辑地址解析为 (区域, 起始绝对位偏移, 占用位数)。
    例如:
      %I0.0   -> ('I', 0, 1)
      %MB10   -> ('M', 80, 8)
      %MW30   -> ('M', 240, 16)
      %MD200  -> ('M', 1600, 32)
    """
    addr = (logical_addr or "").strip().upper()
    bit_m = re.match(r"^%([IQM])(\d+)\.([0-7])$", addr)
    if bit_m:
        area = bit_m.group(1)
        byte_idx = int(bit_m.group(2))
        bit_idx = int(bit_m.group(3))
        return area, byte_idx * 8 + bit_idx, 1

    width_m = re.match(r"^%([IQM])([BWD])(\d+)$", addr)
    if width_m:
        area = width_m.group(1)
        size_code = width_m.group(2)
        byte_idx = int(width_m.group(3))
        width_bits = {"B": 8, "W": 16, "D": 32}[size_code]
        return area, byte_idx * 8, width_bits

    # 无 B/W/D 前缀的纯字节地址兜底（按数据类型推算宽度）
    plain_m = re.match(r"^%([IQM])(\d+)$", addr)
    if plain_m:
        area = plain_m.group(1)
        byte_idx = int(plain_m.group(2))
        dt = (data_type or "Byte").strip().lower()
        if dt in ("word", "int", "uint", "s5time"):
            width_bits = 16
        elif dt in ("dword", "dint", "udint", "real", "time", "tod"):
            width_bits = 32
        else:
            width_bits = 8
        return area, byte_idx * 8, width_bits

    return None


class ValidationResult(tuple):
    """
    兼容易用的校验结果对象：
    既可以作为 (ok, errors) 2 元组解包，也可以通过 .ok, .errors, .warnings 访问。
    """
    def __new__(cls, ok: bool, errors: List[str], warnings: Optional[List[str]] = None):
        return super().__new__(cls, (ok, errors))

    def __init__(self, ok: bool, errors: List[str], warnings: Optional[List[str]] = None):
        self.ok = ok
        self.errors = errors
        self.warnings = warnings or []


def collect_defined_symbols(
    xml_files: List[str]
) -> Tuple[Set[str], Dict[str, Set[str]], List[str], Dict[str, Dict[str, Any]]]:
    """
    扫描目录中所有 TagTable 与 GlobalDB XML，收集：
      1. 全局变量名集合 (global_tags)
      2. DB 块名 -> 成员名集合 (db_members)
      3. 变量表命名、地址分区、注释规范与位级重叠校验错误 (tag_errors)
      4. 变量元数据详细字典 (tag_metadata)
    """
    global_tags: Set[str] = set()
    db_members: Dict[str, Set[str]] = {}
    idb_instance_of: Dict[str, str] = {}
    seen_addresses: Dict[str, str] = {}
    bit_occupancy: Dict[Tuple[str, int], Tuple[str, str]] = {}
    tag_errors: List[str] = []
    tag_metadata: Dict[str, Dict[str, Any]] = {}

    for file_path in xml_files:
        fname = os.path.basename(file_path)
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read().lstrip("\ufeff")
            root = ET.fromstring(content)
        except Exception:
            continue

        # 1. 解析变量表 SW.Tags.PlcTagTable
        for tag_elem in root.findall(".//SW.Tags.PlcTag"):
            name_el = tag_elem.find("AttributeList/Name")
            addr_el = tag_elem.find("AttributeList/LogicalAddress")
            type_el = tag_elem.find("AttributeList/DataTypeName")
            tag_name = (name_el.text or "").strip() if name_el is not None else ""
            logical_addr = (addr_el.text or "").strip() if addr_el is not None else ""
            data_type = (type_el.text or "").strip() if type_el is not None else "Bool"

            comment_texts = [
                (t.text or "").strip()
                for t in tag_elem.findall(".//MultilingualTextItem/AttributeList/Text")
                if t.text
            ]
            joined_comment = " ".join(comment_texts)

            if not tag_name:
                tag_errors.append(f"[{fname}] 存在空的变量名 (PlcTag/Name)")
                continue

            if tag_name in global_tags:
                tag_errors.append(f"[{fname}] 跨表变量重名冲突: '{tag_name}'")
            global_tags.add(tag_name)

            tag_metadata[tag_name] = {
                "name": tag_name,
                "address": logical_addr,
                "dataType": data_type,
                "comment": joined_comment,
                "file": fname
            }

            is_spare = ("备用" in tag_name) or ("备用" in joined_comment)
            is_sys_clock = tag_name in SIEMENS_SYSTEM_CLOCK_TAGS

            if logical_addr:
                addr_upper = logical_addr.upper()
                if addr_upper in seen_addresses and seen_addresses[addr_upper] != tag_name:
                    tag_errors.append(
                        f"[{fname}] 地址冲突: '{logical_addr}' 同时被 '{seen_addresses[addr_upper]}' 和 '{tag_name}' 占用"
                    )
                seen_addresses[addr_upper] = tag_name

                # 位级内存重叠检测 (Bit / Byte / Word / DWord)，官方系统/时钟字节与位天然复用跳过
                span = parse_address_bit_span(addr_upper, data_type) if not is_sys_clock else None
                if span is not None:
                    area, start_bit, bit_len = span
                    conflicts_reported: Set[Tuple[str, str]] = set()
                    for b_off in range(start_bit, start_bit + bit_len):
                        key = (area, b_off)
                        if key in bit_occupancy:
                            occ_name, occ_addr = bit_occupancy[key]
                            # 完全相同的地址已在上方 seen_addresses 报过错，这里只报跨粒度/跨字重叠
                            if occ_name != tag_name and occ_addr != addr_upper:
                                pair = (occ_name, occ_addr)
                                if pair not in conflicts_reported:
                                    conflicts_reported.add(pair)
                                    tag_errors.append(
                                        f"[{fname}] 地址内存重叠冲突 ERROR: '{tag_name}' ({logical_addr}, {data_type}) "
                                        f"与 '{occ_name}' ({occ_addr}) 发生字节/位区域重叠！"
                                    )
                        else:
                            bit_occupancy[key] = (tag_name, addr_upper)

                # 检查命名正则与 %M 地址分区 (MB0~MB9 保留；官方系统/时钟存储器位豁免)
                if addr_upper.startswith("%I") or addr_upper.startswith("%Q"):
                    if not IQ_NAME_RE.match(tag_name) and not is_spare:
                        tag_errors.append(
                            f"[{fname}] I/Q 变量命名不符合 '元器件代号_中文功能' 规范: '{tag_name}' ({logical_addr})"
                        )
                    if not is_spare and not EPLAN_COMMENT_RE.match(joined_comment):
                        tag_errors.append(
                            f"[{fname}] I/Q 变量 '{tag_name}' ({logical_addr}) 注释不符合 '[页码.列号 | 元器件代号 | 端子/线号]' 格式: '{joined_comment}'"
                        )
                elif addr_upper.startswith("%M") and not is_sys_clock:
                    if not M_NAME_RE.match(tag_name) and not is_spare:
                        tag_errors.append(
                            f"[{fname}] M 变量命名不符合 'M_中文功能' 规范: '{tag_name}' ({logical_addr})"
                        )
                    m_match = re.match(r"^%M(?:B|W|D)?(\d+)", addr_upper)
                    if m_match and int(m_match.group(1)) < 10:
                        tag_errors.append(
                            f"[{fname}] M 变量地址违规占用保留区 MB0~MB9: '{tag_name}' ({logical_addr})"
                        )

            # 检查安全信号注释是否标注 (NC) 或 常闭 (S1)
            if logical_addr.upper().startswith("%I") and any(k in tag_name for k in SAFETY_KEYWORDS):
                if not joined_comment or ("NC" not in joined_comment.upper() and "常闭" not in joined_comment):
                    tag_errors.append(
                        f"[{fname}] 安全信号 '{tag_name}' 注释未标注常闭 (NC) (S1): '{joined_comment}'"
                    )

        # 2. 解析全局数据块 SW.Blocks.GlobalDB
        for db_elem in root.findall(".//SW.Blocks.GlobalDB"):
            db_name_el = db_elem.find("AttributeList/Name")
            db_name = (db_name_el.text or "").strip() if db_name_el is not None else ""
            if db_name:
                global_tags.add(db_name)
                members: Set[str] = set()
                static_sec = db_elem.find(".//{*}Section[@Name='Static']")
                mem_nodes = static_sec.findall("{*}Member") if static_sec is not None else db_elem.findall(".//{*}Member")
                for mem in mem_nodes:
                    m_name = mem.attrib.get("Name")
                    m_type = (mem.attrib.get("Datatype") or "").strip().strip('"').upper()
                    if m_name:
                        members.add(m_name)
                        if m_type in TIMER_COUNTER_DATATYPES:
                            db_members.setdefault("__TIMER_MEMBERS__", set()).add(f"{db_name}.{m_name}")
                db_members[db_name] = members

        # 3. 解析功能块 FB 接口公开成员（供 InstanceDB 成员穿透核对）
        for fb_elem in root.findall(".//SW.Blocks.FB"):
            fb_name_el = fb_elem.find("AttributeList/Name")
            fb_name = (fb_name_el.text or "").strip() if fb_name_el is not None else ""
            if fb_name:
                fb_members: Set[str] = set()
                for sec_name in ("Input", "Output", "InOut", "Static"):
                    sec_el = fb_elem.find(f"AttributeList/Interface/{{*}}Sections/{{*}}Section[@Name='{sec_name}']")
                    if sec_el is not None:
                        for mem in sec_el.findall("{*}Member"):
                            m_name = mem.attrib.get("Name")
                            if m_name:
                                fb_members.add(m_name)
                if fb_members:
                    db_members[f"__FB__:{fb_name}"] = fb_members

        # 4. 解析背景数据块 SW.Blocks.InstanceDB
        for idb_elem in root.findall(".//SW.Blocks.InstanceDB"):
            idb_name_el = idb_elem.find("AttributeList/Name")
            idb_name = (idb_name_el.text or "").strip() if idb_name_el is not None else ""
            if idb_name:
                global_tags.add(idb_name)
                idb_members: Set[str] = set()
                for mem in idb_elem.findall("AttributeList/Interface/{*}Sections/{*}Section/{*}Member"):
                    m_name = mem.attrib.get("Name")
                    if m_name:
                        idb_members.add(m_name)
                if idb_members:
                    db_members[idb_name] = idb_members
                else:
                    inst_of_el = idb_elem.find("AttributeList/InstanceOfName")
                    inst_of = (inst_of_el.text or "").strip().strip('"') if inst_of_el is not None else ""
                    if inst_of:
                        idb_instance_of[idb_name] = inst_of

        # 5. 解析用户自定义数据类型 SW.Types.PlcStruct (UDT)
        for udt_elem in root.findall(".//SW.Types.PlcStruct"):
            udt_name_el = udt_elem.find("AttributeList/Name")
            udt_name = (udt_name_el.text or "").strip() if udt_name_el is not None else ""
            if udt_name:
                global_tags.add(udt_name)

    # 跨文件解析 InstanceDB 指向的 FB 接口成员（防止文件扫描顺序影响）
    for idb_name, inst_of in idb_instance_of.items():
        if idb_name not in db_members and f"__FB__:{inst_of}" in db_members:
            db_members[idb_name] = db_members[f"__FB__:{inst_of}"]

    return global_tags, db_members, tag_errors, tag_metadata


def _is_safety_input_var(var_name: str, tag_metadata: Optional[Dict[str, Dict[str, Any]]]) -> bool:
    """判断变量是否为物理安全输入信号 (%I 且注释标注 (NC)/常闭 或含安全关键字)"""
    meta = (tag_metadata or {}).get(var_name)
    if meta is not None:
        addr = meta.get("address", "").upper()
        if not addr.startswith("%I"):
            return False
        comment = meta.get("comment", "").rstrip()
        if comment.endswith("(NC)") or "常闭" in comment:
            return True
    return any(k in var_name for k in SAFETY_KEYWORDS)


def _is_alarm_or_lamp_target(var_name: str) -> bool:
    """判断输出变量是否为报警位、故障位或指示灯"""
    return any(k in var_name for k in ALARM_OR_LAMP_KEYWORDS)


def _is_safety_permissive_var(var_name: str, tag_metadata: Optional[Dict[str, Dict[str, Any]]]) -> bool:
    """判断触点是否为安全前置许可条件：仅认白名单中间变量或物理安全输入，严禁按 KM1/主接触 等名称片段放行"""
    return var_name in SAFETY_PERMISSIVE_TAGS or _is_safety_input_var(var_name, tag_metadata)


# 能流图元件内部直通路径映射 (非 O/Call/Compare 均视作 en->eno)
_PART_POWER_LINKS: Dict[str, List[Tuple[str, str]]] = {
    "Contact": [("in", "out")],
    "Coil": [("in", "out")],
    "SCoil": [("in", "out")],
    "RCoil": [("in", "out")],
    "NCoil": [("in", "out")],
    "PBox": [("in", "out")],
    "NBox": [("in", "out")],
    "TON": [("IN", "Q")],
    "TOF": [("IN", "Q")],
    "TP": [("IN", "Q")],
    "TONR": [("IN", "Q")],
    "CTU": [("CU", "Q"), ("CU", "QU")],
    "CTD": [("CD", "Q"), ("CD", "QD")],
    "CTUD": [("CU", "QU"), ("CD", "QD")],
}
_COMPARE_POWER_PARTS = {"Eq", "Ne", "Gt", "Lt", "Ge", "Le", "InRange", "OutRange", "Valid", "Invalid"}
_WIRE_POWER_SOURCE_PINS = {"out", "eno", "q", "qu", "qd"}


def build_power_graph(wires_elem: ET.Element, part_info_by_uid: Dict[str, Dict[str, Any]]) -> Dict[Any, Set[Any]]:
    """根据程序段 FlgNet 构建能流有向图，节点为 'RAIL' 或 (part_uid, pin_name)"""
    edges: Dict[Any, Set[Any]] = defaultdict(set)
    for wire in wires_elem:
        kids = list(wire)
        tags = [k.tag.split("}")[-1] for k in kids]
        ncs = [
            (k.attrib.get("UId", ""), k.attrib.get("Name", ""))
            for k, t in zip(kids, tags)
            if t == "NameCon"
        ]
        if not ncs:
            continue
        if "Powerrail" in tags:
            src: Any = "RAIL"
            targets = ncs
        else:
            # 数据线通常只有一个 NameCon 连向 IdentCon，不承载能流
            if "IdentCon" in tags and len(ncs) <= 1:
                continue
            srcs = [nc for nc in ncs if nc[1].lower() in _WIRE_POWER_SOURCE_PINS]
            if not srcs:
                continue
            src = srcs[0]
            targets = [nc for nc in ncs if nc != src]
        for dst in targets:
            edges[src].add(dst)

    for uid, info in part_info_by_uid.items():
        p_name = info.get("name", "")
        if p_name == "O":
            card_el = info["elem"].find("{*}TemplateValue[@Name='Card']")
            card_n = int(card_el.text) if card_el is not None and (card_el.text or "").isdigit() else 2
            for idx in range(1, card_n + 1):
                edges[(uid, f"in{idx}")].add((uid, "out"))
        elif p_name in _PART_POWER_LINKS:
            for in_pin, out_pin in _PART_POWER_LINKS[p_name]:
                edges[(uid, in_pin)].add((uid, out_pin))
        elif p_name in _COMPARE_POWER_PARTS:
            edges[(uid, "pre")].add((uid, "out"))
        else:
            edges[(uid, "en")].add((uid, "eno"))

    return edges


def _power_reachable(edges: Dict[Any, Set[Any]], goal: Any, blocked: Optional[Tuple[Any, Any]] = None) -> bool:
    seen = {"RAIL"}
    queue: deque = deque(["RAIL"])
    while queue:
        cur = queue.popleft()
        if cur == goal:
            return True
        for nxt in edges.get(cur, ()):
            if blocked and (cur, nxt) == blocked:
                continue
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def series_contacts_of_coil(
    edges: Dict[Any, Set[Any]],
    coil_uid: str,
    contact_uids: List[str],
) -> Optional[List[str]]:
    """
    计算指定线圈的必经串联触点 UId 列表（割点判定）：
    断开触点 X 的 in->out 通路后若左母线到线圈 in 不再可达，则 X 与该线圈串联。
    线圈不可达时返回 None。
    """
    goal = (coil_uid, "in")
    if not _power_reachable(edges, goal):
        return None
    return [
        cu for cu in contact_uids
        if not _power_reachable(edges, goal, blocked=((cu, "in"), (cu, "out")))
    ]


def validate_single_xml(
    file_path: str,
    known_tags: Optional[Set[str]] = None,
    known_db_members: Optional[Dict[str, Set[str]]] = None,
    tag_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[List[str], Dict[str, Any]]:
    """
    单文件全量静态语法与内部拓扑校验。
    返回:
      errors: 语法与格式错误列表
      block_audit_info: 用于跨文件深度审计的元数据（包含线圈调用、变量读写统计、程序段拓扑等）
    """
    errors: List[str] = []
    fname = os.path.basename(file_path)

    audit_info: Dict[str, Any] = {
        "file": fname,
        "block_type": None,
        "block_name": None,
        "coils": [],        # list of (var_name, nw_idx, coil_type)
        "var_writes": [],   # list of (var_name, nw_idx, reason)
        "var_reads": [],    # list of (var_name, nw_idx, reason)
        "referenced_vars": set(),
        "called_fcs": set(),
        "networks": [],     # list of dict: {nw_idx, contacts: [(var, negated)], coils: [(var, coil_type)]}
    }

    if not os.path.exists(file_path):
        return [f"文件不存在: {file_path}"], audit_info

    # 1. 检查 UTF-8 BOM
    with open(file_path, "rb") as f:
        head = f.read(3)
        if head != b"\xef\xbb\xbf":
            errors.append(f"[{fname}] 缺少 UTF-8 BOM 头 (必须以 0xEF 0xBB 0xBF 开头)")

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    # 2. 严禁 SCL 规则校验 (R2)
    if (
        "ProgrammingLanguage>SCL" in content
        or "FUNCTION_BLOCK" in content
        or "ORGANIZATION_BLOCK" in content
    ):
        errors.append(f"[{fname}] 严重违背最高原则 R2：检测到 SCL 代码特征！")

    # 3. XML 结构解析与 Engineering 版本检查
    try:
        root = ET.fromstring(content.lstrip("\ufeff"))
    except Exception as e:
        return [f"[{fname}] XML 语法解析失败: {str(e)}"], audit_info

    eng = root.find("Engineering")
    if eng is None or eng.attrib.get("version") != "V16":
        ver = eng.attrib.get("version") if eng is not None else "缺失"
        errors.append(f"[{fname}] Engineering version 必须为 'V16'，当前为 '{ver}'")

    # 识别块类型与块名称
    fc_elem = root.find(".//SW.Blocks.FC")
    fb_elem = root.find(".//SW.Blocks.FB")
    ob_elem = root.find(".//SW.Blocks.OB")
    db_elem = root.find(".//SW.Blocks.GlobalDB")
    idb_elem = root.find(".//SW.Blocks.InstanceDB")
    udt_elem = root.find(".//SW.Types.PlcStruct")
    tag_elem = root.find(".//SW.Tags.PlcTagTable")
    watch_elem = root.find(".//SW.WatchAndForceTables.PlcWatchTable")
    force_elem = root.find(".//SW.WatchAndForceTables.PlcForceTable")

    active_block_elem = None
    if fc_elem is not None:
        audit_info["block_type"] = "FC"
        active_block_elem = fc_elem
        name_el = fc_elem.find("AttributeList/Name")
        audit_info["block_name"] = (name_el.text or "").strip() if name_el is not None else fname
    elif fb_elem is not None:
        audit_info["block_type"] = "FB"
        active_block_elem = fb_elem
        name_el = fb_elem.find("AttributeList/Name")
        audit_info["block_name"] = (name_el.text or "").strip() if name_el is not None else fname
    elif ob_elem is not None:
        audit_info["block_type"] = "OB"
        active_block_elem = ob_elem
        name_el = ob_elem.find("AttributeList/Name")
        audit_info["block_name"] = (name_el.text or "").strip() if name_el is not None else fname
        sec_type_el = ob_elem.find("AttributeList/SecondaryType")
        if sec_type_el is None or not (sec_type_el.text or "").strip():
            errors.append(
                f"[{fname}] OB 组织块 AttributeList 中缺少 <SecondaryType>（主循环 OB1 必须为 ProgramCycle）"
            )
    elif db_elem is not None:
        audit_info["block_type"] = "DB"
        name_el = db_elem.find("AttributeList/Name")
        audit_info["block_name"] = (name_el.text or "").strip() if name_el is not None else fname
    elif idb_elem is not None:
        audit_info["block_type"] = "InstanceDB"
        name_el = idb_elem.find("AttributeList/Name")
        audit_info["block_name"] = (name_el.text or "").strip() if name_el is not None else fname
    elif udt_elem is not None:
        audit_info["block_type"] = "UDT"
        name_el = udt_elem.find("AttributeList/Name")
        audit_info["block_name"] = (name_el.text or "").strip() if name_el is not None else fname
    elif tag_elem is not None:
        audit_info["block_type"] = "TagTable"
    elif watch_elem is not None:
        audit_info["block_type"] = "WatchTable"
        name_el = watch_elem.find("AttributeList/Name")
        audit_info["block_name"] = (name_el.text or "").strip() if name_el is not None else fname
    elif force_elem is not None:
        audit_info["block_type"] = "ForceTable"
        name_el = force_elem.find("AttributeList/Name")
        audit_info["block_name"] = (name_el.text or "").strip() if name_el is not None else fname

    # 提取块接口局部变量 (Input/Output/InOut/Static/Temp/Constant/Return)
    local_members: Set[str] = set()
    has_interface_sections = False
    if active_block_elem is not None:
        sections_el = active_block_elem.find("AttributeList/Interface/{*}Sections")
        if sections_el is not None:
            has_interface_sections = True
            for sec_el in sections_el.findall("{*}Section"):
                for mem in sec_el.findall("{*}Member"):
                    m_name = mem.attrib.get("Name")
                    if m_name:
                        local_members.add(m_name)
    audit_info["called_blocks"] = set()

    # 检查块编程语言必须为 LAD (或 GlobalDB 的 DB)
    for prog_lang in root.findall(".//ProgrammingLanguage"):
        lang_val = (prog_lang.text or "").strip()
        if lang_val not in ("LAD", "DB"):
            errors.append(f"[{fname}] 非法的 ProgrammingLanguage: '{lang_val}'（只允许 LAD 或 DB）")

    # 4. 检查文档级 ID 唯一性
    doc_ids: Set[str] = set()
    for elem in root.iter():
        obj_id = elem.attrib.get("ID")
        if obj_id is not None:
            if obj_id in doc_ids:
                errors.append(f"[{fname}] 文档内存在重复的对象 ID='{obj_id}'")
            doc_ids.add(obj_id)

    # 5. 检查多语言 Culture 必须为 zh-CN
    for item in root.findall(".//MultilingualTextItem"):
        culture = item.find("AttributeList/Culture")
        if culture is not None and (culture.text or "").strip() != "zh-CN":
            errors.append(
                f"[{fname}] MultilingualTextItem Culture 必须为 'zh-CN', 当前为 '{culture.text}'"
            )

    # 提取 OB1 / FC / FB 中的 FC 与用户 FB 块调用
    for call_info in root.findall(".//{*}Call/{*}CallInfo"):
        call_blk_type = call_info.attrib.get("BlockType", "")
        if call_blk_type in ("FC", "FB"):
            callee = call_info.attrib.get("Name", "").strip().strip('"')
            if callee and callee not in SYSTEM_STANDARD_FBS:
                audit_info["called_blocks"].add(callee)
                if call_blk_type == "FC":
                    audit_info["called_fcs"].add(callee)

    # 6. 检查每个包含 FlgNet 的 CompileUnit 并深度分析程序段拓扑
    ns = {"flg": "http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4"}
    compile_units = root.findall(".//SW.Blocks.CompileUnit")

    for nw_idx, cu in enumerate(compile_units, start=1):
        flg = cu.find(".//flg:FlgNet", namespaces=ns)
        if flg is None:
            flg = cu.find(".//FlgNet")
        if flg is None:
            continue

        # 检查程序段 Title 与 Comment
        title_text = ""
        comment_text = ""
        for ml in cu.findall("ObjectList/MultilingualText"):
            comp_name = ml.attrib.get("CompositionName", "")
            txt_el = ml.find(".//MultilingualTextItem/AttributeList/Text")
            txt_val = (txt_el.text or "").strip() if txt_el is not None else ""
            if comp_name == "Title":
                title_text = txt_val
            elif comp_name == "Comment":
                comment_text = txt_val

        parts_elem = flg.find("flg:Parts", namespaces=ns)
        if parts_elem is None:
            parts_elem = flg.find("Parts")
        if parts_elem is not None and len(list(parts_elem)) > 0 and fname not in TEMPLATE_GALLERY_FILES:
            if not title_text:
                errors.append(f"[{fname}] 程序段 {nw_idx} 缺少中文标题 (Title)")
            if not comment_text and audit_info.get("block_type") in ("FC", "FB"):
                errors.append(
                    f"[{fname}] {audit_info.get('block_type')} 程序段 {nw_idx} 缺少调试指导注释 (Comment)"
                )

        # 提取所有 Part / Call / Access 的 UId 及其元数据
        parts_uids: Set[str] = set()
        access_uids: Set[str] = set()
        all_uids: Set[str] = set()
        uid_to_symbol: Dict[str, str] = {}
        part_info_by_uid: Dict[str, Dict[str, Any]] = {}

        if parts_elem is not None:
            for child in parts_elem:
                uid = child.attrib.get("UId")
                tag_name = child.tag.split("}")[-1]
                part_name = child.attrib.get("Name", "")
                if uid:
                    if uid in all_uids:
                        errors.append(f"[{fname}] 程序段 {nw_idx} 存在重复的 Part/Access UId: {uid}")
                    all_uids.add(uid)
                    if tag_name == "Access":
                        access_uids.add(uid)
                    elif tag_name == "Call":
                        parts_uids.add(uid)
                        ci = child.find("{*}CallInfo")
                        ci_name = ci.attrib.get("Name", "").strip('"') if ci is not None else ""
                        ci_btype = ci.attrib.get("BlockType", "") if ci is not None else ""
                        param_sections: Dict[str, str] = {}
                        if ci is not None:
                            for p_el in ci.findall("{*}Parameter"):
                                p_nm = p_el.attrib.get("Name")
                                if p_nm:
                                    param_sections[p_nm] = p_el.attrib.get("Section", "")
                        part_info_by_uid[uid] = {
                            "tag": "Call",
                            "name": ci_name,
                            "block_type": ci_btype,
                            "param_sections": param_sections,
                            "negated": False,
                            "elem": child,
                        }
                    else:
                        parts_uids.add(uid)
                        is_negated = child.find("{*}Negated[@Name='operand']") is not None
                        part_info_by_uid[uid] = {
                            "tag": tag_name,
                            "name": part_name,
                            "negated": is_negated,
                            "elem": child
                        }

                # 防呆检查：禁止 <Part Name="Call">
                if tag_name == "Part" and part_name == "Call":
                    errors.append(
                        f"[{fname}] 程序段 {nw_idx} 误用了 <Part Name=\"Call\">，必须直接使用 <Call UId=\"...\">"
                    )

                # 防呆检查：时间常量禁止使用 LiteralConstant
                if tag_name == "Access" and child.attrib.get("Scope") == "LiteralConstant":
                    c_val_el = child.find(".//{*}ConstantValue")
                    c_val = (c_val_el.text or "").strip() if c_val_el is not None else ""
                    if c_val.upper().startswith("T#"):
                        errors.append(
                            f"[{fname}] 程序段 {nw_idx} 时间常量 '{c_val}' 误用了 Scope=\"LiteralConstant\"，必须使用 Scope=\"TypedConstant\""
                        )

                # 提取局部变量符号引用并校验块接口存在性
                if tag_name == "Access" and child.attrib.get("Scope") == "LocalVariable":
                    comps = [
                        c.attrib.get("Name", "")
                        for c in child.findall(".//{*}Component")
                        if c.attrib.get("Name")
                    ]
                    if comps:
                        top_local = comps[0]
                        full_local = "#" + (".".join(comps[:2]) if len(comps) >= 2 else top_local)
                        uid_to_symbol[uid] = full_local
                        audit_info["referenced_vars"].add(full_local)
                        audit_info["referenced_vars"].add(f"#{top_local}")
                        if has_interface_sections and top_local not in local_members:
                            errors.append(
                                f"[{fname}] 程序段 {nw_idx} 引用了块接口中未定义的局部变量: '{top_local}'"
                            )

                # 提取全局变量符号引用并做存在性校验
                if tag_name == "Access" and child.attrib.get("Scope") == "GlobalVariable":
                    comps = [
                        c.attrib.get("Name", "")
                        for c in child.findall(".//{*}Component")
                        if c.attrib.get("Name")
                    ]
                    if comps:
                        root_sym = comps[0]
                        full_sym = ".".join(comps) if len(comps) >= 2 else root_sym
                        uid_to_symbol[uid] = full_sym
                        audit_info["referenced_vars"].add(root_sym)
                        audit_info["referenced_vars"].add(full_sym)

                        if known_tags is not None and root_sym not in known_tags and fname not in TEMPLATE_GALLERY_FILES:
                            errors.append(
                                f"[{fname}] 程序段 {nw_idx} 引用了未在变量表或 DB 中定义的符号: '{root_sym}'"
                            )
                        if (
                            len(comps) >= 2
                            and known_db_members is not None
                            and root_sym in known_db_members
                            and comps[1] not in known_db_members[root_sym]
                            and fname not in TEMPLATE_GALLERY_FILES
                        ):
                            errors.append(
                                f"[{fname}] 程序段 {nw_idx} 引用了 DB '{root_sym}' 中不存在的成员: '{comps[1]}'"
                            )
                        if len(comps) >= 3:
                            timer_set = known_db_members.get("__TIMER_MEMBERS__", set()) if known_db_members is not None else set()
                            is_timer_or_counter = f"{root_sym}.{comps[1]}" in timer_set
                            if is_timer_or_counter and comps[2] not in TIMER_SUBMEMBERS:
                                errors.append(
                                    f"[{fname}] 程序段 {nw_idx} 引用了非法定时器/计数器子成员: '{'.'.join(comps)}'"
                                )

                # 检查 Part(TON/TOF/TP) 与 CallInfo(FB) 的 Instance 引用 (GlobalVariable / LocalVariable)
                inst_el = child.find("{*}Instance")
                if inst_el is None and tag_name == "Call":
                    inst_el = child.find("{*}CallInfo/{*}Instance")
                if inst_el is not None:
                    inst_scope = inst_el.attrib.get("Scope", "")
                    inst_uid = inst_el.attrib.get("UId")
                    if inst_uid:
                        if inst_uid in all_uids:
                            errors.append(
                                f"[{fname}] 程序段 {nw_idx} 存在重复的 Instance UId: {inst_uid}"
                            )
                        all_uids.add(inst_uid)
                    comps = [
                        c.attrib.get("Name", "")
                        for c in inst_el.findall(".//{*}Component")
                        if c.attrib.get("Name")
                    ]
                    if comps:
                        inst_root_name = comps[0]
                        if inst_scope == "GlobalVariable":
                            audit_info["referenced_vars"].add(inst_root_name)
                            if known_tags is not None and inst_root_name not in known_tags and fname not in TEMPLATE_GALLERY_FILES:
                                errors.append(
                                    f"[{fname}] 程序段 {nw_idx} Instance 引用了未定义的符号/DB: '{inst_root_name}'"
                                )
                            if (
                                len(comps) >= 2
                                and known_db_members is not None
                                and inst_root_name in known_db_members
                                and comps[1] not in known_db_members[inst_root_name]
                                and fname not in TEMPLATE_GALLERY_FILES
                            ):
                                errors.append(
                                    f"[{fname}] 程序段 {nw_idx} Instance 引用了 DB '{inst_root_name}' 中不存在的成员: '{comps[1]}'"
                                )
                        elif inst_scope == "LocalVariable":
                            audit_info["referenced_vars"].add(f"#{inst_root_name}")
                            if has_interface_sections and inst_root_name not in local_members:
                                errors.append(
                                    f"[{fname}] 程序段 {nw_idx} 多重背景 Instance 引用了未定义的块 Static 成员: '{inst_root_name}'"
                                )

        # 提取 Wires 中引用的 UId 并检查接线拓扑
        wires_elem = flg.find("flg:Wires", namespaces=ns)
        if wires_elem is None:
            wires_elem = flg.find("Wires")
        used_ident_uids: Set[str] = set()
        used_part_uids: Set[str] = set()
        part_connected_pins: Dict[str, Set[str]] = {}
        nw_contacts: List[Tuple[str, bool]] = []
        nw_coils: List[Tuple[str, str]] = []
        contact_uid_to_var: Dict[str, str] = {}
        coil_uid_list: List[Tuple[str, str, str]] = []

        if wires_elem is not None:
            for wire in wires_elem:
                w_uid = wire.attrib.get("UId")
                if w_uid:
                    if w_uid in all_uids:
                        errors.append(f"[{fname}] 程序段 {nw_idx} Wire 存在重复的 UId: {w_uid}")
                    all_uids.add(w_uid)

                children = list(wire)
                if len(children) < 2:
                    errors.append(f"[{fname}] 程序段 {nw_idx} Wire (UId={w_uid}) 连接端点不足 2 个")

                ident_uids_in_wire = []
                name_cons_in_wire = []

                for sub in children:
                    sub_uid = sub.attrib.get("UId")
                    sub_tag = sub.tag.split("}")[-1]
                    if sub_tag == "OpenCon" and sub_uid:
                        if sub_uid in all_uids:
                            errors.append(
                                f"[{fname}] 程序段 {nw_idx} OpenCon 存在重复的 UId: {sub_uid}"
                            )
                        all_uids.add(sub_uid)
                    elif sub_uid:
                        if sub_tag == "IdentCon":
                            used_ident_uids.add(sub_uid)
                            ident_uids_in_wire.append(sub_uid)
                        elif sub_tag == "NameCon":
                            used_part_uids.add(sub_uid)
                            pin_name = sub.attrib.get("Name", "")
                            part_connected_pins.setdefault(sub_uid, set()).add(pin_name)
                            name_cons_in_wire.append((sub_uid, pin_name))

                # 精确区分 Wire 内变量读写与触点/线圈收集
                has_output_source_pin = any(
                    pin.lower() in SOURCE_OUT_PINS for _, pin in name_cons_in_wire
                )
                for id_uid in ident_uids_in_wire:
                    sym_name = uid_to_symbol.get(id_uid)
                    if not sym_name:
                        continue
                    for p_uid, pin in name_cons_in_wire:
                        p_info = part_info_by_uid.get(p_uid, {})
                        p_tag = p_info.get("tag", "")
                        p_part_name = p_info.get("name", "")
                        if p_tag == "Call":
                            param_sec = p_info.get("param_sections", {}).get(pin, "")
                            if param_sec == "Output" or pin == "Ret_Val" or pin.lower() == "eno":
                                audit_info["var_writes"].append((sym_name, nw_idx, f"Call:{p_part_name}:{pin}"))
                            elif param_sec == "InOut":
                                audit_info["var_reads"].append((sym_name, nw_idx, f"Call:{p_part_name}:{pin}"))
                                audit_info["var_writes"].append((sym_name, nw_idx, f"Call:{p_part_name}:{pin}"))
                            else:
                                audit_info["var_reads"].append((sym_name, nw_idx, f"Call:{p_part_name}:{pin}"))
                        elif pin == "operand":
                            if p_part_name in ("Coil", "SCoil", "RCoil", "NCoil"):
                                audit_info["coils"].append((sym_name, nw_idx, p_part_name))
                                audit_info["var_writes"].append((sym_name, nw_idx, p_part_name))
                                nw_coils.append((sym_name, p_part_name))
                                coil_uid_list.append((p_uid, sym_name, p_part_name))
                            elif p_part_name == "Contact":
                                audit_info["var_reads"].append((sym_name, nw_idx, "Contact"))
                                nw_contacts.append((sym_name, bool(p_info.get("negated"))))
                                contact_uid_to_var[p_uid] = sym_name
                            else:
                                audit_info["var_reads"].append((sym_name, nw_idx, f"{p_part_name}:{pin}"))
                        elif pin == "bit":
                            # PBox/NBox 边沿存储位每周期同时读取上一周期状态并写入当前周期状态
                            audit_info["var_reads"].append((sym_name, nw_idx, f"{p_part_name}:bit"))
                            audit_info["var_writes"].append((sym_name, nw_idx, f"{p_part_name}:bit"))
                        elif pin.lower() in SOURCE_OUT_PINS:
                            audit_info["var_writes"].append((sym_name, nw_idx, f"{p_part_name}:{pin}"))
                        else:
                            # 当同 Wire 中存在元件输出引脚（例如 Move.out1 -> IdentCon 并分叉）时，
                            # 只对真正属于读取端口的引脚计入 reads，避免把目标写回的另一端误判为读取
                            if not has_output_source_pin:
                                audit_info["var_reads"].append((sym_name, nw_idx, f"{p_part_name}:{pin}"))

        nw_series_by_coil: Dict[str, List[Tuple[str, bool]]] = {}
        if wires_elem is not None and coil_uid_list:
            edges = build_power_graph(wires_elem, part_info_by_uid)
            c_uids = list(contact_uid_to_var.keys())
            for coil_uid, coil_var, _ in coil_uid_list:
                sc = series_contacts_of_coil(edges, coil_uid, c_uids)
                if sc is None:
                    if fname not in TEMPLATE_GALLERY_FILES:
                        errors.append(
                            f"[{fname}] 程序段 {nw_idx} 线圈 '{coil_var}' 与左母线能流不连通（死线圈）"
                        )
                    continue
                cur_series = [
                    (contact_uid_to_var[u], bool(part_info_by_uid[u].get("negated")))
                    for u in sc
                ]
                if coil_var not in nw_series_by_coil:
                    nw_series_by_coil[coil_var] = cur_series
                else:
                    prev_set = set(nw_series_by_coil[coil_var])
                    nw_series_by_coil[coil_var] = [x for x in cur_series if x in prev_set]

        audit_info["networks"].append({
            "nw_idx": nw_idx,
            "contacts": nw_contacts,
            "coils": nw_coils,
            "series": nw_series_by_coil,
        })

        # 校验核心 Part（Contact/Coil/PBox/O/TON 等）必需引脚完整性
        for p_uid, p_info in part_info_by_uid.items():
            p_tag = p_info.get("tag", "")
            p_name = p_info.get("name", "")
            if p_tag != "Part":
                continue
            connected = part_connected_pins.get(p_uid, set())
            if p_name in REQUIRED_PART_PINS:
                for req_pin in REQUIRED_PART_PINS[p_name]:
                    if req_pin not in connected:
                        extra_hint = "（未使用的 ET 引脚必须连接 OpenCon）" if req_pin == "ET" else ""
                        errors.append(
                            f"[{fname}] 程序段 {nw_idx} {p_name} 元件 (UId={p_uid}) 缺少必需引脚连接: '{req_pin}'{extra_hint}"
                        )
            elif p_name == "O":
                card_el = p_info["elem"].find("{*}TemplateValue[@Name='Card']")
                card_n = int(card_el.text) if card_el is not None and (card_el.text or "").isdigit() else 2
                req_o_pins = {f"in{i}" for i in range(1, card_n + 1)} | {"out"}
                for req_pin in sorted(req_o_pins):
                    if req_pin not in connected:
                        errors.append(
                            f"[{fname}] 程序段 {nw_idx} 并联节点 O (UId={p_uid}) 缺少必需引脚连接: '{req_pin}'"
                        )

        # 校验：所有 Access 必须在 Wires 中被作为 IdentCon 使用
        orphan_access = access_uids - used_ident_uids
        if orphan_access:
            errors.append(
                f"[{fname}] 程序段 {nw_idx} 存在未被接线使用的孤立 Access UID (博途将拒绝导入): {orphan_access}"
            )

        # 校验：所有 Part/Call 必须在 Wires 中被作为 NameCon 使用
        orphan_parts = parts_uids - used_part_uids
        if orphan_parts:
            errors.append(
                f"[{fname}] 程序段 {nw_idx} 存在未接线的孤立元件 Part/Call UID: {orphan_parts}"
            )

    return errors, audit_info


def compare_eplan_with_tia_hw(
    eplan_io_path: str,
    tia_status_json_path: Optional[str] = None,
    tag_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    fallback_project_dir: Optional[str] = None,
) -> Tuple[List[str], List[str], List[str]]:
    """
    模块 2 静态比对引擎：
    1. 解析 EPLAN 识图清单 / I/O 分配表 Markdown 中的 %I, %Q, %IW, %QW 地址；
    2. 与 TagTable XML 中的已声明变量地址交叉核对；
    3. 若提供 --tia-status-json，则对图纸地址与 TagTable 变量地址的起止位偏移进行博途硬件组态 IoRanges 越界核对。
    返回: (hw_errors, hw_warnings, report_lines)
    """
    hw_errors: List[str] = []
    hw_warnings: List[str] = []
    report_lines: List[str] = []

    resolved_eplan = resolve_project_path(eplan_io_path) if eplan_io_path else None
    if (not resolved_eplan or not os.path.exists(resolved_eplan)) and fallback_project_dir:
        for cand_name in ("01_识图清单.md", "05_现场调试交付三表.md"):
            cand_p = os.path.join(fallback_project_dir, cand_name)
            if os.path.exists(cand_p):
                resolved_eplan = cand_p
                break

    eplan_addrs: List[Tuple[str, str]] = []
    if resolved_eplan and os.path.exists(resolved_eplan):
        with open(resolved_eplan, "r", encoding="utf-8", errors="replace") as f:
            eplan_text = f.read()

        seen_eplan_addrs: Set[str] = set()
        for line in eplan_text.splitlines():
            if "|" not in line:
                continue
            m = re.search(r"(%[IQ](?:B|W|D)?\d+(?:\.\d+)?)", line, re.IGNORECASE)
            if m:
                addr = m.group(1).upper()
                if addr not in seen_eplan_addrs:
                    seen_eplan_addrs.add(addr)
                    cols = [c.strip(" `") for c in line.strip().strip("|").split("|")]
                    desc = cols[1] if len(cols) > 1 else addr
                    eplan_addrs.append((addr, desc))

        report_lines.append(f"\n--- EPLAN 图纸 I/O 映射对账结果 (源: {resolved_eplan}) ---")
        report_lines.append(f"  图纸识别物理 I/O 点数: {len(eplan_addrs)} 点")

        # 1. 与 TagTable XML 已定义地址交叉核对
        if tag_metadata is not None:
            defined_iq_addrs = {
                meta.get("address", "").upper(): tname
                for tname, meta in tag_metadata.items()
                if meta.get("address", "").upper().startswith(("%I", "%Q"))
            }
            missing_in_xml = [
                f"{addr} ({desc})" for addr, desc in eplan_addrs if addr not in defined_iq_addrs
            ]
            if missing_in_xml:
                for item in missing_in_xml:
                    hw_errors.append(
                        f"EPLAN 与变量表不一致 ERROR: 图纸声明的物理地址 {item} 未在 TagTable XML 中定义！"
                    )
            else:
                report_lines.append(
                    f"  🟢 图纸 I/O 地址与 TagTable XML 100% 吻合 (共核对 {len(eplan_addrs)} 点)"
                )
    elif eplan_io_path:
        hw_warnings.append(f"未找到指定的 EPLAN I/O 映射文件: {eplan_io_path}")

    # 2. 若提供了博途硬件状态 JSON，执行硬件组态地址区间与首尾位偏移越界核对
    if tia_status_json_path:
        resolved_json = resolve_project_path(tia_status_json_path)
        if not resolved_json or not os.path.exists(resolved_json):
            hw_warnings.append(f"未找到博途硬件状态 JSON 文件: {tia_status_json_path}")
            return hw_errors, hw_warnings, report_lines

        try:
            with open(resolved_json, "r", encoding="utf-8-sig") as jf:
                env_info = json.load(jf)
        except Exception as e:
            hw_errors.append(f"解析博途硬件状态 JSON 失败: {e}")
            return hw_errors, hw_warnings, report_lines

        proj_data = env_info.get("Project") or {}
        hw_modules = proj_data.get("HardwareModules") or []
        configured_ranges = []
        for mod in hw_modules:
            m_name = mod.get("Name", "")
            slot = mod.get("PositionNumber", "")
            for r in mod.get("IoRanges", []):
                start_b = int(r.get("StartAddress", 0))
                eff_bits = r.get("EffectiveBits") or r.get("LengthBits")
                if eff_bits is not None and int(eff_bits) > 0:
                    start_bit = start_b * 8
                    end_bit = start_bit + int(eff_bits) - 1
                else:
                    end_b = int(r.get("EndAddress", start_b))
                    start_bit = start_b * 8
                    end_bit = (end_b + 1) * 8 - 1
                configured_ranges.append({
                    "module": m_name,
                    "slot": slot,
                    "ioType": r.get("IoType", ""),
                    "start_bit": start_bit,
                    "end_bit": end_bit,
                })

        # 合并待校验地址：优先图纸地址，若无图纸文件则从 TagTable 提取 %I/%Q
        addrs_to_check: List[Tuple[str, str, str]] = [(a, d, "Bool") for a, d in eplan_addrs]
        if not addrs_to_check and tag_metadata:
            for tname, meta in tag_metadata.items():
                addr_u = meta.get("address", "").upper()
                if addr_u.startswith(("%I", "%Q")):
                    addrs_to_check.append((addr_u, tname, meta.get("dataType", "Bool")))

        for addr, desc, dtype in addrs_to_check:
            io_type = "Input" if addr.startswith("%I") else "Output"
            span = parse_address_bit_span(addr, dtype)
            if span is None:
                continue
            _, start_bit, bit_len = span
            end_bit = start_bit + bit_len - 1

            matched = any(
                cr["ioType"] == io_type and cr["start_bit"] <= start_bit and end_bit <= cr["end_bit"]
                for cr in configured_ranges
            )
            if not matched:
                hw_errors.append(
                    f"硬件组态地址越界 ERROR: 地址 '{addr}' ({desc}, 位范围={start_bit}..{end_bit}) 超出博途当前已组态的所有 {io_type} 模块地址范围，请在博途设备视图中补插扩展模块或调整起始地址！"
                )

    return hw_errors, hw_warnings, report_lines


def _audit_safety_rules(
    audit_list: List[Dict[str, Any]],
    tag_metadata: Dict[str, Dict[str, Any]],
    all_errors: List[str],
    all_warnings: List[str],
) -> None:
    """
    执行安全规则 S2 ~ S5 自动审计（基于能流拓扑的必经串联触点判定）：
      - S2: NC 安全输入信号严禁在驱动非报警/非指示灯的回路中以常闭触点 (Negated) 扫描
      - S3: 物理执行器线圈 (%Q 接触器/电磁阀/驱动) 必须在串联主干路上直接或间接串入安全允许条件
      - S4: 同 FC 内正反转、星三角接触器必须在串联主干路上互相串入对方的常闭触点 (软件互锁)
      - S5: 存在热继电器输入时，电机接触器线圈 (%Q) 串联主干路必须直接或间接串入热继常开扫描
    """
    # 1. 预计算安全许可传递闭包（支持经主接触器等上游线圈常开串联传递，禁止自身自锁绕过）
    safety_covered: Set[str] = set(SAFETY_PERMISSIVE_TAGS)
    for tname in tag_metadata:
        if _is_safety_permissive_var(tname, tag_metadata):
            safety_covered.add(tname)
    for audit_info in audit_list:
        for nw in audit_info.get("networks", []):
            for cv, _ in nw.get("contacts", []):
                if _is_safety_permissive_var(cv, tag_metadata):
                    safety_covered.add(cv)

    changed = True
    while changed:
        changed = False
        for audit_info in audit_list:
            fname = audit_info.get("file", "")
            if fname in TEMPLATE_GALLERY_FILES or audit_info.get("block_type") not in ("FC", "FB"):
                continue
            for nw in audit_info.get("networks", []):
                coils = nw["coils"]
                if not coils:
                    continue
                coil_names = [c[0] for c in coils]
                if all(_is_alarm_or_lamp_target(cn) for cn in coil_names):
                    continue
                series_map = nw.get("series", {})
                for cname, ctype in coils:
                    if ctype not in ("Coil", "SCoil"):
                        continue
                    series_cts = series_map.get(cname, [])
                    if any((not is_neg) and cv != cname and cv in safety_covered for cv, is_neg in series_cts):
                        if cname not in safety_covered:
                            safety_covered.add(cname)
                            changed = True

    # 2. 预计算每个变量受哪些热继电器保护（仅统计串联主干路常开触点传递，排除自锁与 RCoil）
    fr_tags: Set[str] = {
        tname for tname, meta in tag_metadata.items()
        if meta.get("address", "").upper().startswith("%I")
        and ("热继" in tname or "过载" in tname or re.match(r"^FR\d*(?:_|$)", tname))
    }
    fr_coverage: Dict[str, Set[str]] = {fr: {fr} for fr in fr_tags}
    if fr_tags:
        changed = True
        while changed:
            changed = False
            for audit_info in audit_list:
                fname = audit_info.get("file", "")
                if fname in TEMPLATE_GALLERY_FILES or audit_info.get("block_type") not in ("FC", "FB"):
                    continue
                for nw in audit_info.get("networks", []):
                    coils = nw["coils"]
                    if not coils:
                        continue
                    coil_names = [c[0] for c in coils]
                    if all(_is_alarm_or_lamp_target(cn) for cn in coil_names):
                        continue
                    series_map = nw.get("series", {})
                    for cname, ctype in coils:
                        if ctype not in ("Coil", "SCoil"):
                            continue
                        nw_frs: Set[str] = set()
                        for cv, is_neg in series_map.get(cname, []):
                            if (not is_neg) and cv != cname and (cv in fr_coverage):
                                nw_frs.update(fr_coverage[cv])
                        if nw_frs:
                            cur_set = fr_coverage.setdefault(cname, set())
                            before_len = len(cur_set)
                            cur_set.update(nw_frs)
                            if len(cur_set) > before_len:
                                changed = True

    for audit_info in audit_list:
        fname = audit_info.get("file", "")
        # 纯样板画廊文件豁免业务逻辑审计
        if fname in TEMPLATE_GALLERY_FILES or audit_info.get("block_type") not in ("FC", "FB"):
            continue

        networks = audit_info.get("networks", [])

        # S2, S3 & S5 单段校验
        for nw in networks:
            nw_idx = nw["nw_idx"]
            contacts = nw["contacts"]
            coils = nw["coils"]
            series_map = nw.get("series", {})
            if not coils:
                continue

            coil_names = [c[0] for c in coils]
            all_coils_are_alarm_or_lamp = all(_is_alarm_or_lamp_target(cname) for cname in coil_names)

            # S2: 安全信号在控制使能回路不得取反 (Negated)
            if not all_coils_are_alarm_or_lamp:
                for c_var, is_neg in contacts:
                    if is_neg and _is_safety_input_var(c_var, tag_metadata):
                        all_errors.append(
                            f"[{fname} 程序段 {nw_idx}] 安全信号极性违规 ERROR (S2): "
                            f"常闭安全信号 '{c_var}' 在控制输出回路 ({', '.join(coil_names)}) 中被写成了常闭触点 (Negated)！"
                            f"按 Fail-Safe 原则应使用常开触点扫描。"
                        )

            # S3 & S5: 驱动物理执行器 %Q 必须在串联主干路串入安全允许条件；电机接触器须串联热继
            for cname, ctype in coils:
                if ctype not in ("Coil", "SCoil"):
                    continue
                meta = tag_metadata.get(cname, {})
                addr = meta.get("address", "").upper()
                is_q_actuator = (
                    addr.startswith("%Q")
                    and not _is_alarm_or_lamp_target(cname)
                    and any(k in cname for k in ACTUATOR_PREFIX_OR_KEYWORDS)
                )
                if is_q_actuator:
                    series_cts = series_map.get(cname, [])
                    has_safety = any(
                        (not is_neg) and cv != cname and cv in safety_covered
                        for cv, is_neg in series_cts
                    )
                    if not has_safety:
                        all_errors.append(
                            f"[{fname} 程序段 {nw_idx}] 安全条件缺失 ERROR (S3): "
                            f"物理执行器输出 '{cname}' ({addr}) 的串联控制主干路未串入 'M_安全条件满足'、'M_急停正常' 或有效的上游安全条件！"
                        )

                    is_q_motor = any(k in cname for k in ("KM", "电机", "正转", "反转", "星", "角"))
                    if fr_tags and is_q_motor:
                        covered_frs = fr_coverage.get(cname, set())
                        motor_id_m = re.search(r"(\d+号电机)", cname)
                        expected_frs = (
                            {f for f in fr_tags if motor_id_m.group(1) in f}
                            if motor_id_m else set()
                        )
                        if expected_frs:
                            if not (covered_frs & expected_frs):
                                all_errors.append(
                                    f"[{fname} 程序段 {nw_idx}] 热继保护缺失 ERROR (S5): "
                                    f"电机输出 '{cname}' ({addr}) 的串联控制回路未串入对应热继电器常开扫描 ({', '.join(sorted(expected_frs))})！"
                                )
                        elif not covered_frs:
                            all_errors.append(
                                f"[{fname} 程序段 {nw_idx}] 热继保护缺失 ERROR (S5): "
                                f"电机输出 '{cname}' ({addr}) 的串联控制回路未直接或间接串入热继电器常开扫描 ({', '.join(sorted(fr_tags))})！"
                            )

        # S4: 同 FC 内正反转 / 星三角软件互锁检查（仅认串联主干路中的常闭互锁触点）
        fc_q_coils: List[Tuple[int, str, List[Tuple[str, bool]]]] = []
        for nw in networks:
            series_map = nw.get("series", {})
            for cname, ctype in nw["coils"]:
                if ctype not in ("Coil", "SCoil"):
                    continue
                meta = tag_metadata.get(cname, {})
                addr = meta.get("address", "").upper()
                if addr.startswith("%Q") and not _is_alarm_or_lamp_target(cname):
                    fc_q_coils.append((nw["nw_idx"], cname, series_map.get(cname, [])))

        def _check_mutual_pair(kw_a: str, kw_b: str, desc_label: str) -> None:
            group_a = [item for item in fc_q_coils if kw_a in item[1]]
            group_b = [item for item in fc_q_coils if kw_b in item[1]]
            if group_a and group_b:
                for nw_a, name_a, contacts_a in group_a:
                    if not any((name_b, True) in contacts_a for _, name_b, _ in group_b):
                        all_errors.append(
                            f"[{fname} 程序段 {nw_a}] {desc_label}软件互锁缺失 ERROR (S4): "
                            f"输出 '{name_a}' 未串入互斥输出 {[x[1] for x in group_b]} 的常闭触点 (Negated)！"
                        )
                for nw_b, name_b, contacts_b in group_b:
                    if not any((name_a, True) in contacts_b for _, name_a, _ in group_a):
                        all_errors.append(
                            f"[{fname} 程序段 {nw_b}] {desc_label}软件互锁缺失 ERROR (S4): "
                            f"输出 '{name_b}' 未串入互斥输出 {[x[1] for x in group_a]} 的常闭触点 (Negated)！"
                        )

        _check_mutual_pair("正转", "反转", "正反转")
        _check_mutual_pair("星", "角", "星三角")


def validate_project_xmls(
    xml_dir: str,
    extra_tag_dirs: Optional[List[str]] = None,
    eplan_io_path: Optional[str] = None,
    tia_status_json_path: Optional[str] = None,
) -> ValidationResult:
    """
    批量校验整个工程生成的 XML 目录。
    执行全维度静态语法校验、位级内存重叠检测、安全规则 S1~S5、双线圈冲突审计、悬空 I/O 检测、M 变量读写闭环及 OB1 调用完整性审计。
    """
    all_errors: List[str] = []
    all_warnings: List[str] = []

    xml_dir = resolve_project_path(xml_dir) or xml_dir
    if not os.path.exists(xml_dir):
        return ValidationResult(False, [f"目录不存在: {xml_dir}"], [])

    xml_files = [
        os.path.join(xml_dir, f)
        for f in sorted(os.listdir(xml_dir))
        if f.lower().endswith(".xml")
    ]

    # 自动关联项目目录下的 02_变量表（如果当前传入的是 03_梯形图XML）
    parent_dir = os.path.dirname(os.path.abspath(xml_dir))
    sibling_tag_dir = os.path.join(parent_dir, "02_变量表")
    candidate_dirs = list(extra_tag_dirs or [])
    if os.path.isdir(sibling_tag_dir):
        candidate_dirs.append(sibling_tag_dir)

    all_scan_files = list(xml_files)
    for d in candidate_dirs:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.lower().endswith(".xml"):
                    full_p = os.path.join(d, f)
                    if full_p not in all_scan_files:
                        all_scan_files.append(full_p)

    if not all_scan_files:
        return ValidationResult(False, ["未找到任何待校验的 .xml 文件"], [])

    known_tags, known_db_members, tag_errors, tag_metadata = collect_defined_symbols(all_scan_files)
    all_errors.extend(tag_errors)

    # 若存在 export-all 生成的系统/工艺符号列表，将其合入合法全局符号集合
    sys_sym_file = os.path.join(xml_dir, "tia_system_symbols.json")
    if os.path.exists(sys_sym_file):
        try:
            with open(sys_sym_file, "r", encoding="utf-8-sig") as sf:
                sys_data = json.load(sf)
            for sname in sys_data.get("SystemSymbols", []) or []:
                if isinstance(sname, str) and sname.strip():
                    known_tags.add(sname.strip())
        except Exception:
            pass

    check_tags = known_tags if len(known_tags) > 0 else None
    check_dbs = known_db_members if len(known_db_members) > 0 else None

    # 存储跨文件审计数据
    coil_audit: Dict[str, List[Tuple[str, int, str]]] = {}
    all_referenced_symbols: Set[str] = set()
    global_var_writes: Dict[str, int] = {}
    global_var_reads: Dict[str, int] = {}
    found_callable_blocks: Set[str] = set()
    all_called_blocks: Set[str] = set()
    has_ob1 = False
    collected_audits: List[Dict[str, Any]] = []

    for f in all_scan_files:
        errs, audit_info = validate_single_xml(
            f,
            known_tags=check_tags,
            known_db_members=check_dbs,
            tag_metadata=tag_metadata,
        )
        all_errors.extend(errs)
        collected_audits.append(audit_info)

        b_type = audit_info.get("block_type")
        b_name = audit_info.get("block_name")
        fname = audit_info.get("file")

        if b_type in ("FC", "FB") and b_name and fname not in TEMPLATE_GALLERY_FILES:
            found_callable_blocks.add(b_name)
        if b_type == "OB":
            has_ob1 = True
        if b_type in ("OB", "FC", "FB"):
            all_called_blocks.update(audit_info.get("called_blocks", set()))
            all_called_blocks.update(audit_info.get("called_fcs", set()))

        all_referenced_symbols.update(audit_info.get("referenced_vars", set()))

        # 收集线圈信息（排除纯指令积木画廊样板；局部变量加块名前缀隔离）
        for var_name, nw_idx, coil_type in audit_info.get("coils", []):
            if fname in TEMPLATE_GALLERY_FILES:
                continue
            coil_key = f"{b_name}::{var_name}" if var_name.startswith("#") else var_name
            coil_audit.setdefault(coil_key, []).append((fname, nw_idx, coil_type))

        # 收集变量写入与读取频次
        for var_name, _, _ in audit_info.get("var_writes", []):
            global_var_writes[var_name] = global_var_writes.get(var_name, 0) + 1
        for var_name, _, _ in audit_info.get("var_reads", []):
            global_var_reads[var_name] = global_var_reads.get(var_name, 0) + 1

    # =========================================================================
    # 安全合规审计 (S2 ~ S4)
    # =========================================================================
    _audit_safety_rules(collected_audits, tag_metadata, all_errors, all_warnings)

    # =========================================================================
    # 工业级审计 1: 双线圈检测 (Multiple Coil Check)
    # =========================================================================
    for var_name, occurrences in coil_audit.items():
        normal_coils = [occ for occ in occurrences if occ[2] == "Coil"]
        if len(normal_coils) > 1:
            detail_list = [f"[{f} 程序段 {nw}]" for f, nw, _ in normal_coils]
            all_errors.append(
                f"双线圈冲突 ERROR: 全局变量 '{var_name}' 在以下程序段被普通线圈 (Coil) 重复驱动: "
                f"{', '.join(detail_list)}（严重违背 PLC 逻辑确定性原则，后者将无条件覆盖前者，请使用 SCoil/RCoil 置位复位或合并并联分支！）"
            )

    # =========================================================================
    # 工业级审计 2: 悬空实物 I/O 检测 (Unused I/O Check)
    # =========================================================================
    for tag_name, meta in tag_metadata.items():
        addr = meta.get("address", "")
        comment = meta.get("comment", "")
        if addr.upper().startswith("%I") or addr.upper().startswith("%Q"):
            if "备用" in tag_name or "备用" in comment:
                continue
            if tag_name not in all_referenced_symbols:
                all_warnings.append(
                    f"[{meta.get('file')}] 悬空实物 I/O 告警: 变量 '{tag_name}' ({addr}) 在所有梯形图块中均未被引用，图纸实物信号未进入梯形图逻辑！"
                )

    # =========================================================================
    # 工业级审计 3: M 变量读写闭环检测 (Read/Write Balance Check)
    # =========================================================================
    for tag_name, meta in tag_metadata.items():
        addr = meta.get("address", "")
        if addr.upper().startswith("%M"):
            if tag_name in SIEMENS_SYSTEM_CLOCK_TAGS or any(k in tag_name for k in ("报警汇总", "HMI", "预留", "备用", "指示", "仿真")):
                continue
            writes = global_var_writes.get(tag_name, 0)
            reads = global_var_reads.get(tag_name, 0)

            if writes > 0 and reads == 0:
                all_warnings.append(
                    f"[{meta.get('file')}] M 变量读写告警: 中间变量 '{tag_name}' ({addr}) 只写未读 (写入 {writes} 次, 读取 0 次)，可能存在输出未被下游逻辑引用的悬空状态"
                )
            elif writes == 0 and reads > 0:
                all_warnings.append(
                    f"[{meta.get('file')}] M 变量读写告警: 中间变量 '{tag_name}' ({addr}) 只读未写 (写入 0 次, 读取 {reads} 次)，该变量从未被赋值，可能永久为 0"
                )
            elif writes == 0 and reads == 0:
                all_warnings.append(
                    f"[{meta.get('file')}] M 变量读写告警: 中间变量 '{tag_name}' ({addr}) 未在任何梯形图逻辑中被引用"
                )

    # =========================================================================
    # 工业级审计 4: OB1 / 跨块调用完整性检测 (Call Integrity Check)
    # =========================================================================
    if found_callable_blocks:
        if not has_ob1:
            all_errors.append("OB1 缺失 ERROR: 未找到 OB1 主组织块 XML，生成的 FC/FB 功能块必须在 OB1 调用链中被执行！")
        else:
            uncalled_blocks = found_callable_blocks - all_called_blocks
            for ublk in sorted(uncalled_blocks):
                all_errors.append(
                    f"调用完整性违规 ERROR (漏调 FC/FB): 功能块 '{ublk}' 已生成但未在 OB1 或上层 FC/FB 中被调用，PLC 下载后该块逻辑将无法周期执行！"
                )

    # =========================================================================
    # 工业级审计 5: EPLAN I/O 与变量表/博途硬件组态对账
    # =========================================================================
    hw_report_lines: List[str] = []
    if eplan_io_path or tia_status_json_path:
        hw_errs, hw_warns, hw_report_lines = compare_eplan_with_tia_hw(
            eplan_io_path or "",
            tia_status_json_path=tia_status_json_path,
            tag_metadata=tag_metadata,
            fallback_project_dir=parent_dir,
        )
        all_errors.extend(hw_errs)
        all_warnings.extend(hw_warns)

    is_ok = len(all_errors) == 0
    res = ValidationResult(is_ok, all_errors, all_warnings)
    res.hw_report_lines = hw_report_lines
    return res


if __name__ == "__main__":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="SimaticML LAD XML 静态语法、安全逻辑与硬件组态对账校验器"
    )
    parser.add_argument(
        "xml_dir",
        nargs="?",
        default="tools/test_out",
        help="待校验的 XML 目录路径（如 03_梯形图XML）",
    )
    parser.add_argument(
        "--eplan-io",
        dest="eplan_io",
        default=None,
        help="EPLAN 识图清单或 I/O 分配表 Markdown 路径（用于 I/O 对账）",
    )
    parser.add_argument(
        "--tia-status-json",
        dest="tia_status_json",
        default=None,
        help="博途环境快照 JSON 路径（用于硬件组态地址越界比对）",
    )
    args = parser.parse_args()

    target_dir = resolve_project_path(args.xml_dir) or args.xml_dir
    print(f"=== SimaticML LAD XML 静态与安全校验器 (目标: {target_dir}) ===")
    res = validate_project_xmls(
        target_dir,
        eplan_io_path=args.eplan_io,
        tia_status_json_path=args.tia_status_json,
    )
    ok = res.ok
    errs = res.errors
    warns = res.warnings

    for line in getattr(res, "hw_report_lines", []):
        print(line)

    if warns:
        print(f"\n--- 工业级深度审计告警清单 (共 {len(warns)} 项) ---")
        for w in warns:
            print("  ⚠ [WARN]", w)

    print("\n--- 门禁校验结论 ---")
    print("验证结果:", "全部通过 (PASS)" if ok else f"存在 {len(errs)} 处错误 (FAIL)")
    for e in errs:
        print("  🔴 [ERROR]", e)

    sys.exit(0 if ok else 1)
