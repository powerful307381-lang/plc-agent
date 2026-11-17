# -*- coding: utf-8 -*-
"""
模块名称: parse_engineering_drawings
功能说明:
  1. 多模态图纸与工艺输入提取（支持 GEMINI_API_KEY + google-genai 交错提取；
     未配置 Key 或离线环境自动无感降级为本地 pdfplumber/openpyxl/eplan_parser 逻辑），
     输出标准化状态机 JSON 中间表示 (IR)；
  2. 缺失工艺澄清问答生成（doc-coauthoring 模式）与控制功能说明书 FDS.docx 固化导出；
  3. 内建本地 Safety Critic 形式化审查器（借鉴 Google ADK critic_agent 模式），
     在导入博途前对照安全联锁矩阵与能流拓扑割点 S1~S5 逐条核查，不达标自动生成重写指令。
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from google import genai  # type: ignore
except ImportError:
    genai = None

try:
    import pdfplumber  # type: ignore
except ImportError:
    pdfplumber = None

try:
    import docx  # type: ignore
except ImportError:
    docx = None

import openpyxl

try:
    from eplan_parser import (
        parse_eplan_bom_table,
        parse_eplan_plc_table,
        parse_process_spec,
    )
    from validate_lad_xml import (
        build_power_graph,
        collect_defined_symbols,
        series_contacts_of_coil,
        validate_project_xmls,
    )
except ImportError:
    from scripts.eplan_parser import (  # type: ignore
        parse_eplan_bom_table,
        parse_eplan_plc_table,
        parse_process_spec,
    )
    from scripts.validate_lad_xml import (  # type: ignore
        build_power_graph,
        collect_defined_symbols,
        series_contacts_of_coil,
        validate_project_xmls,
    )


_SAFETY_KWS = ("急停", "安全门", "光栅", "热继", "极限", "限位", "停止", "过载")


def _extract_pdf_text_clues(pdf_paths: Optional[List[str]]) -> List[str]:
    """使用 pdfplumber（若安装）或简单文本回退提取 PDF 图纸关键工艺/安全线索"""
    clues: List[str] = []
    for p in (pdf_paths or []):
        if not p or not os.path.exists(p):
            continue
        if pdfplumber is not None:
            try:
                with pdfplumber.open(p) as pdf:
                    for page_idx, page in enumerate(pdf.pages[:20], start=1):
                        txt = (page.extract_text() or "").strip()
                        if txt:
                            clues.append(f"[{os.path.basename(p)} P{page_idx}] {txt[:600]}")
            except Exception:
                continue
        else:
            # 降级：保留文件存在线索，不因缺失 pdfplumber 崩溃
            clues.append(f"[PDF文件已登记-未安装pdfplumber文本提取] {os.path.basename(p)}")
    return clues


def _parse_xlsx_safety_matrix(path: str) -> List[Dict[str, Any]]:
    """支持直接解析 .xlsx 格式的安全联锁矩阵"""
    rules: List[Dict[str, Any]] = []
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True) if any(str(c or "").strip() for c in r)]
    if len(rows) < 2:
        return rules
    headers = [str(h or "").strip() for h in rows[0]]
    for row in rows[1:]:
        row_map = {headers[i]: str(row[i] or "").strip() for i in range(min(len(headers), len(row)))}
        act = ""
        perms: List[str] = []
        mutex: List[str] = []
        reason = ""
        for k, v in row_map.items():
            if not v or v in ("—", "-", "无"):
                continue
            if "执行器" in k or "输出" in k:
                act = v.strip("` ")
            elif "允许" in k or "条件" in k:
                perms.extend([t.strip("` ") for t in re.split(r"[,，;；\s]+", v) if t.strip("` ")])
            elif "互斥" in k or "禁止" in k:
                mutex.extend([t.strip("` ") for t in re.split(r"[,，;；\s]+", v) if t.strip("` ")])
            elif "理由" in k or "说明" in k:
                reason = v
        if act:
            rules.append({
                "actuator": act,
                "required_permissives": perms,
                "mutex_coils": mutex,
                "reason": reason,
            })
    return rules


def _try_gemini_multimodal_extract(
    pdf_paths: List[str],
    base_ir: Dict[str, Any],
    api_key: str,
) -> Optional[Dict[str, Any]]:
    """当显式提供 GEMINI_API_KEY 且 google-genai 可用时，调用 gemini-3.1-pro-preview 补充图纸机械约束"""
    if genai is None or not api_key or not pdf_paths:
        return None
    try:
        client = genai.Client(api_key=api_key)
        prompt = (
            "你是工业控制专家。请结合以下电气/气动/机械图纸提取执行器的机械干涉与电磁阀中位机能，"
            "严格输出 JSON：{valve_center_types: {}, extra_interlocks: [{actuator, required_permissives, mutex_coils, reason}]}。"
            f"\n已识别I/O变量: {[t['name'] for t in base_ir.get('io_tags', [])]}"
        )
        parts: List[Any] = [prompt]
        for p in pdf_paths[:4]:
            if os.path.exists(p):
                with open(p, "rb") as f:
                    data = f.read()
                ext = os.path.splitext(p)[1].lower()
                mime = "application/pdf" if ext == ".pdf" else "image/png"
                parts.append(genai.types.Part.from_bytes(data=data, mime_type=mime))
        resp = client.models.generate_content(
            model="gemini-3.1-pro-preview",
            contents=parts,
            config=genai.types.GenerateContentConfig(response_mime_type="application/json"),
        )
        if getattr(resp, "text", None):
            return json.loads(resp.text)
    except Exception:
        return None
    return None


def extract_process_ir_from_drawings(
    pdf_paths: Optional[List[str]] = None,
    xlsx_io_path: Optional[str] = None,
    bom_path: Optional[str] = None,
    spec_paths: Optional[List[str]] = None,
    safety_matrix_path: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    提取并融合 EPLAN 表格、BOM、图纸 PDF、工艺步序与安全联锁矩阵，
    生成标准化状态机 JSON 中间表示 (IR)。无 GEMINI_API_KEY 时自动降级本地解析。
    """
    io_tags = parse_eplan_plc_table(xlsx_io_path) if xlsx_io_path and os.path.exists(xlsx_io_path) else []
    bom = parse_eplan_bom_table(bom_path) if bom_path and os.path.exists(bom_path) else {}

    merged_spec: Dict[str, Any] = {
        "origin_conditions": [],
        "homing_sequence": [],
        "steps": [],
        "interlocks": [],
    }
    for sp in (spec_paths or []):
        if sp and os.path.exists(sp):
            parsed = parse_process_spec(sp)
            merged_spec["origin_conditions"].extend(parsed.get("origin_conditions", []))
            merged_spec["homing_sequence"].extend(parsed.get("homing_sequence", []))
            merged_spec["steps"].extend(parsed.get("steps", []))
            merged_spec["interlocks"].extend(parsed.get("interlocks", []))

    if safety_matrix_path and os.path.exists(safety_matrix_path):
        ext = os.path.splitext(safety_matrix_path)[1].lower()
        if ext in (".xlsx", ".xlsm"):
            merged_spec["interlocks"].extend(_parse_xlsx_safety_matrix(safety_matrix_path))
        else:
            parsed_mat = parse_process_spec(safety_matrix_path)
            merged_spec["interlocks"].extend(parsed_mat.get("interlocks", []))

    pdf_clues = _extract_pdf_text_clues(pdf_paths)

    safety_inputs: List[Dict[str, Any]] = []
    actuators: List[Dict[str, Any]] = []
    valve_center_types: Dict[str, str] = {}
    stations: Set[str] = set()

    for t in io_tags:
        if t.get("is_spare"):
            continue
        name = t["name"]
        addr = t["logical_address"].upper()
        st_m = re.search(r"(OP\d+|ST\d+|工位\d+)", name + " " + t.get("description", ""), re.IGNORECASE)
        if st_m:
            stations.add(st_m.group(1).upper())

        if addr.startswith("%I") and any(k in name or k in t.get("description", "") for k in _SAFETY_KWS):
            safety_inputs.append({
                "name": name,
                "address": addr,
                "contact_type": t.get("contact_type", ""),
            })
        elif addr.startswith("%Q"):
            dt_key = t.get("device_tag", "").lstrip("-")
            b_info = bom.get(dt_key, {})
            ctrl_type = b_info.get("control_type", "")
            if not ctrl_type:
                ctrl_type = "电磁阀" if "YV" in name.upper() or "气缸" in name else "接触器/执行器"
            actuators.append({
                "name": name,
                "address": addr,
                "device_tag": dt_key,
                "control_type": ctrl_type,
            })
            if "YV" in name.upper() or "阀" in name:
                valve_center_types[name] = ctrl_type

    effective_key = api_key or os.environ.get("GEMINI_API_KEY", "").strip()
    extraction_mode = "local_deterministic_fallback"
    base_ir: Dict[str, Any] = {
        "extraction_mode": extraction_mode,
        "stations": sorted(stations) if stations else ["OP10_主工位"],
        "io_tags": io_tags,
        "safety_inputs": safety_inputs,
        "actuators": actuators,
        "valve_center_types": valve_center_types,
        "origin_conditions": merged_spec["origin_conditions"],
        "homing_sequence": merged_spec["homing_sequence"],
        "steps": merged_spec["steps"],
        "interlocks": merged_spec["interlocks"],
        "pdf_clues": pdf_clues,
        "missing_clarifications": [],
    }

    gemini_patch = _try_gemini_multimodal_extract(pdf_paths or [], base_ir, effective_key)
    if gemini_patch:
        base_ir["extraction_mode"] = "gemini_3_1_pro_multimodal"
        for k, v in (gemini_patch.get("valve_center_types") or {}).items():
            base_ir["valve_center_types"][k] = v
        for rule in (gemini_patch.get("extra_interlocks") or []):
            if isinstance(rule, dict) and rule.get("actuator"):
                base_ir["interlocks"].append(rule)

    base_ir["missing_clarifications"] = generate_clarification_questions(base_ir)
    return base_ir


