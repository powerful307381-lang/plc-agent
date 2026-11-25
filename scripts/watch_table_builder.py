# -*- coding: utf-8 -*-
"""
模块名称: watch_table_builder
功能说明: 基于本机 TIA Portal V16 实测模板（WatchTable_Template.xml / ForceTable_Template.xml），
         确定性构建符合 SimaticML V16 规范的 PlcWatchTable 与 PlcForceTable XML 文件，
         并支持按工位与控制功能自动切分变量表生成整套调试监控与冷态强制测试表。
符合规范: templates/WatchTable_Template.xml, templates/ForceTable_Template.xml
"""

import os
from typing import Any, Dict, List, Optional

try:
    from common_utils import escape_xml as _escape_xml, write_utf8_bom_crlf
except ImportError:
    from scripts.common_utils import escape_xml as _escape_xml, write_utf8_bom_crlf  # type: ignore


def _write_utf8_bom_crlf(out_path: str, lines: List[str]) -> str:
    return write_utf8_bom_crlf(out_path, "\r\n".join(lines) + "\r\n")


def _infer_display_format(data_type: str, explicit_fmt: Optional[str] = None) -> str:
    if explicit_fmt:
        return explicit_fmt
    dt = (data_type or "Bool").strip().lower()
    mapping = {
        "bool": "Bool",
        "byte": "Hex",
        "word": "Hex",
        "dword": "Hex",
        "int": "DEC_signed",
        "dint": "DEC_signed",
        "uint": "DEC_unsigned",
        "udint": "DEC_unsigned",
        "real": "Float",
        "lreal": "Float",
        "time": "Time",
        "string": "String",
    }
    return mapping.get(dt, "Bool")


def _default_modify_value(data_type: str, fmt: str) -> str:
    if fmt == "Bool" or (data_type or "").lower() == "bool":
        return "false"
    if fmt == "Time":
        return "T#0ms"
    if fmt == "Float":
        return "0.0"
    return "0"


def build_watch_table_xml(
    table_name: str,
    entries: List[Dict[str, Any]],
    out_xml_path: str,
) -> str:
    """
    基于 templates/WatchTable_Template.xml 的 V16 Schema 生成 PlcWatchTable XML。
    entries 每项支持字段: name, address, data_type, display_format, modify_value, comment
    """
    xml_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<Document>",
        '  <Engineering version="V16" />',
        '  <SW.WatchAndForceTables.PlcWatchTable ID="0">',
        "    <AttributeList>",
        f"      <Name>{_escape_xml(table_name)}</Name>",
        "    </AttributeList>",
        "    <ObjectList>",
    ]

    id_seq = 1
    for entry in entries:
        name = str(entry.get("name", "")).strip()
        addr = str(entry.get("address") or entry.get("logical_address") or "").strip().upper()
        dtype = str(entry.get("data_type", "Bool")).strip()
        fmt = _infer_display_format(dtype, entry.get("display_format"))
        mod_val = str(entry.get("modify_value") or _default_modify_value(dtype, fmt))
        comment = str(entry.get("comment", "")).strip() or f"{name} 监控"

        e_id = hex(id_seq)[2:]
        ml_id = hex(id_seq + 1)[2:]
        mli_id = hex(id_seq + 2)[2:]
        id_seq += 3

        attr_lines = []
        if addr:
            attr_lines.append(f"          <Address>{_escape_xml(addr)}</Address>")
        attr_lines.extend([
            f"          <DisplayFormat>{_escape_xml(fmt)}</DisplayFormat>",
            f"          <ModifyValue>{_escape_xml(mod_val)}</ModifyValue>",
            f"          <Name>{_escape_xml(name)}</Name>",
        ])
        xml_lines.extend([
            f'      <SW.WatchAndForceTables.PlcWatchTableEntry ID="{e_id}" CompositionName="Entries">',
            "        <AttributeList>",
            *attr_lines,
            "        </AttributeList>",
            "        <ObjectList>",
            f'          <MultilingualText ID="{ml_id}" CompositionName="Comment">',
            "            <ObjectList>",
            f'              <MultilingualTextItem ID="{mli_id}" CompositionName="Items">',
            "                <AttributeList>",
            "                  <Culture>zh-CN</Culture>",
            f"                  <Text>{_escape_xml(comment)}</Text>",
            "                </AttributeList>",
            "              </MultilingualTextItem>",
            "            </ObjectList>",
            "          </MultilingualText>",
            "        </ObjectList>",
            "      </SW.WatchAndForceTables.PlcWatchTableEntry>",
        ])

    xml_lines.extend([
        "    </ObjectList>",
        "  </SW.WatchAndForceTables.PlcWatchTable>",
        "</Document>",
    ])
    return _write_utf8_bom_crlf(out_xml_path, xml_lines)


