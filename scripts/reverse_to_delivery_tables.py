# -*- coding: utf-8 -*-
"""
模块名称: reverse_to_delivery_tables
功能说明: 【功能模块 4 工业级实现】
         从任意已导出的博途 V16 XML 目录（或附加到运行中的博途进程反向导出）一键生成：
         1. 《05_现场调试交付三表.xlsx》 (说明、I_O打点表、单机空载调试步骤、联锁排障速查表)
         2. 《05_现场调试交付三表.md》 预览
         3. 《PLC变量表_中文.xlsx》 中文变量汇总 (8 列标准格式)
符合规范: eplan-to-tia-lad SKILL.md 阶段 5, references/08_调试交付三表模板.md
"""

import os
import sys
import re
import shutil
import subprocess
import argparse
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Dict, Any, Tuple

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
root_dir = os.path.dirname(parent_dir)
for p in [current_dir, parent_dir, root_dir, os.path.join(root_dir, "tools")]:
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from delivery_builder import create_delivery_tables
except ImportError:
    try:
        from tools.delivery_builder import create_delivery_tables
    except ImportError:
        create_delivery_tables = None


COMMENT_REGEX = re.compile(
    r"^\[\s*(?P<pos>[^|\]]+?)\s*\|\s*(?P<dev>[^|\]]+?)\s*\|\s*(?P<term>[^|\]]+?)\s*\]\s*(?P<desc>.*)$"
)
SAFETY_KEYWORDS = ("急停", "热继", "停止", "极限", "限位", "安全门", "光栅", "过载")


def parse_tag_comment(comment_text: str, tag_name: str, logical_address: str = "") -> Dict[str, str]:
    """解析注释中的 [页码.列号 | 元器件代号 | 端子/线号] 中文说明(NO/NC/线圈)"""
    match = COMMENT_REGEX.match(comment_text.strip())
    if match:
        pos = match.group("pos").strip()
        dev = match.group("dev").strip()
        term = match.group("term").strip()
        desc = match.group("desc").strip()
    else:
        parts = tag_name.split("_", 1)
        dev = f"-{parts[0]}" if parts else "-DEV"
        pos = "1.0"
        term = "XT1:1"
        desc = comment_text.strip() or (parts[1] if len(parts) > 1 else tag_name)

    addr_u = (logical_address or "").strip().upper()
    if addr_u.startswith("%Q"):
        if "指示灯" in comment_text or "指示灯" in tag_name:
            contact_type = "指示灯"
        elif "电磁阀" in comment_text or "YV" in tag_name.upper():
            contact_type = "电磁阀"
        elif "变频" in comment_text:
            contact_type = "变频器DI"
        else:
            contact_type = "线圈"
    elif "NC" in comment_text.upper() or "常闭" in comment_text:
        contact_type = "NC"
    else:
        contact_type = "NO"

    return {
        "page_col": pos,
        "device_tag": dev,
        "terminal": term,
        "contact_type": contact_type,
        "description": desc,
    }


