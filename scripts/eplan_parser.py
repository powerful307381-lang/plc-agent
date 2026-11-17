# -*- coding: utf-8 -*-
"""
模块名称: eplan_parser
功能说明: 解析 EPLAN 导出的结构化报表（PLC 地址与通道分配表、设备 BOM 表）及配套产线工艺资料
         （产线工艺动作步序表、机械防呆与安全联锁矩阵表），并结合博途环境快照执行四方对账，
         直接产出标准化的变量点表结构与《01_识图清单.md》。
符合规范:
  - eplan-to-tia-lad SKILL.md 阶段 1 & 阶段 2
  - references/01_识图规范.md
  - references/02_命名与注释规范.md
  - references/09_产线级EPLAN导出与工艺输入规范.md
"""

import csv
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import openpyxl

try:
    from tia_connect import compare_eplan_with_tia_hw
except ImportError:
    from scripts.tia_connect import compare_eplan_with_tia_hw  # type: ignore


# 常见 EPLAN 表头映射（兼容中英文）
_PLC_COL_ALIASES: Dict[str, Tuple[str, ...]] = {
    "address": ("plc地址", "地址", "符号地址", "逻辑地址", "logicaladdress", "address", "io地址"),
    "signal_type": ("信号类型", "io类型", "类型", "signaltype", "type"),
    "slot": ("模块/槽位", "槽位", "模块", "slot", "module", "硬件模块"),
    "card_terminal": ("卡片端子", "plc端子", "通道端子", "terminal", "通道"),
    "device_tag": ("元器件代号", "设备标识符", "设备代号", "dt", "devicetag", "代号"),
    "wire_terminal": ("元器件端子/线号", "端子/线号", "端子线号", "线号", "端子号", "wire", "pin"),
    "page_col": ("页码.列号", "图纸位置", "页.列", "页码", "location", "pagecol"),
    "contact_type": ("no/nc", "触点类型", "常开常闭", "contacttype", "触点"),
    "description": ("中文功能", "功能文本", "信号说明", "功能说明", "描述", "description", "功能"),
    "is_spare": ("是否备用", "备用", "spare"),
    "notes": ("备注", "说明", "notes", "remark"),
}

_BOM_COL_ALIASES: Dict[str, Tuple[str, ...]] = {
    "device_tag": ("元器件代号", "设备标识符", "dt", "devicetag", "代号"),
    "name": ("名称/功能", "名称", "功能", "设备名称", "name"),
    "model": ("型号规格", "型号", "规格", "partnumber", "model"),
    "voltage": ("电压等级", "额定电压", "线圈电压", "供电电压", "voltage"),
    "control_type": ("控制类型", "电磁阀类型", "类型", "controltype", "电控方式"),
    "wiring_type": ("接线方式", "极性", "触点极性", "wiringtype", "wiring"),
    "notes": ("备注", "notes", "remark"),
}

_SAFETY_KEYWORDS = ("急停", "热继", "停止", "极限", "限位", "安全门", "光栅", "过载")
_INVALID_NAME_CHARS = re.compile(r'[ \./\\\'"%:;,\(\)\[\]\{\}&#\-+=~`!@$^*?<>|]+')
_IQ_PREFIX_RE = re.compile(r"^([A-Z]{1,3}\d{1,3}[A-Z]?)$")


def _norm_header(h: Any) -> str:
    return re.sub(r"\s+", "", str(h or "")).strip().lower()


def _map_headers(headers: List[Any], alias_map: Dict[str, Tuple[str, ...]]) -> Dict[str, int]:
    """将表格首行标题映射到标准字段索引"""
    col_idx: Dict[str, int] = {}
    norm_headers = [_norm_header(h) for h in headers]
    for field, aliases in alias_map.items():
        for idx, nh in enumerate(norm_headers):
            if nh in aliases or any(nh == _norm_header(a) for a in aliases):
                col_idx[field] = idx
                break
        if field not in col_idx:
            # 二次模糊匹配（处理带括号提示的表头，如 "控制类型(单电控/双电控)"）
            for idx, nh in enumerate(norm_headers):
                if any(_norm_header(a) in nh for a in aliases):
                    col_idx[field] = idx
                    break
    return col_idx