def build_force_table_xml(
    table_name: str,
    entries: List[Dict[str, Any]],
    out_xml_path: str,
) -> str:
    """
    基于 templates/ForceTable_Template.xml 的 V16 Schema 生成 PlcForceTable XML。
    自动将 %I/%Q 地址补充外设寻址后缀 ':P'（若尚未携带），默认 ForceValue='false'/'0' 保证冷态安全。
    """
    xml_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<Document>",
        '  <Engineering version="V16" />',
        '  <SW.WatchAndForceTables.PlcForceTable ID="0">',
        "    <AttributeList>",
        f"      <Name>{_escape_xml(table_name)}</Name>",
        "    </AttributeList>",
        "    <ObjectList>",
    ]

    id_seq = 1
    for entry in entries:
        name = str(entry.get("name", "")).strip()
        raw_addr = str(entry.get("address") or entry.get("logical_address") or "").strip().upper()
        addr = raw_addr if (not raw_addr or raw_addr.endswith(":P")) else f"{raw_addr}:P"
        dtype = str(entry.get("data_type", "Bool")).strip()
        fmt = _infer_display_format(dtype, entry.get("display_format"))
        force_val = str(entry.get("force_value") or _default_modify_value(dtype, fmt))
        raw_cmt = str(entry.get("comment", "")).strip()
        comment = raw_cmt if "人工确认" in raw_cmt else f"{raw_cmt} 【冷态强制测试：须断开主动力电源并经人工确认】".strip()

        e_id = hex(id_seq)[2:]
        ml_id = hex(id_seq + 1)[2:]
        mli_id = hex(id_seq + 2)[2:]
        id_seq += 3

        xml_lines.extend([
            f'      <SW.WatchAndForceTables.PlcForceTableEntry ID="{e_id}" CompositionName="Entries">',
            "        <AttributeList>",
            f"          <Address>{_escape_xml(addr)}</Address>",
            f"          <DisplayFormat>{_escape_xml(fmt)}</DisplayFormat>",
            f"          <ForceValue>{_escape_xml(force_val)}</ForceValue>",
            f"          <Name>{_escape_xml(name)}</Name>",
            "        </AttributeList>",
            "        <ObjectList>",
            f'          <MultilingualText ID="{ml_id}" CompositionName="Comment">',
            "            <ObjectList>",
            f'              <MultilingualTextItem ID="{mli_id}" CompositionName="Items">',
            "                <AttributeList>",
            "                  <Culture>zh-CN</Culture>",
            f"                  <Text>{_escape_xml(comment)}</Text>",
            "                </AttributeList>",
            "              </MultilingualTextItem>",
            "            </ObjectList>",
            "          </MultilingualText>",
            "        </ObjectList>",
            "      </SW.WatchAndForceTables.PlcForceTableEntry>",
        ])

    xml_lines.extend([
        "    </ObjectList>",
        "  </SW.WatchAndForceTables.PlcForceTable>",
        "</Document>",
    ])
    return _write_utf8_bom_crlf(out_xml_path, xml_lines)


