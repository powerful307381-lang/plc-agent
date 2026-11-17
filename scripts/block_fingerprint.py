# -*- coding: utf-8 -*-
"""
模块名称: block_fingerprint
功能说明: 对 SimaticML XML（程序块 FC/FB/OB、数据块 DB/InstanceDB、UDT、变量表 TagTable）
         提取去 UId/ID 化的语义指纹，并对比两个 XML 之间的逻辑、接口与变量差异。
用途: 供 do_import_pipeline.ps1 在关卡③（-DiffOnly）阶段执行精确改动检测。
"""

import hashlib
import io
import json
import sys
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Tuple


def _tag_local(elem: ET.Element) -> str:
    return elem.tag.split("}")[-1]


def _parse_xml(path: str) -> ET.Element:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        return ET.fromstring(f.read())


def _format_symbol(container: ET.Element) -> str:
    """将 Symbol 或 Instance 下的 Component 序列（含数组下标）格式化为可读路径"""
    segs: List[str] = []
    for comp in container:
        if _tag_local(comp) != "Component":
            continue
        cname = comp.attrib.get("Name", "")
        if not cname:
            continue
        idx_parts: List[str] = []
        for sub in comp:
            if _tag_local(sub) == "Access":
                cv = next((c.text or "" for c in sub.iter() if _tag_local(c) == "ConstantValue"), "")
                sub_comps = [sc.attrib.get("Name", "") for sc in sub.iter() if _tag_local(sc) == "Component"]
                idx_parts.append(".".join(sub_comps) if sub_comps else cv)
        if idx_parts:
            segs.append(f"{cname}[{','.join(idx_parts)}]")
        else:
            segs.append(cname)
    return ".".join(segs)


def _format_access(access: ET.Element) -> str:
    scope = access.attrib.get("Scope", "")
    sym = next((c for c in access if _tag_local(c) == "Symbol"), None)
    if sym is not None:
        path = _format_symbol(sym)
        return f"#{path}" if scope == "LocalVariable" else path
    const_type = next((c.text or "" for c in access.iter() if _tag_local(c) == "ConstantType"), "")
    const_val = next((c.text or "" for c in access.iter() if _tag_local(c) == "ConstantValue"), "")
    if const_type:
        return f"#{const_type}:{const_val}"
    return f"#{const_val}"


def _extract_title(cu: ET.Element) -> str:
    for ml in cu.iter():
        if _tag_local(ml) == "MultilingualText" and ml.attrib.get("CompositionName") == "Title":
            for txt in ml.iter():
                if _tag_local(txt) == "Text":
                    return (txt.text or "").strip()
    return ""


def _extract_comment_text(elem: ET.Element) -> str:
    for ml in elem.iter():
        if _tag_local(ml) == "MultilingualText" and ml.attrib.get("CompositionName") == "Comment":
            for item in ml.iter():
                if _tag_local(item) == "MultilingualTextItem":
                    culture = next((c.text or "" for c in item.iter() if _tag_local(c) == "Culture"), "")
                    txt = next((c.text or "" for c in item.iter() if _tag_local(c) == "Text"), "")
                    if culture == "zh-CN":
                        return txt.strip()
            for txt in ml.iter():
                if _tag_local(txt) == "Text" and txt.text:
                    return txt.text.strip()
    return ""