def generate_clarification_questions(ir_data: Dict[str, Any]) -> List[str]:
    """
    借鉴 doc-coauthoring 模式：根据 IR 缺失项生成 5~10 个结构化澄清问题，
    供工程师补充工艺步序、回原策略、交接握手与电磁阀中位机能。
    """
    questions: List[str] = []

    if not ir_data.get("steps"):
        questions.append(
            "【步序缺失】请明确各工位自动循环的完整动作步序（S10 -> S20 -> ...），包含触发条件、到位传感器及每步超时阈值（推荐默认 T#5S）？"
        )
    if not ir_data.get("homing_sequence"):
        questions.append(
            "【回原顺序】按下手动一键回原位按钮时，多个执行器（如压装头 vs 推料气缸）的安全退回先后顺序是什么？（推荐：先退垂直/压装轴，到位后再退水平送料轴）"
        )
    if not ir_data.get("interlocks"):
        questions.append(
            "【防撞联锁】除急停与安全门外，各气缸/电机之间有哪些绝对不可同时动作的机械防撞约束（例如压头不在上到位禁止送料）？"
        )
    if not ir_data.get("origin_conditions"):
        questions.append(
            "【原点定义】请确认启动自动循环前必须同时满足的原点传感器信号（如：各气缸缩回/上到位磁性开关）？"
        )

    questions.append(
        "【电磁阀中位机能】气动/液压双电控电磁阀在急停断电或安全门打开时，要求中封保压还是中泄卸压（关系到断电恢复后回原位安全性）？"
    )
    questions.append(
        "【工位交接握手】若存在多工位衔接，上下游是否采用标准 UDT_LineHandover（ReqToNext / RdyFromNext / TransferDone）四步闭环握手？"
    )
    questions.append(
        "【安全与故障复位】急停或安全门恢复后，是仅允许清除故障并手动回原位，还是允许断点续走？（安全规范强制推荐：禁止自动续走，必须人工按故障复位）"
    )
    questions.append(
        "【超时故障分级】动作超时报警（B级故障）发生后，要求仅挂起当前工位并保持已夹紧执行器状态，还是整线立即全停？"
    )

    pending_tags = [t["logical_address"] for t in ir_data.get("io_tags", []) if t.get("needs_confirm")]
    if pending_tags:
        questions.append(
            f"【点表待核实】检测到未标准化的地址或缺少图纸定位的点位 {pending_tags[:5]}，请现场核对真实 %I/%Q 地址与常开/常闭极性？"
        )

    return questions[:10]