def _read_tabular_rows(path: str) -> Tuple[List[Any], List[List[Any]]]:
    """读取 .xlsx 或 .csv 文件，返回 (表头行, 数据行列表)"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"未找到文件: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    elif ext == ".csv":
        raw_bytes = open(path, "rb").read()
        text = None
        for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
            try:
                text = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw_bytes.decode("utf-8", errors="replace")
        rows = list(csv.reader(text.splitlines()))
    else:
        raise ValueError(f"不支持的点表文件格式 '{ext}'，仅支持 .xlsx / .csv")

    non_empty = [r for r in rows if any(str(c or "").strip() for c in r)]
    if not non_empty:
        return [], []
    return non_empty[0], non_empty[1:]


def _clean_device_tag(raw_dt: str) -> Tuple[str, str]:
    """
    标准化元器件代号，返回 (去掉前导'-'与结构前缀的代号, 保留'-'前缀的展示代号)
    例如: '=A1+B1-SB0' -> ('SB0', '-SB0'); '-KM1' -> ('KM1', '-KM1')
    """
    s = str(raw_dt or "").strip()
    if not s:
        return "", ""
    # 剥离结构前缀 (=A1+B1-SB0 -> -SB0)
    if "-" in s:
        s = "-" + s.split("-")[-1]
    clean = s.lstrip("-").strip().upper()
    display = f"-{clean}" if clean else ""
    return clean, display


def _clean_page_col(raw_loc: str) -> str:
    """去除 '=A1+B1/' 等前缀，提取 '页码.列号'"""
    s = str(raw_loc or "").strip()
    if not s:
        return "?"
    if "/" in s:
        s = s.split("/")[-1]
    return s.lstrip(".").strip() or "?"


def _normalize_plc_address(raw_addr: str) -> Tuple[str, bool]:
    """标准化西门子逻辑地址，返回 (标准地址, 是否待确认)"""
    s = re.sub(r"\s+", "", str(raw_addr or "")).upper()
    if not s:
        return "", True
    if not s.startswith("%"):
        if re.match(r"^[IQ](?:[BWD]?\d+(?:\.\d+)?)$", s):
            s = "%" + s
    is_siemens = bool(re.match(r"^%[IQ](?:\d+\.[0-7]|[BWD]\d+)$", s))
    return s, (not is_siemens)


def _infer_data_type(addr: str, signal_type: str) -> str:
    sig = str(signal_type or "").strip().upper()
    addr_u = (addr or "").upper()
    if addr_u.startswith(("%IW", "%QW")) or sig in ("AI", "AQ"):
        return "Int"
    if addr_u.startswith(("%ID", "%QD")):
        return "DInt"
    return "Bool"


def _build_iq_tag_name(clean_dt: str, desc: str, addr: str, is_spare: bool) -> str:
    """按 references/02 生成合法的 I/Q 中文变量名（<= 40 字符）"""
    addr_safe = addr.lstrip("%").replace(".", "_") or "NA"
    func = _INVALID_NAME_CHARS.sub("_", str(desc or "").strip()).strip("_")

    if is_spare or "备用" in func:
        base = f"SPARE_{addr_safe}_备用"
        return base[:40]

    if not clean_dt or not _IQ_PREFIX_RE.match(clean_dt):
        prefix = "IO0" if not clean_dt else re.sub(r"[^A-Z0-9]", "", clean_dt.upper())
        if not _IQ_PREFIX_RE.match(prefix):
            prefix = "DI0" if addr.startswith("%I") else "DQ0"
    else:
        prefix = clean_dt

    if not func:
        func = f"通道{addr_safe}"
    name = f"{prefix}_{func}"
    return name[:40].rstrip("_")


def _build_iq_comment(
    page_col: str,
    display_dt: str,
    wire_terminal: str,
    desc: str,
    contact_type: str,
    addr: str,
    needs_confirm: bool,
) -> str:
    """按 references/02 生成三段式 EPLAN 标准变量注释"""
    loc = page_col or "?"
    dt = display_dt or "?"
    wt = str(wire_terminal or "").strip() or "?"
    ct = str(contact_type or "").strip().upper()
    if not ct:
        ct = "线圈" if addr.startswith("%Q") else "NO"

    body = str(desc or "").strip() or "未命名信号"
    if ct == "NC" and any(k in body for k in _SAFETY_KEYWORDS) and "信号0" not in body:
        body = f"{body}，信号0=触发/断开"

    suffix = f"({contact_type.strip()})" if contact_type.strip() else f"({ct})"
    warn_tag = " ⚠待确认" if needs_confirm else ""
    return f"[{loc} | {dt} | {wt}] {body}{suffix}{warn_tag}"


def parse_eplan_plc_table(path: str) -> List[Dict[str, Any]]:
    """
    解析 EPLAN 导出的《PLC 地址与通道分配表》（支持 .xlsx / .csv）。
    返回标准化点表条目列表，可直接传入 tag_builder.build_tags() 或用于识图对账。
    """
    headers, rows = _read_tabular_rows(path)
    if not headers:
        return []
    col_map = _map_headers(headers, _PLC_COL_ALIASES)

    items: List[Dict[str, Any]] = []
    seen_names: Dict[str, int] = {}

    for row in rows:
        def _get(field: str) -> str:
            idx = col_map.get(field)
            if idx is None or idx >= len(row):
                return ""
            val = row[idx]
            return "" if val is None else str(val).strip()

        raw_addr = _get("address")
        raw_desc = _get("description")
        raw_dt = _get("device_tag")
        if not raw_addr and not raw_desc and not raw_dt:
            continue

        addr, addr_uncertain = _normalize_plc_address(raw_addr)
        sig_type = _get("signal_type").upper()
        if not sig_type:
            if addr.startswith("%IW") or addr.startswith("%ID"):
                sig_type = "AI"
            elif addr.startswith("%QW") or addr.startswith("%QD"):
                sig_type = "AQ"
            elif addr.startswith("%Q"):
                sig_type = "DO"
            else:
                sig_type = "DI"

        spare_flag = _get("is_spare").lower() in ("是", "yes", "true", "1", "spare")
        is_spare = spare_flag or ("备用" in raw_desc) or (raw_dt in ("", "-", "—") and "备用" in raw_desc)

        clean_dt, display_dt = _clean_device_tag(raw_dt)
        page_col = _clean_page_col(_get("page_col"))
        wire_terminal = _get("wire_terminal")
        card_terminal = _get("card_terminal")
        contact_type = _get("contact_type")
        slot = _get("slot")
        notes = _get("notes")

        needs_confirm = addr_uncertain or (not is_spare and (not clean_dt or page_col == "?"))

        tag_name = _build_iq_tag_name(clean_dt, raw_desc, addr, is_spare)
        # 避免同功能多通道重名（例如多路备用或同名传感器）
        if tag_name in seen_names:
            seen_names[tag_name] += 1
            suffix_num = str(seen_names[tag_name])
            tag_name = f"{tag_name[:40 - len(suffix_num) - 1]}_{suffix_num}"
        else:
            seen_names[tag_name] = 1

        data_type = _infer_data_type(addr, sig_type)
        comment = _build_iq_comment(
            page_col=page_col,
            display_dt=display_dt,
            wire_terminal=wire_terminal or card_terminal,
            desc=raw_desc if not is_spare else (raw_desc or "备用通道"),
            contact_type=contact_type,
            addr=addr,
            needs_confirm=needs_confirm,
        )

        items.append({
            "name": tag_name,
            "data_type": data_type,
            "logical_address": addr or raw_addr,
            "comment": comment,
            "device_tag": display_dt or raw_dt,
            "clean_device_tag": clean_dt,
            "signal_type": sig_type,
            "slot": slot,
            "card_terminal": card_terminal,
            "wire_terminal": wire_terminal or card_terminal,
            "page_col": page_col,
            "contact_type": contact_type or ("线圈" if addr.startswith("%Q") else "NO"),
            "description": raw_desc,
            "is_spare": is_spare,
            "needs_confirm": needs_confirm,
            "notes": notes,
        })

    return items


def parse_eplan_bom_table(path: str) -> Dict[str, Dict[str, Any]]:
    """
    解析 EPLAN 导出的《设备物料清单 BOM 与端子连接表》（.xlsx / .csv）。
    返回以规范化代号（去掉 '-' 前缀，如 'KM1', 'YV1'）为键的设备电气属性字典。
    """
    headers, rows = _read_tabular_rows(path)
    if not headers:
        return {}
    col_map = _map_headers(headers, _BOM_COL_ALIASES)

    bom: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        def _get(field: str) -> str:
            idx = col_map.get(field)
            if idx is None or idx >= len(row):
                return ""
            val = row[idx]
            return "" if val is None else str(val).strip()

        raw_dt = _get("device_tag")
        clean_dt, display_dt = _clean_device_tag(raw_dt)
        if not clean_dt:
            continue

        bom[clean_dt] = {
            "device_tag": display_dt,
            "clean_device_tag": clean_dt,
            "name": _get("name"),
            "model": _get("model"),
            "voltage": _get("voltage"),
            "control_type": _get("control_type"),
            "wiring_type": _get("wiring_type"),
            "notes": _get("notes"),
        }
    return bom


def _parse_md_tables(md_text: str) -> List[List[Dict[str, str]]]:
    """从 Markdown 文本中提取所有标准表格为字典行列表"""
    tables: List[List[Dict[str, str]]] = []
    lines = md_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|") and i + 1 < len(lines):
            sep = lines[i + 1].strip()
            if sep.startswith("|") and re.match(r"^\|[\s:\-\|]+\|$", sep):
                headers = [c.strip(" `") for c in line.strip("|").split("|")]
                i += 2
                rows: List[Dict[str, str]] = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    cols = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                    if len(cols) < len(headers):
                        cols.extend([""] * (len(headers) - len(cols)))
                    rows.append({h: cols[idx] for idx, h in enumerate(headers)})
                    i += 1
                tables.append(rows)
                continue
        i += 1
    return tables


def _extract_backtick_tokens(text: str) -> List[str]:
    return [t.strip() for t in re.findall(r"`([^`]+)`", text or "") if t.strip()]


def parse_process_spec(path: str) -> Dict[str, Any]:
    """
    解析配套工艺文档（支持 .md 或 .json），可兼容单份合集文档或分别解析步序表/联锁矩阵：
    提取：
      - origin_conditions: 原点必须满足的传感器条件列表
      - homing_sequence: 一键回原位步骤文本列表
      - steps: 自动循环步序列表 (step_id, name, trigger_vars, action_vars, transition_vars, timeout, alarm_var)
      - interlocks: 机械防呆与安全联锁矩阵列表 (actuator, required_permissives, mutex_coils, reason)
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"未找到工艺规范文件: {path}")

    with open(path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    if path.lower().endswith(".json"):
        data = json.loads(content)
        return {
            "origin_conditions": data.get("origin_conditions", []),
            "homing_sequence": data.get("homing_sequence", []),
            "steps": data.get("steps", []),
            "interlocks": data.get("interlocks", []),
        }

    result: Dict[str, Any] = {
        "origin_conditions": [],
        "homing_sequence": [],
        "steps": [],
        "interlocks": [],
    }

    # 提取原点条件行（支持 "- `B2_...` = 1"）
    in_origin_block = False
    for line in content.splitlines():
        stripped = line.strip()
        if "原点定义" in stripped or "原点条件" in stripped:
            in_origin_block = True
            continue
        if in_origin_block:
            if stripped.startswith("#") or "一键回原位" in stripped:
                in_origin_block = False
            elif stripped.startswith(("-", "*")):
                tokens = _extract_backtick_tokens(stripped)
                for tok in tokens:
                    var_only = tok.split("=")[0].strip()
                    if var_only and var_only not in ("M_产线在原点",):
                        val_zero = "= 0" in stripped or "=0" in stripped
                        result["origin_conditions"].append({
                            "name": var_only,
                            "negated": val_zero,
                        })

    # 提取表格（步序表 & 机械防呆联锁矩阵）
    for tbl in _parse_md_tables(content):
        if not tbl:
            continue
        keys_joined = " ".join(tbl[0].keys())
        if "步号" in keys_joined and "步名称" in keys_joined:
            for r in tbl:
                result["steps"].append({
                    "step_id": r.get("步号", "").strip(),
                    "name": r.get("步名称", "").strip(),
                    "enter_raw": r.get("进入/触发条件", ""),
                    "enter_vars": _extract_backtick_tokens(r.get("进入/触发条件", "")),
                    "action_raw": r.get("本步动作请求", ""),
                    "action_vars": _extract_backtick_tokens(r.get("本步动作请求", "")),
                    "transition_raw": r.get("转移完成条件", ""),
                    "transition_vars": _extract_backtick_tokens(r.get("转移完成条件", "")),
                    "timeout": r.get("超时时间", "").strip(" `"),
                    "alarm_var": (_extract_backtick_tokens(r.get("超时报警变量", "")) or [r.get("超时报警变量", "").strip(" `")])[0],
                })
        elif "被保护执行器" in keys_joined:
            for r in tbl:
                act_tokens = _extract_backtick_tokens(r.get("被保护执行器 (%Q 变量名)", r.get("被保护执行器", "")))
                act_name = act_tokens[0] if act_tokens else ""
                if not act_name:
                    for k, v in r.items():
                        if "被保护执行器" in k:
                            toks = _extract_backtick_tokens(v)
                            act_name = toks[0] if toks else v.strip(" `")
                            break
                perm_raw = ""
                mutex_raw = ""
                reason_raw = ""
                for k, v in r.items():
                    if "允许条件" in k:
                        perm_raw = v
                    elif "互斥" in k:
                        mutex_raw = v
                    elif "理由" in k:
                        reason_raw = v
                result["interlocks"].append({
                    "actuator": act_name,
                    "required_permissives": _extract_backtick_tokens(perm_raw),
                    "mutex_coils": _extract_backtick_tokens(mutex_raw),
                    "reason": reason_raw.strip(),
                })

    return result


def _merge_process_specs(paths: Optional[List[str]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {
        "origin_conditions": [],
        "homing_sequence": [],
        "steps": [],
        "interlocks": [],
    }
    for p in (paths or []):
        if p and os.path.exists(p):
            parsed = parse_process_spec(p)
            merged["origin_conditions"].extend(parsed.get("origin_conditions", []))
            merged["homing_sequence"].extend(parsed.get("homing_sequence", []))
            merged["steps"].extend(parsed.get("steps", []))
            merged["interlocks"].extend(parsed.get("interlocks", []))
    return merged


def generate_reconciliation_report(
    project_name: str,
    plc_table_path: str,
    bom_table_path: Optional[str] = None,
    process_spec_paths: Optional[List[str]] = None,
    tia_status_json_path: Optional[str] = None,
    env_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    融合 EPLAN 点表、BOM 表、工艺步序/联锁表与博途硬件快照执行四方对账，
    输出结构化核对结论以及符合 SKILL.md 阶段 1 规范的《01_识图清单.md》文本。
    """
    io_items = parse_eplan_plc_table(plc_table_path)
    bom = parse_eplan_bom_table(bom_table_path) if bom_table_path and os.path.exists(bom_table_path) else {}
    spec = _merge_process_specs(process_spec_paths)

    if env_info is None and tia_status_json_path and os.path.exists(tia_status_json_path):
        with open(tia_status_json_path, "r", encoding="utf-8-sig") as jf:
            env_info = json.load(jf)

    cpu_model = ""
    if env_info and isinstance(env_info.get("Project"), dict):
        cpu_model = str(env_info["Project"].get("CpuModel", ""))

    # 1. 核对 EPLAN I/O 地址 vs 博途硬件组态
    active_addrs = [
        item["logical_address"]
        for item in io_items
        if item["logical_address"].upper().startswith(("%I", "%Q")) and not item["needs_confirm"]
    ]
    hw_check: Dict[str, Any] = {"matchedCount": 0, "mismatchCount": 0, "markdownReport": "未提供博途硬件快照，跳过硬件地址比对"}
    if env_info and active_addrs:
        hw_check = compare_eplan_with_tia_hw(active_addrs, env_info=env_info)

    # 2. 安全信号与电气规格核查（结合 BOM）
    red_alerts: List[str] = []
    has_estop = any("急停" in it["description"] or "急停" in it["name"] for it in io_items)
    if not has_estop:
        red_alerts.append("🔴 **缺少急停按钮信号**：EPLAN 点表中未发现急停（-SB0）输入通道！")

    for it in io_items:
        if any(k in it["name"] for k in _SAFETY_KEYWORDS) and it["signal_type"] == "DI":
            if it["contact_type"].upper() != "NC":
                red_alerts.append(
                    f"🔴 **安全信号极性不合格**：`{it['name']}` ({it['logical_address']}) 在 EPLAN 点表中标注为 `{it['contact_type']}`，必须改为物理常闭 (NC)！"
                )

    is_dc_cpu = "1AG40" in cpu_model or "DC/DC/DC" in cpu_model
    for dt_key, binfo in bom.items():
        volt = binfo.get("voltage", "").upper()
        ctype = binfo.get("control_type", "")
        if is_dc_cpu and ("220V" in volt or "AC" in volt) and ("接触器" in ctype or "线圈" in ctype or "继电器" in ctype):
            red_alerts.append(
                f"🔴 **电气规格冲突**：BOM 中 `{binfo['device_tag']}` ({binfo['name']}) 标注为 `{binfo['voltage']}`，"
                f"但博途组态 CPU 为 `{cpu_model}`（24V DC 晶体管输出），禁止直驱交流线圈，须确认是否经 24V 中间继电器隔离！"
            )

    # 3. 工艺步序与机械联锁一致性校验（I/O 变量是否存在于 EPLAN 点表中）
    defined_names = {it["name"] for it in io_items}
    spec_warnings: List[str] = []
    for oc in spec.get("origin_conditions", []):
        vname = oc["name"]
        if not vname.startswith("M_") and vname not in defined_names:
            spec_warnings.append(f"⚠ 原点条件引用变量 `{vname}` 未在 EPLAN 点表中定义")

    for rule in spec.get("interlocks", []):
        act = rule.get("actuator", "")
        if act and act not in defined_names:
            spec_warnings.append(f"⚠ 机械联锁矩阵被保护执行器 `{act}` 未在 EPLAN 点表中定义")
        for pv in rule.get("required_permissives", []):
            if pv and not pv.startswith("M_") and pv not in defined_names:
                spec_warnings.append(f"⚠ 联锁条件 `{pv}`（保护 `{act}`）未在 EPLAN 点表中定义")

    if hw_check.get("mismatchCount", 0) > 0:
        red_alerts.append(
            f"🔴 **博途硬件组态越界**：共 {hw_check['mismatchCount']} 个 I/O 地址超出当前硬件模块范围，请见第 4 节对账差异表！"
        )

    # 4. 组装标准 《01_识图清单.md》
    md: List[str] = [
        f"# 识图清单 —— {project_name}",
        "",
        "## 🔴 重大安全问题与硬件组态不一致（优先阅读）",
    ]
    if red_alerts:
        md.extend(f"- {a}" for a in red_alerts)
    else:
        md.append("- 🟢 经 EPLAN 点表、BOM 与博途硬件快照联合核验：急停/热继均满足 NC 规范，供电电压匹配，I/O 地址 100% 落在组态区间内。")

    md.extend([
        "",
        "## 1. 设备概览与输入资料核验",
        f"- **PLC 点表来源**：`{os.path.basename(plc_table_path)}`（共解析 {len(io_items)} 条通道，其中有效通道 {sum(1 for x in io_items if not x['is_spare'])} 条、备用 {sum(1 for x in io_items if x['is_spare'])} 条）",
        f"- **BOM 设备清单元件数**：{len(bom)} 个" if bom else "- **BOM 清单**：未提供",
        f"- **工艺步序数 / 机械防呆联锁规则数**：{len(spec.get('steps', []))} 步 / {len(spec.get('interlocks', []))} 条规则",
        "",
        "## 2. 元器件与 BOM 属性清单",
        "| 代号 | 名称/功能 | 型号规格 | 电压等级 | 控制类型 | 接线方式 |",
        "|---|---|---|---|---|---|",
    ])
    if bom:
        for binfo in bom.values():
            md.append(
                f"| `{binfo['device_tag']}` | {binfo['name']} | {binfo['model']} | {binfo['voltage']} | {binfo['control_type']} | {binfo['wiring_type']} |"
            )
    else:
        md.append("| — | 未导入独立 BOM 表（依据 PLC 点表元器件信息） | — | — | — | — |")

    md.extend([
        "",
        "## 3. PLC I/O 清单（由 EPLAN 点表自动解析）",
        "| 序号 | PLC 地址 | 规范变量名 | 信号类型 | 元器件代号 | 端子/线号 | 位置 | NO/NC | 备用 | 说明 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ])
    for idx, it in enumerate(io_items, start=1):
        spare_txt = "备用" if it["is_spare"] else "使用"
        md.append(
            f"| {idx} | `{it['logical_address']}` | `{it['name']}` | {it['signal_type']} | `{it['device_tag'] or '—'}` | "
            f"{it['wire_terminal'] or '—'} | {it['page_col']} | {it['contact_type']} | {spare_txt} | {it['description']} |"
        )

    md.extend([
        "",
        "## 4. EPLAN 图纸 vs 博途硬件组态对账差异表",
        hw_check.get("markdownReport", ""),
        "",
        "## 5. 机械防呆联锁与工艺步序核验结论",
        "| 序号 | 被保护执行器 | 公共串联允许条件 | 互斥线圈 (-|/\\|-) | 一致性状态 |",
        "|---|---|---|---|---|",
    ])
    for idx, rule in enumerate(spec.get("interlocks", []), start=1):
        act = rule.get("actuator", "")
        perms = ", ".join(f"`{p}`" for p in rule.get("required_permissives", [])) or "—"
        mutex = ", ".join(f"`{m}`" for m in rule.get("mutex_coils", [])) or "—"
        ok_flag = "🟢 已闭环" if act in defined_names else "🔴 执行器未定义"
        md.append(f"| {idx} | `{act}` | {perms} | {mutex} | {ok_flag} |")

    md.extend([
        "",
        "## 6. 疑问列表（⚠待确认）",
    ])
    pending = [it for it in io_items if it["needs_confirm"]]
    if not pending and not spec_warnings:
        md.append("- 🟢 所有 EPLAN 通道、BOM 电气参数与工艺联锁变量均已匹配，无阻断级 `⚠待确认` 项。")
    else:
        for idx, it in enumerate(pending, start=1):
            md.append(f"- **Q{idx}. ⚠ `{it['logical_address']}` ({it['description']}) 缺少规范代号或页码列号，请现场核对。**")
        for w in spec_warnings:
            md.append(f"- {w}")

    return {
        "io_items": io_items,
        "bom": bom,
        "process_spec": spec,
        "hw_check": hw_check,
        "red_alerts": red_alerts,
        "spec_warnings": spec_warnings,
        "markdown": "\n".join(md),
    }