def network_signatures(path: str) -> List[Dict[str, Any]]:
    """提取块内每个 CompileUnit 的语义指纹（忽略 UId、ID、Wire 声明顺序）"""
    root = _parse_xml(path)
    sigs: List[Dict[str, Any]] = []
    compile_units = [e for e in root.iter() if _tag_local(e) == "CompileUnit" or _tag_local(e) == "SW.Blocks.CompileUnit"]

    for idx, cu in enumerate(compile_units, start=1):
        title = _extract_title(cu)
        flg = next((e for e in cu.iter() if _tag_local(e) == "FlgNet"), None)
        if flg is None:
            sigs.append({"nw": idx, "title": title, "hash": "", "items": []})
            continue

        parts_elem = next((c for c in flg if _tag_local(c) == "Parts"), None)
        wires_elem = next((c for c in flg if _tag_local(c) == "Wires"), None)

        acc_map: Dict[str, str] = {}
        parts_meta: Dict[str, Dict[str, Any]] = {}

        if parts_elem is not None:
            for child in parts_elem:
                ctag = _tag_local(child)
                uid = child.attrib.get("UId", "")
                if not uid:
                    continue
                if ctag == "Access":
                    acc_map[uid] = _format_access(child)
                elif ctag in ("Part", "Call"):
                    if ctag == "Call":
                        ci = next((c for c in child if _tag_local(c) == "CallInfo"), None)
                        ci_name = ci.attrib.get("Name", "").strip('"') if ci is not None else "Call"
                        ci_type = ci.attrib.get("BlockType", "") if ci is not None else ""
                        base_name = f"Call:{ci_type}:{ci_name}" if ci_type else f"Call:{ci_name}"
                        inst_el = next((c for c in (ci if ci is not None else []) if _tag_local(c) == "Instance"), None)
                    else:
                        base_name = child.attrib.get("Name", "")
                        inst_el = next((c for c in child if _tag_local(c) == "Instance"), None)

                    neg_pins = sorted(
                        c.attrib.get("Name", "operand")
                        for c in child
                        if _tag_local(c) == "Negated"
                    )
                    if neg_pins:
                        base_name = f"{base_name}/NC({','.join(neg_pins)})"

                    meta_kv: List[str] = []
                    if inst_el is not None:
                        inst_scope = inst_el.attrib.get("Scope", "")
                        inst_sym = _format_symbol(inst_el)
                        if inst_sym:
                            prefix = "#" if inst_scope == "LocalVariable" else ""
                            meta_kv.append(f"inst={prefix}{inst_sym}")
                    for tv in child:
                        if _tag_local(tv) == "TemplateValue":
                            tv_name = tv.attrib.get("Name", "")
                            tv_val = (tv.text or "").strip()
                            if tv_name:
                                meta_kv.append(f"{tv_name}={tv_val}")

                    parts_meta[uid] = {
                        "name": base_name,
                        "meta": sorted(meta_kv),
                        "ops": [],
                    }

        wires_list = list(wires_elem) if wires_elem is not None else []

        # 第一遍：收集所有 IdentCon 与元件引脚的绑定关系，确保 Wire 顺序无关
        parsed_wires: List[List[Any]] = []
        for wire in wires_list:
            ends: List[Any] = []
            for sub in wire:
                stag = _tag_local(sub)
                if stag == "Powerrail":
                    ends.append(("RAIL", "RAIL"))
                elif stag == "OpenCon":
                    ends.append(("OPEN", "OPEN"))
                elif stag == "IdentCon":
                    uid = sub.attrib.get("UId", "")
                    ends.append(("VAR", acc_map.get(uid, "?")))
                elif stag == "NameCon":
                    uid = sub.attrib.get("UId", "")
                    pin = sub.attrib.get("Name", "")
                    ends.append(("PIN", (uid, pin)))
            if ends:
                parsed_wires.append(ends)
                var_ops = [val for kind, val in ends if kind == "VAR"]
                if var_ops:
                    for kind, val in ends:
                        if kind == "PIN":
                            p_uid, pin = val
                            if p_uid in parts_meta:
                                for op in var_ops:
                                    parts_meta[p_uid]["ops"].append(f"{pin}={op}")

        def _part_label(p_uid: str) -> str:
            info = parts_meta.get(p_uid)
            if not info:
                return f"UnknownPart"
            attrs = sorted(info["meta"] + info["ops"])
            return f"{info['name']}[{','.join(attrs)}]"

        def _end_label(end: Tuple[str, Any]) -> str:
            kind, val = end
            if kind in ("RAIL", "OPEN", "VAR"):
                return str(val)
            p_uid, pin = val
            return f"{_part_label(p_uid)}.{pin}"

        # 第二遍：生成去 UId 化的规范有向连线签名
        edge_items: List[str] = []
        for ends in parsed_wires:
            src_lab = _end_label(ends[0])
            dst_labs = sorted(_end_label(e) for e in ends[1:])
            edge_items.append(f"{src_lab} -> {', '.join(dst_labs)}")

        part_items = sorted(_part_label(uid) for uid in parts_meta)
        items = part_items + sorted(edge_items)
        digest = hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()[:12]
        sigs.append({"nw": idx, "title": title, "hash": digest, "items": items})

    return sigs