def export_fds_docx(
    ir_data: Dict[str, Any],
    out_docx_path: str,
    project_name: str = "自动化产线控制工程",
) -> str:
    """使用 python-docx 将工艺状态机 IR 固化为标准《控制功能说明书 (FDS).docx》"""
    if docx is None:
        raise RuntimeError("未安装 python-docx，无法导出 .docx 文件")

    doc = docx.Document()
    doc.add_heading(f"{project_name} — 控制功能说明书 (FDS)", level=0)

    doc.add_heading("1. 工位概览与解析模式", level=1)
    doc.add_paragraph(f"提取模式: {ir_data.get('extraction_mode', 'local')}")
    doc.add_paragraph(f"包含工位: {', '.join(ir_data.get('stations', []))}")

    doc.add_heading("2. 安全输入与物理执行器清单", level=1)
    doc.add_paragraph("安全信号（物理常闭 NC，程序常开扫描）:")
    for si in ir_data.get("safety_inputs", []):
        doc.add_paragraph(f"- {si.get('name')} ({si.get('address')}) [{si.get('contact_type')}]", style="List Bullet")

    doc.add_paragraph("物理执行器与电磁阀类型:")
    for act in ir_data.get("actuators", []):
        doc.add_paragraph(
            f"- {act.get('name')} ({act.get('address')}) | 控制类型: {act.get('control_type')}",
            style="List Bullet",
        )

    doc.add_heading("3. 自动循环步序表", level=1)
    steps = ir_data.get("steps", [])
    if steps:
        tbl = doc.add_table(rows=1, cols=5)
        tbl.style = "Table Grid"
        hdr = tbl.rows[0].cells
        hdr[0].text, hdr[1].text, hdr[2].text, hdr[3].text, hdr[4].text = (
            "步号", "步名称", "本步动作请求", "转移完成条件", "超时设置"
        )
        for st in steps:
            row_cells = tbl.add_row().cells
            row_cells[0].text = str(st.get("step_id", ""))
            row_cells[1].text = str(st.get("step_name", ""))
            row_cells[2].text = ", ".join(st.get("action_requests", []))
            row_cells[3].text = ", ".join(st.get("transition_vars", []))
            row_cells[4].text = str(st.get("timeout", ""))
    else:
        doc.add_paragraph("待与工艺工程师澄清补全步序。")

    doc.add_heading("4. 机械防呆与安全联锁矩阵", level=1)
    for rule in ir_data.get("interlocks", []):
        doc.add_paragraph(
            f"- 被保护执行器 {rule.get('actuator')}: 允许条件={rule.get('required_permissives')} | "
            f"互斥线圈={rule.get('mutex_coils')} | 理由={rule.get('reason', '')}",
            style="List Bullet",
        )

    doc.add_heading("5. 工艺澄清问题记录", level=1)
    for idx, q in enumerate(ir_data.get("missing_clarifications", []), start=1):
        doc.add_paragraph(f"{idx}. {q}")

    abs_out = os.path.abspath(out_docx_path)
    os.makedirs(os.path.dirname(abs_out), exist_ok=True)
    doc.save(abs_out)
    return abs_out