def build_station_watch_tables_from_tags(
    tag_list: List[Dict[str, Any]],
    out_dir: str,
    station_prefix_map: Optional[Dict[str, str]] = None,
    timer_members: Optional[List[str]] = None,
) -> Dict[str, List[str]]:
    """
    根据点表与可选工位映射自动生成整套监控表与冷态强制测试表：
      - WatchTable_01_安全与模式监控.xml
      - WatchTable_02_<工位或全线>IO对点监控.xml
      - WatchTable_03_自动步序与定时器监控.xml
      - ForceTable_01_冷态安全强制测试.xml
    """
    os.makedirs(out_dir, exist_ok=True)
    watch_files: List[str] = []
    force_files: List[str] = []

    safety_mode_entries: List[Dict[str, Any]] = []
    step_alarm_entries: List[Dict[str, Any]] = []
    force_q_entries: List[Dict[str, Any]] = []
    station_io_buckets: Dict[str, List[Dict[str, Any]]] = {}

    safety_kws = ("急停", "安全门", "光栅", "热继", "模式", "安全条件", "原位", "原点", "手自动", "复位")
    step_kws = ("步序", "STEP", "请求", "报警", "ALM", "ERR", "仿真")

    for tag in tag_list:
        name = str(tag.get("name", "")).strip()
        addr = str(tag.get("logical_address") or tag.get("address") or "").strip().upper()
        dtype = str(tag.get("data_type", "Bool")).strip()
        comment = str(tag.get("comment", "")).strip()
        if not name or "备用" in name or "备用" in comment:
            continue

        entry = {
            "name": name,
            "address": addr,
            "data_type": dtype,
            "comment": comment,
        }

        if any(k in name for k in safety_kws):
            safety_mode_entries.append(entry)

        if addr.startswith("%M") and any(k in name.upper() for k in step_kws):
            step_alarm_entries.append(entry)

        if addr.startswith(("%I", "%Q")):
            station_key = "全线"
            if station_prefix_map:
                for st_name, prefix_kw in station_prefix_map.items():
                    if prefix_kw in name or prefix_kw in comment:
                        station_key = st_name
                        break
            station_io_buckets.setdefault(station_key, []).append(entry)

        if addr.startswith("%Q") and not addr.startswith("%QW"):
            force_q_entries.append(entry)

    for tm in (timer_members or []):
        step_alarm_entries.append({
            "name": f"全局定时器.{tm}.Q",
            "address": "",
            "data_type": "Bool",
            "comment": f"定时器 {tm} 到达状态",
        })
        step_alarm_entries.append({
            "name": f"全局定时器.{tm}.ET",
            "address": "",
            "data_type": "Time",
            "comment": f"定时器 {tm} 当前经过时间",
        })

    if safety_mode_entries:
        p = build_watch_table_xml(
            "WatchTable_01_安全与模式监控",
            safety_mode_entries,
            os.path.join(out_dir, "WatchTable_01_安全与模式监控.xml"),
        )
        watch_files.append(p)

    for idx, (st_name, st_entries) in enumerate(sorted(station_io_buckets.items()), start=1):
        safe_st = st_name.replace(" ", "_")
        t_name = f"WatchTable_02_{safe_st}IO对点监控" if idx == 1 and safe_st == "全线" else f"WatchTable_02_{safe_st}_IO对点监控"
        p = build_watch_table_xml(t_name, st_entries, os.path.join(out_dir, f"{t_name}.xml"))
        watch_files.append(p)

    if step_alarm_entries:
        p = build_watch_table_xml(
            "WatchTable_03_自动步序与定时器监控",
            step_alarm_entries,
            os.path.join(out_dir, "WatchTable_03_自动步序与定时器监控.xml"),
        )
        watch_files.append(p)

    if force_q_entries:
        p = build_force_table_xml(
            "ForceTable_01_冷态安全强制测试",
            force_q_entries,
            os.path.join(out_dir, "ForceTable_01_冷态安全强制测试.xml"),
        )
        force_files.append(p)

    return {"watch_tables": watch_files, "force_tables": force_files}