def tag_signatures(path: str) -> Dict[str, Tuple[str, str, str]]:
    """提取变量表中每条变量的 (逻辑地址, 数据类型, 注释)"""
    root = _parse_xml(path)
    out: Dict[str, Tuple[str, str, str]] = {}
    for tag_el in root.iter():
        if _tag_local(tag_el) != "SW.Tags.PlcTag":
            continue
        al = next((c for c in tag_el if _tag_local(c) == "AttributeList"), None)
        if al is None:
            continue
        get_attr = lambda n: next(((c.text or "").strip() for c in al if _tag_local(c) == n), "")
        name = get_attr("Name")
        if not name:
            continue
        addr = get_attr("LogicalAddress").upper()
        dtype = get_attr("DataTypeName")
        cmt = _extract_comment_text(tag_el)
        out[name] = (addr, dtype, cmt)
    return out


def interface_signatures(path: str) -> List[str]:
    """提取 DB / FB / FC / UDT / InstanceDB 的接口成员签名（忽略博途自动展开与系统提示字段）"""
    root = _parse_xml(path)
    members: List[str] = []

    def _collect_members(sec_el: ET.Element, sec_name: str, prefix: str = "") -> None:
        for mem in sec_el:
            if _tag_local(mem) != "Member":
                continue
            if mem.attrib.get("Informative", "").strip().lower() == "true":
                continue
            m_name = mem.attrib.get("Name", "")
            m_type = mem.attrib.get("Datatype", "")
            full_name = f"{prefix}.{m_name}" if prefix else m_name
            start_el = next((c for c in mem if _tag_local(c) == "StartValue"), None)
            start_val = (start_el.text or "").strip() if start_el is not None else ""
            sig = f"{sec_name}:{full_name}:{m_type}"
            if start_val:
                sig += f"={start_val}"
            members.append(sig)
            if m_type == "Struct":
                for sub_secs in mem:
                    if _tag_local(sub_secs) != "Sections":
                        continue
                    for sub_sec in sub_secs:
                        if _tag_local(sub_sec) == "Section":
                            _collect_members(sub_sec, sec_name, full_name)

    for el in root.iter():
        if _tag_local(el) == "InstanceOfName" and el.text:
            members.append(f"InstanceOf={el.text.strip().strip('\"')}")
        elif _tag_local(el) == "Interface":
            for secs in el:
                if _tag_local(secs) != "Sections":
                    continue
                for sec in secs:
                    if _tag_local(sec) == "Section":
                        _collect_members(sec, sec.attrib.get("Name", ""))
    return sorted(members)


def watch_force_signatures(path: str) -> List[Tuple[str, str, str, str, str]]:
    """提取监控表或强制表条目的语义签名列表"""
    root = _parse_xml(path)
    entries: List[Tuple[str, str, str, str, str]] = []
    for el in root.iter():
        t = _tag_local(el)
        if t not in ("SW.WatchAndForceTables.PlcWatchTableEntry", "SW.WatchAndForceTables.PlcForceTableEntry"):
            continue
        al = next((c for c in el if _tag_local(c) == "AttributeList"), None)
        if al is None:
            continue
        get_attr = lambda n: next(((c.text or "").strip() for c in al if _tag_local(c) == n), "")
        name = get_attr("Name")
        addr = get_attr("Address").upper()
        fmt = get_attr("DisplayFormat")
        val = get_attr("ModifyValue") or get_attr("ForceValue")
        cmt = _extract_comment_text(el)
        entries.append((name, addr, fmt, val, cmt))
    return sorted(entries)