def parse_xml_directory(
    xml_dir: str
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    解析指定目录中的 XML 文件，提取：
      - project_info
      - io_list (用于三表打点)
      - all_tags (包含 M 区等所有变量)
      - fc_list (用于调试步骤提取)
    """
    io_list: List[Dict[str, Any]] = []
    all_tags: List[Dict[str, Any]] = []
    fc_list: List[Dict[str, Any]] = []

    xml_files = []
    scan_roots = [xml_dir]
    parent_dir_path = os.path.dirname(os.path.abspath(xml_dir))
    sibling_tag_dir = os.path.join(parent_dir_path, "02_变量表")
    if os.path.isdir(sibling_tag_dir) and os.path.abspath(sibling_tag_dir) != os.path.abspath(xml_dir):
        scan_roots.append(sibling_tag_dir)

    for s_root in scan_roots:
        for root, _, files in os.walk(s_root):
            for f in sorted(files):
                if f.lower().endswith(".xml"):
                    full_p = os.path.join(root, f)
                    if full_p not in xml_files:
                        xml_files.append(full_p)

    plc_model = "西门子 SIMATIC S7-1200 (CPU 1214C DC/DC/DC)"
    base_name = os.path.basename(os.path.abspath(xml_dir))
    project_name = os.path.basename(parent_dir_path) if base_name in ("03_梯形图XML", "02_变量表") else base_name

    for xf in xml_files:
        try:
            with open(xf, "r", encoding="utf-8", errors="replace") as f:
                content = f.read().lstrip("\ufeff")
            root_elem = ET.fromstring(content)
        except ET.ParseError:
            continue

        # 1. 扫描变量表
        table_name_el = root_elem.find(".//{*}SW.Tags.PlcTagTable/{*}AttributeList/{*}Name")
        table_name = (table_name_el.text or "").strip() if table_name_el is not None else os.path.basename(xf)

        for tag_elem in root_elem.findall(".//{*}SW.Tags.PlcTag"):
            name_el = tag_elem.find("{*}AttributeList/{*}Name")
            addr_el = tag_elem.find("{*}AttributeList/{*}LogicalAddress")
            type_el = tag_elem.find("{*}AttributeList/{*}DataTypeName")
            t_name = (name_el.text or "").strip() if name_el is not None else ""
            t_addr = (addr_el.text or "").strip() if addr_el is not None else ""
            t_type = (type_el.text or "").strip() if type_el is not None else "Bool"

            comment_nodes = tag_elem.findall(".//{*}MultilingualTextItem/{*}AttributeList/{*}Text")
            c_text = " ".join([(cn.text or "").strip() for cn in comment_nodes if cn.text])

            if not t_name:
                continue

            parsed_info = parse_tag_comment(c_text, t_name, t_addr)
            is_safety = (
                t_addr.upper().startswith("%I")
                and (parsed_info["contact_type"] == "NC" or any(k in t_name for k in SAFETY_KEYWORDS))
            )
            pending_flag = "⚠待确认" if ("⚠" in c_text or "?" in c_text) else "已确认"

            tag_record = {
                "name": t_name,
                "logical_address": t_addr,
                "data_type": t_type,
                "comment": c_text,
                "device_tag": parsed_info["device_tag"],
                "page_col": parsed_info["page_col"],
                "terminal": parsed_info["terminal"],
                "contact_type": parsed_info["contact_type"],
                "description": parsed_info["description"],
                "table_name": table_name,
                "is_safety": is_safety,
                "confidence": pending_flag,
                "file": os.path.basename(xf)
            }
            if not any(x["name"] == t_name and x["logical_address"] == t_addr for x in all_tags):
                all_tags.append(tag_record)

            if t_addr.upper().startswith("%I") or t_addr.upper().startswith("%Q"):
                if not any(x["name"] == t_name and x["logical_address"] == t_addr for x in io_list):
                    io_list.append(tag_record)

        # 2. 扫描 FC 与 FB 功能块，提取每个程序段的线圈、触点、定时器拓扑（按 UId 精确关联 PT）
        target_blocks = root_elem.findall(".//{*}SW.Blocks.FC") + root_elem.findall(".//{*}SW.Blocks.FB")
        for fc_elem in target_blocks:
            fc_name_el = fc_elem.find("{*}AttributeList/{*}Name")
            fc_name = (fc_name_el.text or "").strip() if fc_name_el is not None else os.path.basename(xf)
            networks = []
            for idx, cu in enumerate(fc_elem.findall(".//{*}SW.Blocks.CompileUnit"), start=1):
                t_node = cu.find(".//{*}MultilingualText[@CompositionName='Title']//{*}AttributeList/{*}Text")
                c_node = cu.find(".//{*}MultilingualText[@CompositionName='Comment']//{*}AttributeList/{*}Text")
                nw_title = (t_node.text or "").strip() if t_node is not None else f"程序段 {idx}"
                nw_comment = (c_node.text or "").strip() if c_node is not None else ""

                coils_in_nw: List[str] = []
                contacts_in_nw: List[str] = []
                timer_by_uid: Dict[str, str] = {}
                timer_pt_by_uid: Dict[str, str] = {}

                flg = cu.find(".//{*}FlgNet")
                if flg is not None:
                    uid_sym: Dict[str, str] = {}
                    part_kind: Dict[str, str] = {}
                    for acc in flg.findall(".//{*}Access"):
                        u = acc.attrib.get("UId", "")
                        comps = [c.attrib.get("Name", "") for c in acc.findall(".//{*}Component") if c.attrib.get("Name")]
                        cval = acc.find(".//{*}ConstantValue")
                        if comps:
                            uid_sym[u] = ".".join(comps)
                        elif cval is not None and cval.text:
                            uid_sym[u] = cval.text.strip()
                    for pt in flg.findall(".//{*}Part"):
                        pu = pt.attrib.get("UId", "")
                        pname = pt.attrib.get("Name", "")
                        part_kind[pu] = pname
                        if pname in ("TON", "TOF", "TP"):
                            inst_comps = [c.attrib.get("Name", "") for c in pt.findall(".//{*}Instance//{*}Component") if c.attrib.get("Name")]
                            timer_by_uid[pu] = ".".join(inst_comps) if inst_comps else pname
                    for wire in flg.findall(".//{*}Wire"):
                        id_con = wire.find("{*}IdentCon")
                        nm_con = wire.find("{*}NameCon")
                        if id_con is not None and nm_con is not None:
                            sym = uid_sym.get(id_con.attrib.get("UId", ""), "")
                            p_uid = nm_con.attrib.get("UId", "")
                            pk = part_kind.get(p_uid, "")
                            pin = nm_con.attrib.get("Name", "")
                            if not sym:
                                continue
                            if pk in ("Coil", "SCoil", "RCoil") and pin == "operand":
                                if sym not in coils_in_nw:
                                    coils_in_nw.append(sym)
                            elif pk == "Contact" and pin == "operand":
                                if sym not in contacts_in_nw:
                                    contacts_in_nw.append(sym)
                            elif pk in ("TON", "TOF", "TP") and pin == "PT":
                                timer_pt_by_uid[p_uid] = sym

                timers_in_nw = [
                    f"{t_name}(PT={timer_pt_by_uid[t_uid]})" if t_uid in timer_pt_by_uid else t_name
                    for t_uid, t_name in timer_by_uid.items()
                ]

                if nw_title or coils_in_nw or contacts_in_nw:
                    networks.append({
                        "index": idx,
                        "title": nw_title,
                        "comment": nw_comment,
                        "coils": coils_in_nw,
                        "contacts": contacts_in_nw,
                        "timers": timers_in_nw,
                    })
            fc_list.append({"name": fc_name, "networks": networks, "file": os.path.basename(xf)})

    def addr_sort_key(item):
        addr = item["logical_address"].upper()
        prefix = 0 if addr.startswith("%I") else (1 if addr.startswith("%Q") else 2)
        num_m = re.findall(r"\d+", addr)
        nums = [int(n) for n in num_m] if num_m else [0]
        return (prefix, nums)

    io_list.sort(key=addr_sort_key)

    pending_tags = [t["name"] for t in all_tags if "⚠" in t.get("confidence", "")]
    pending_text = "、".join(pending_tags) if pending_tags else "无未闭环待确认项"

    project_info = {
        "project_name": project_name,
        "plc_model": plc_model,
        "schematic_ver": "EPLAN 自动反向提取工程",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "hardware_interlock": "已由现场实际接线与反向导出的梯形图互锁逻辑保障",
        "pending_items": pending_text,
    }

    return project_info, io_list, all_tags, fc_list


def export_tag_table_excel(all_tags: List[Dict[str, Any]], out_xlsx: str) -> None:
    """生成符合规范 08 的 8 列《PLC变量表_中文.xlsx》"""
    if not OPENPYXL_AVAILABLE:
        raise RuntimeError("缺少 openpyxl 依赖库，无法生成 Excel 变量表")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PLC变量表"
    ws.views.sheetView[0].showGridLines = True

    font_header = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
    font_normal = Font(name="微软雅黑", size=9)
    fill_header = PatternFill(start_color="004C87", end_color="004C87", fill_type="solid")
    fill_alt = PatternFill(start_color="F2F6F9", end_color="F2F6F9", fill_type="solid")
    border_thin = Side(style="thin", color="D0D0D0")
    border_all = Border(left=border_thin, right=border_thin, top=border_thin, bottom=border_thin)

    headers = ["序号", "变量名", "数据类型", "地址", "注释", "所属表", "是否安全信号", "置信度/待确认"]
    ws.append(headers)
    ws.row_dimensions[1].height = 26

    for c_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c_idx)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border_all

    for idx, t in enumerate(all_tags, start=1):
        is_safe = "是(NC)" if t.get("is_safety") else "否"
        row_data = [
            idx,
            t.get("name", ""),
            t.get("data_type", "Bool"),
            t.get("logical_address", ""),
            t.get("comment", ""),
            t.get("table_name", t.get("file", "")),
            is_safe,
            t.get("confidence", "已确认"),
        ]
        ws.append(row_data)
        r_num = idx + 1
        ws.row_dimensions[r_num].height = 22
        is_even = (r_num % 2 == 0)

        for col_i in range(1, len(row_data) + 1):
            c = ws.cell(row=r_num, column=col_i)
            c.font = font_normal
            c.border = border_all
            if is_even:
                c.fill = fill_alt
            if col_i in (1, 3, 4, 6, 7, 8):
                c.alignment = Alignment(horizontal="center", vertical="center")
            else:
                c.alignment = Alignment(horizontal="left", vertical="center")

    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        max_l = max([len(str(c.value or '').encode('gbk', errors='ignore')) for c in col])
        ws.column_dimensions[col_letter].width = min(max(max_l + 3, 12), 55)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.print_title_rows = "1:1"
    os.makedirs(os.path.dirname(os.path.abspath(out_xlsx)), exist_ok=True)
    wb.save(out_xlsx)


def resolve_project_path(path_str: str) -> str:
    """智能解析项目路径（自动兼容是否带 '项目/' 前缀）"""
    if not path_str or os.path.exists(path_str):
        return path_str
    alt_in_proj = os.path.join("项目", path_str)
    if os.path.exists(alt_in_proj):
        return alt_in_proj
    parent_in_proj = os.path.join("项目", os.path.dirname(path_str))
    if os.path.exists(parent_in_proj):
        return alt_in_proj
    return path_str


def _locate_ps_bridge() -> str:
    """优先从 Skill 内置 scripts/ 目录查找 tia_bridge_core.ps1，兼容回退 tools/"""
    candidates = [
        os.path.join(current_dir, "tia_bridge_core.ps1"),
        os.path.join(root_dir, "tools", "tia_bridge_core.ps1"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return candidates[0]


def reverse_generate(
    xml_dir: str,
    output_dir: str,
    from_tia: bool = False,
    process_index: int = -1,
    project_path: str = "",
    plc_name: str = "",
) -> Dict[str, Any]:
    """反向生成核心函数"""
    if create_delivery_tables is None:
        return {
            "success": False,
            "error": "未找到 delivery_builder.create_delivery_tables 模块，无法生成《05_现场调试交付三表》"
        }

    temp_export_dir = None
    target_scan_dir = resolve_project_path(xml_dir)
    output_dir = resolve_project_path(output_dir)

    tia_project_name = ""
    tia_cpu_model = ""
    try:
        if from_tia:
            # Windows 下 tempfile.mkdtemp() 会剥离父目录继承 ACL，导致管理员权限运行的博途导出的文件不可读；
            # 改用 os.makedirs 在系统临时目录创建继承用户 ACL 的目录。
            temp_export_dir = os.path.join(
                tempfile.gettempdir(), f"tia_reverse_export_{uuid.uuid4().hex[:8]}"
            )
            os.makedirs(temp_export_dir, exist_ok=True)
            ps_script = _locate_ps_bridge()
            if not os.path.isfile(ps_script):
                return {"success": False, "error": f"缺少 Openness 桥接脚本: {ps_script}"}
            cmd = [
                "powershell",
                "-ExecutionPolicy", "Bypass",
                "-File", ps_script,
                "-Action", "export-all",
                "-ProcessIndex", str(process_index),
                "-ExportDir", temp_export_dir
            ]
            if project_path:
                cmd.extend(["-ProjectPath", project_path])
            if plc_name:
                cmd.extend(["-PlcName", plc_name])
            try:
                proc = subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
                stdout_str = (proc.stdout or "").strip()
                j_idx = stdout_str.find("{")
                if j_idx != -1:
                    import json as _json
                    meta = _json.loads(stdout_str[j_idx:])
                    tia_project_name = meta.get("ProjectName", "") or ""
                    tia_cpu_model = meta.get("CpuModel", "") or ""
                target_scan_dir = temp_export_dir
            except subprocess.CalledProcessError as e:
                detail = (e.stdout or e.stderr or str(e)).strip()
                return {"success": False, "error": f"从博途导出项目数据失败: {detail}"}
            except Exception as e:
                return {"success": False, "error": f"从博途导出项目数据失败: {str(e)}"}

        if not os.path.exists(target_scan_dir):
            return {"success": False, "error": f"输入目录不存在: {target_scan_dir}"}

        os.makedirs(output_dir, exist_ok=True)

        project_info, io_list, all_tags, fc_list = parse_xml_directory(target_scan_dir)
        if tia_project_name:
            project_info["project_name"] = tia_project_name
        if tia_cpu_model:
            project_info["plc_model"] = f"西门子 SIMATIC S7-1200 ({tia_cpu_model})"

        out_xlsx = os.path.join(output_dir, "05_现场调试交付三表.xlsx")
        out_md = os.path.join(output_dir, "05_现场调试交付三表.md")
        tags_xlsx = os.path.join(output_dir, "PLC变量表_中文.xlsx")

        create_delivery_tables(project_info, io_list, out_xlsx, out_md, fc_list=fc_list)
        export_tag_table_excel(all_tags, tags_xlsx)

        return {
            "success": True,
            "totalIo": len(io_list),
            "totalTags": len(all_tags),
            "totalBlocks": len(fc_list),
            "deliveryXlsx": out_xlsx,
            "deliveryMd": out_md,
            "tagsXlsx": tags_xlsx
        }
    except Exception as ex:
        return {"success": False, "error": f"生成交付物失败: {str(ex)}"}
    finally:
        if temp_export_dir and os.path.exists(temp_export_dir):
            shutil.rmtree(temp_export_dir, ignore_errors=True)


if __name__ == "__main__":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    parser = argparse.ArgumentParser(description="从博途 XML 目录或运行中博途反向一键生成交付三表")
    parser.add_argument("xml_dir", nargs="?", default="项目/演示项目_20261028/03_梯形图XML", help="待扫描的 XML 或项目目录")
    parser.add_argument("-o", "--out-dir", dest="out_dir", default=None, help="交付物输出目录 (默认输出到 xml_dir/05_反向生成交付)")
    parser.add_argument("--from-tia", action="store_true", help="直接从当前博途进程导出并生成")
    parser.add_argument("--process-index", type=int, default=-1, help="博途进程序号（-1 为单进程自动选择）")
    parser.add_argument("--project-path", default="", help="目标博途项目路径（多实例时推荐）")
    parser.add_argument("--plc-name", default="", help="目标 PLC 名称（多 PLC 时必填）")

    args = parser.parse_args()

    out_d = args.out_dir
    if not out_d:
        out_d = os.path.join(args.xml_dir, "05_反向生成交付")

    print("=== 反向一键生成《现场调试交付三表》工具 ===")
    print(f"源数据路径: {args.xml_dir}")
    print(f"输出目录: {out_d}")

    res = reverse_generate(
        args.xml_dir,
        out_d,
        from_tia=args.from_tia,
        process_index=args.process_index,
        project_path=args.project_path,
        plc_name=args.plc_name,
    )
    if res["success"]:
        print("\n生成成功！")
        print(f"  - 识别物理 I/O 点数: {res['totalIo']}")
        print(f"  - 识别变量总数: {res['totalTags']}")
        print(f"  - 交付三表 Excel: {res['deliveryXlsx']}")
        print(f"  - 交付三表 Markdown: {res['deliveryMd']}")
        print(f"  - 中文变量表 Excel: {res['tagsXlsx']}")
    else:
        print(f"\n生成失败: {res.get('error')}")
        sys.exit(1)