def _extract_coil_series_map(xml_dirs: List[str]) -> Tuple[Dict[str, List[Tuple[str, int, List[Tuple[str, bool]]]]], Dict[str, List[Tuple[str, bool]]]]:
    """
    基于 validate_lad_xml 的有向能流割点图，收集全工程所有线圈的必经串联触点，
    并计算中间变量的安全/条件传递闭包（排除自锁旁路）。
    """
    coil_series: Dict[str, List[Tuple[str, int, List[Tuple[str, bool]]]]] = {}
    var_direct_series: Dict[str, List[Tuple[str, bool]]] = {}

    xml_files: List[str] = []
    for d in xml_dirs:
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith(".xml"):
                    xml_files.append(os.path.join(d, fn))

    for fp in xml_files:
        try:
            with open(fp, "r", encoding="utf-8-sig", errors="replace") as f:
                root = ET.fromstring(f.read())
        except Exception:
            continue

        blk_el = root.find(".//SW.Blocks.FC")
        if blk_el is None:
            blk_el = root.find(".//SW.Blocks.FB")
        if blk_el is None:
            continue
        bname_el = blk_el.find("AttributeList/Name")
        bname = (bname_el.text or "").strip() if bname_el is not None else os.path.basename(fp)

        for nw_idx, cu in enumerate(root.findall(".//SW.Blocks.CompileUnit"), start=1):
            flg = cu.find(".//{*}FlgNet")
            if flg is None:
                continue
            parts_el = flg.find("{*}Parts")
            wires_el = flg.find("{*}Wires")
            if parts_el is None or wires_el is None:
                continue

            uid_to_sym: Dict[str, str] = {}
            part_info_by_uid: Dict[str, Dict[str, Any]] = {}
            for ch in parts_el:
                uid = ch.attrib.get("UId", "")
                tag = ch.tag.split("}")[-1]
                if not uid:
                    continue
                if tag == "Access":
                    comps = [c.attrib.get("Name", "") for c in ch.findall(".//{*}Component") if c.attrib.get("Name")]
                    if comps:
                        uid_to_sym[uid] = ".".join(comps[:2]) if len(comps) >= 2 else comps[0]
                elif tag == "Part":
                    part_info_by_uid[uid] = {
                        "tag": "Part",
                        "name": ch.attrib.get("Name", ""),
                        "negated": ch.find("{*}Negated[@Name='operand']") is not None,
                        "elem": ch,
                    }

            contact_uid_to_var: Dict[str, str] = {}
            coil_uids: List[Tuple[str, str, str]] = []
            for w in wires_el:
                id_uids = [sub.attrib.get("UId", "") for sub in w if sub.tag.split("}")[-1] == "IdentCon"]
                ncs = [(sub.attrib.get("UId", ""), sub.attrib.get("Name", "")) for sub in w if sub.tag.split("}")[-1] == "NameCon"]
                for iu in id_uids:
                    sym = uid_to_sym.get(iu)
                    if not sym:
                        continue
                    for pu, pin in ncs:
                        if pin == "operand":
                            pn = part_info_by_uid.get(pu, {}).get("name", "")
                            if pn == "Contact":
                                contact_uid_to_var[pu] = sym
                            elif pn in ("Coil", "SCoil"):
                                coil_uids.append((pu, sym, pn))

            if not coil_uids:
                continue
            edges = build_power_graph(wires_el, part_info_by_uid)
            c_uids = list(contact_uid_to_var.keys())
            for cu_id, c_var, _ in coil_uids:
                sc = series_contacts_of_coil(edges, cu_id, c_uids)
                if sc is None:
                    continue
                series_pairs = [
                    (contact_uid_to_var[u], bool(part_info_by_uid[u].get("negated")))
                    for u in sc
                    if contact_uid_to_var[u] != c_var
                ]
                coil_series.setdefault(c_var, []).append((bname, nw_idx, series_pairs))
                if c_var not in var_direct_series:
                    var_direct_series[c_var] = list(series_pairs)

    return coil_series, var_direct_series