def _xml_kind(root: ET.Element) -> str:
    for el in root.iter():
        t = _tag_local(el)
        if t in (
            "SW.Tags.PlcTagTable",
            "SW.Types.PlcStruct",
            "SW.Blocks.GlobalDB",
            "SW.Blocks.InstanceDB",
            "SW.Blocks.FB",
            "SW.Blocks.FC",
            "SW.Blocks.OB",
            "SW.WatchAndForceTables.PlcWatchTable",
            "SW.WatchAndForceTables.PlcForceTable",
        ):
            return t
    return "Unknown"


def compare(old_path: str, new_path: str) -> List[str]:
    """对比两个 SimaticML XML 的语义差异，返回中文差异描述列表（无差异返回空列表）"""
    old_root = _parse_xml(old_path)
    new_root = _parse_xml(new_path)
    old_kind = _xml_kind(old_root)
    new_kind = _xml_kind(new_root)

    diffs: List[str] = []
    if old_kind != new_kind and "Unknown" not in (old_kind, new_kind):
        diffs.append(f"对象类型不同（博途内: {old_kind}, 待导入: {new_kind}）")
        return diffs

    if new_kind in ("SW.WatchAndForceTables.PlcWatchTable", "SW.WatchAndForceTables.PlcForceTable"):
        o_entries = watch_force_signatures(old_path)
        n_entries = watch_force_signatures(new_path)
        if o_entries != n_entries:
            removed_e = sorted(set(o_entries) - set(n_entries))[:5]
            added_e = sorted(set(n_entries) - set(o_entries))[:5]
            diffs.append(f"监控/强制表条目变更；博途内独有: {removed_e}；待导入独有: {added_e}")
        return diffs

    if new_kind == "SW.Tags.PlcTagTable" or old_kind == "SW.Tags.PlcTagTable":
        o_tags = tag_signatures(old_path)
        n_tags = tag_signatures(new_path)
        for name in sorted(set(o_tags) | set(n_tags)):
            if o_tags.get(name) != n_tags.get(name):
                diffs.append(f"变量 '{name}' 变更: 博途内 {o_tags.get(name)} -> 待导入 {n_tags.get(name)}")
        return diffs

    o_iface = interface_signatures(old_path)
    n_iface = interface_signatures(new_path)
    if o_iface != n_iface:
        removed_m = sorted(set(o_iface) - set(n_iface))[:5]
        added_m = sorted(set(n_iface) - set(o_iface))[:5]
        diffs.append(f"接口/成员定义不同；博途内独有: {removed_m}；待导入独有: {added_m}")

    if new_kind in ("SW.Types.PlcStruct", "SW.Blocks.GlobalDB", "SW.Blocks.InstanceDB"):
        return diffs

    o_nws = network_signatures(old_path)
    n_nws = network_signatures(new_path)
    if len(o_nws) != len(n_nws):
        diffs.append(f"程序段数量不同（博途内 {len(o_nws)} 段，待导入 {len(n_nws)} 段）")

    for a, b in zip(o_nws, n_nws):
        if a["hash"] != b["hash"]:
            removed = sorted(set(a["items"]) - set(b["items"]))[:5]
            added = sorted(set(b["items"]) - set(a["items"]))[:5]
            title = b["title"] or a["title"] or f"NW{a['nw']}"
            diffs.append(
                f"程序段 {a['nw']}《{title}》逻辑不同；博途内独有: {removed}；待导入独有: {added}"
            )
    return diffs


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if len(sys.argv) < 3:
        print(json.dumps(["用法错误: 需要传入 <old_xml> <new_xml>"], ensure_ascii=False))
        sys.exit(2)
    result = compare(sys.argv[1], sys.argv[2])
    print(json.dumps(result, ensure_ascii=False))