def _collect_transitive_series(
    series_pairs: List[Tuple[str, bool]],
    var_direct_series: Dict[str, List[Tuple[str, bool]]],
) -> Set[Tuple[str, bool]]:
    """沿正向（常开）串联条件向下展开上游中间变量包含的串联条件"""
    collected: Set[Tuple[str, bool]] = set(series_pairs)
    queue = [v for v, neg in series_pairs if not neg and v in var_direct_series]
    visited: Set[str] = set()
    while queue:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)
        for dep_var, dep_neg in var_direct_series.get(cur, []):
            collected.add((dep_var, dep_neg))
            if not dep_neg and dep_var in var_direct_series and dep_var not in visited:
                queue.append(dep_var)
    return collected


def run_local_safety_critic(
    xml_dir: str,
    ir_data: Dict[str, Any],
    eplan_io_path: Optional[str] = None,
    tia_status_json_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    本地 Safety Critic 形式化审查器（Google ADK critic_agent 模式）：
      1. 运行 validate_project_xmls 执行全量静态语法与 S1~S5 拓扑审计；
      2. 对照 ir_data['interlocks'] 安全联锁矩阵，利用有向能流割点验证每个被保护执行器的
         必经串联主干路是否真正串入了所有 required_permissives 与 mutex_coils；
      3. 不达标时生成精确的重写指令 (revision_directives)，驱动 lad_builder.py 重写自愈。
    """
    static_res = validate_project_xmls(
        xml_dir,
        eplan_io_path=eplan_io_path,
        tia_status_json_path=tia_status_json_path,
    )

    violations: List[Dict[str, Any]] = []
    revision_directives: List[str] = []

    for err in static_res.errors:
        violations.append({
            "code": "STATIC_SAFETY_OR_SYNTAX",
            "block": "",
            "network": 0,
            "actuator": "",
            "message": err,
        })
        revision_directives.append(f"修复静态安全/拓扑错误: {err}")

    parent_dir = os.path.dirname(os.path.abspath(xml_dir))
    scan_dirs = [xml_dir]
    sibling_tag_dir = os.path.join(parent_dir, "02_变量表")
    if os.path.isdir(sibling_tag_dir):
        scan_dirs.append(sibling_tag_dir)

    coil_series, var_direct_series = _extract_coil_series_map(scan_dirs)

    for rule in ir_data.get("interlocks", []):
        act = str(rule.get("actuator", "")).strip()
        if not act:
            continue
        req_perms = [p for p in rule.get("required_permissives", []) if p]
        req_mutex = [m for m in rule.get("mutex_coils", []) if m]

        occurrences = coil_series.get(act, [])
        if not occurrences:
            msg = f"安全联锁矩阵审查失败: 被保护执行器 '{act}' 未在任何 LAD 块中被线圈驱动"
            violations.append({
                "code": "MISSING_ACTUATOR_COIL",
                "block": "",
                "network": 0,
                "actuator": act,
                "message": msg,
            })
            revision_directives.append(f"使用 lad_builder.build_manual_auto_interlocked_actuator_network 为 '{act}' 构建受控驱动网络。")
            continue

        for bname, nw_idx, direct_series in occurrences:
            expanded = _collect_transitive_series(direct_series, var_direct_series)
            missing_perms = [p for p in req_perms if (p, False) not in expanded]
            missing_mutex = [m for m in req_mutex if (m, True) not in direct_series]
            if missing_perms or missing_mutex:
                msg = (
                    f"[{bname} 程序段 {nw_idx}] 矩阵联锁缺失: 执行器 '{act}' 的必经串联主干路缺少 "
                    f"允许条件={missing_perms} 或 互斥常闭={missing_mutex}！"
                )
                violations.append({
                    "code": "INTERLOCK_MATRIX_VIOLATION",
                    "block": bname,
                    "network": nw_idx,
                    "actuator": act,
                    "missing_permissives": missing_perms,
                    "missing_mutex": missing_mutex,
                    "message": msg,
                })
                revision_directives.append(
                    f"打回重写 [{bname} NW{nw_idx}]: 在 '{act}' 的 O 汇流节点之后必经主干路串联 "
                    f"常开={missing_perms} 与常闭={missing_mutex}。"
                )

    return {
        "passed": len(violations) == 0,
        "static_errors": static_res.errors,
        "static_warnings": static_res.warnings,
        "violations": violations,
        "revision_directives": revision_directives,
    }
