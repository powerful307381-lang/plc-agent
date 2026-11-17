# -*- coding: utf-8 -*-
"""
模块名称: tag_builder
功能说明: 负责将确认后的点表数据校验并生成：
         1. 符合规范 08 的 8 列全中文 Excel (PLC变量表_中文.xlsx)
         2. 符合博途 SimaticML 规范的变量表 XML (TagTable_IO变量表.xml / TagTable_M辅助继电器表.xml)
符合规范: eplan-to-tia-lad SKILL.md 阶段 2, references/02, references/04, references/08
"""

import os
import re
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from typing import List, Dict, Any, Tuple, Optional


# 正则规则
RE_IQ_NAME = re.compile(r'^[A-Z]{1,3}\d{1,3}[A-Z]?_[\u4e00-\u9fa5A-Za-z0-9_]+$')
RE_M_NAME = re.compile(r'^(?:M(?:B|W|D)?\d*|ALM|ERR|ST|STEP|CNT|HMI)_[\u4e00-\u9fa5A-Za-z0-9_]+$')
RE_INVALID_CHARS = re.compile(r'[ \./\\\'"%:;,\(\)\[\]\{\}&#-]')
RE_BIT_ADDR = re.compile(r'^%?[IQM]\d+\.[0-7]$', re.IGNORECASE)
RE_WORD_ADDR = re.compile(r'^%?[IQM][BWD]\d+$', re.IGNORECASE)
RE_EPLAN_COMMENT = re.compile(r'^\[\s*[^|\]]+\s*\|\s*[^|\]]+\s*\|\s*[^|\]]+\s*\]')
SAFETY_KEYWORDS = ("急停", "热继", "停止", "极限", "限位", "安全门", "光栅", "过载")


def _parse_addr_bit_span(logical_addr: str, data_type: str = "Bool") -> Optional[Tuple[str, int, int]]:
    """将西门子逻辑地址解析为 (区域, 起始绝对位偏移, 占用位数)"""
    addr = (logical_addr or "").strip().upper()
    bit_m = re.match(r'^%([IQM])(\d+)\.([0-7])$', addr)
    if bit_m:
        return bit_m.group(1), int(bit_m.group(2)) * 8 + int(bit_m.group(3)), 1
    width_m = re.match(r'^%([IQM])([BWD])(\d+)$', addr)
    if width_m:
        width_bits = {"B": 8, "W": 16, "D": 32}[width_m.group(2)]
        return width_m.group(1), int(width_m.group(3)) * 8, width_bits
    plain_m = re.match(r'^%([IQM])(\d+)$', addr)
    if plain_m:
        dt = (data_type or "Byte").strip().lower()
        if dt in ("word", "int", "uint", "s5time"):
            w = 16
        elif dt in ("dword", "dint", "udint", "real", "time", "tod"):
            w = 32
        else:
            w = 8
        return plain_m.group(1), int(plain_m.group(2)) * 8, w
    return None


def _escape_xml(val: Any) -> str:
    return (
        str(val)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def validate_tag_item(tag: Dict[str, Any]) -> List[str]:
    """单条变量校验规则（区分 I/Q 三段式注释与 M 变量注释）"""
    errors = []
    name = str(tag.get('name', '')).strip()
    raw_addr = str(tag.get('logical_address', '')).strip().upper()
    addr = raw_addr if raw_addr.startswith('%') else ('%' + raw_addr if raw_addr else '')
    data_type = str(tag.get('data_type', 'Bool')).strip()
    comment = str(tag.get('comment', '')).strip()
    is_spare = ("备用" in name) or ("备用" in comment)

    # 1. 名称校验
    if not name:
        errors.append("变量名称不能为空")
    else:
        if len(name) > 40:
            errors.append(f"变量名 '{name}' 长度超过 40 个字符限制")
        if RE_INVALID_CHARS.search(name):
            errors.append(f"变量名 '{name}' 包含非法特殊字符")
        if not is_spare:
            if addr.startswith('%M'):
                if not RE_M_NAME.match(name):
                    errors.append(f"M 变量 '{name}' 不符合规范 'M_中文功能'")
            elif addr.startswith('%I') or addr.startswith('%Q'):
                if not RE_IQ_NAME.match(name):
                    errors.append(f"I/Q 变量 '{name}' 不符合规范 '元器件代号_中文功能'")

    # 2. 地址格式与分区校验
    if not addr:
        errors.append(f"变量 '{name}' 缺少逻辑地址")
    elif not (RE_BIT_ADDR.match(addr) or RE_WORD_ADDR.match(addr) or _parse_addr_bit_span(addr, data_type)):
        errors.append(f"变量 '{name}' 地址格式非法: '{raw_addr}'")

    if addr.startswith('%M'):
        m_match = re.search(r'^%M(?:B|W|D)?(\d+)', addr)
        if m_match and int(m_match.group(1)) < 10:
            errors.append(f"M 变量地址 '{addr}' 侵占了保留区 MB0~MB9，M 区必须从 MB10 开始使用")

    # 3. 注释规范校验（I/Q 必须三段式，M 变量允许 [FC名/程序段号] 功能说明）
    if not comment:
        errors.append(f"变量 '{name}' 缺少中文注释")
    elif not is_spare:
        if addr.startswith('%I') or addr.startswith('%Q'):
            if not RE_EPLAN_COMMENT.match(comment):
                errors.append(f"I/Q 变量 '{name}' 注释未遵循 '[页码.列号 | 元器件代号 | 端子号/线号]' 格式: {comment}")
        elif not (comment.startswith('[') and ']' in comment):
            errors.append(f"M 变量 '{name}' 注释未遵循 '[FC名/程序段号] 功能说明' 格式: {comment}")

    return errors


def _write_tag_table_xml(table_name: str, items: List[Dict[str, Any]], out_xml_path: str) -> None:
    """将单个变量表写入 SimaticML XML（UTF-8 带 BOM）"""
    xml_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Document>',
        '  <Engineering version="V16" />',
        '  <SW.Tags.PlcTagTable ID="0">',
        '    <AttributeList>',
        f'      <Name>{_escape_xml(table_name)}</Name>',
        '    </AttributeList>',
        '    <ObjectList>'
    ]

    id_seq = 1
    for item in items:
        name = _escape_xml(item.get('name', '').strip())
        datatype = _escape_xml(item.get('data_type', 'Bool'))
        addr = _escape_xml(item.get('logical_address', ''))
        comment = _escape_xml(item.get('comment', ''))

        tag_id = hex(id_seq)[2:]
        ml_id = hex(id_seq + 1)[2:]
        mli_id = hex(id_seq + 2)[2:]
        id_seq += 3

        xml_lines.append(f'      <SW.Tags.PlcTag ID="{tag_id}" CompositionName="Tags">')
        xml_lines.append('        <AttributeList>')
        xml_lines.append(f'          <DataTypeName>{datatype}</DataTypeName>')
        xml_lines.append(f'          <LogicalAddress>{addr}</LogicalAddress>')
        xml_lines.append(f'          <Name>{name}</Name>')
        xml_lines.append('        </AttributeList>')
        xml_lines.append('        <ObjectList>')
        xml_lines.append(f'          <MultilingualText ID="{ml_id}" CompositionName="Comment">')
        xml_lines.append('            <ObjectList>')
        xml_lines.append(f'              <MultilingualTextItem ID="{mli_id}" CompositionName="Items">')
        xml_lines.append('                <AttributeList>')
        xml_lines.append('                  <Culture>zh-CN</Culture>')
        xml_lines.append(f'                  <Text>{comment}</Text>')
        xml_lines.append('                </AttributeList>')
        xml_lines.append('              </MultilingualTextItem>')
        xml_lines.append('            </ObjectList>')
        xml_lines.append('          </MultilingualText>')
        xml_lines.append('        </ObjectList>')
        xml_lines.append('      </SW.Tags.PlcTag>')

    xml_lines.append('    </ObjectList>')
    xml_lines.append('  </SW.Tags.PlcTagTable>')
    xml_lines.append('</Document>')

    os.makedirs(os.path.dirname(os.path.abspath(out_xml_path)), exist_ok=True)
    with open(out_xml_path, 'wb') as f:
        f.write(b'\xef\xbb\xbf')
        f.write('\r\n'.join(xml_lines).encode('utf-8'))


def build_tags(
    tag_list: List[Dict[str, Any]],
    excel_path: str,
    xml_path: str,
    table_name: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """
    核心构建函数：接收点表列表，执行命名/分区/位级重叠校验，并输出：
      1. 符合规范 08 的中文 8 列 Excel (PLC变量表_中文.xlsx)
      2. 按所属表名分别输出的 SimaticML XML (若存在多个表，主表写入 xml_path，其余表同目录输出 TagTable_<表名>.xml)
    """
    all_errors = []
    names_seen = set()
    addresses_seen: Dict[str, str] = {}
    bit_occupancy: Dict[Tuple[str, int], Tuple[str, str]] = {}

    # 全局重名、地址冲突与位级重叠校验
    for item in tag_list:
        errs = validate_tag_item(item)
        all_errors.extend(errs)

        name = str(item.get('name', '')).strip()
        addr = str(item.get('logical_address', '')).strip().upper()
        if addr and not addr.startswith('%'):
            addr = '%' + addr
        item['logical_address'] = addr
        data_type = str(item.get('data_type', 'Bool')).strip()

        if name in names_seen:
            all_errors.append(f"存在重复变量名称: '{name}'")
        names_seen.add(name)

        if addr:
            if addr in addresses_seen and addresses_seen[addr] != name:
                all_errors.append(f"存在冲突的逻辑地址: '{addr}' 同时被 '{addresses_seen[addr]}' 和 '{name}' 占用")
            addresses_seen[addr] = name

            span = _parse_addr_bit_span(addr, data_type)
            if span is not None:
                area, start_bit, bit_len = span
                reported = set()
                for b_off in range(start_bit, start_bit + bit_len):
                    key = (area, b_off)
                    if key in bit_occupancy:
                        occ_name, occ_addr = bit_occupancy[key]
                        if occ_name != name and occ_addr != addr and (occ_name, occ_addr) not in reported:
                            reported.add((occ_name, occ_addr))
                            all_errors.append(
                                f"地址内存重叠冲突: '{name}' ({addr}, {data_type}) 与 '{occ_name}' ({occ_addr}) 发生字节/位区域重叠"
                            )
                    else:
                        bit_occupancy[key] = (name, addr)

    if all_errors:
        return False, all_errors

    # 1. 生成符合规范 08 的全中文 8 列 Excel
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PLC变量表"
    ws.views.sheetView[0].showGridLines = True

    headers = [
        "序号", "变量名", "数据类型", "地址",
        "注释", "所属表", "是否安全信号", "置信度/待确认"
    ]
    ws.append(headers)

    font_header = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
    fill_header = PatternFill(start_color="004C87", end_color="004C87", fill_type="solid")
    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    border_thin = Side(style="thin", color="D0D0D0")
    border_all = Border(left=border_thin, right=border_thin, top=border_thin, bottom=border_thin)

    for col_idx in range(1, 9):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_all
    ws.row_dimensions[1].height = 26

    fill_alt = PatternFill(start_color="F2F6F9", end_color="F2F6F9", fill_type="solid")

    grouped_tables: Dict[str, List[Dict[str, Any]]] = {}

    for row_idx, item in enumerate(tag_list, start=2):
        seq = row_idx - 1
        name = str(item.get('name', '')).strip()
        datatype = str(item.get('data_type', 'Bool')).strip()
        addr = item.get('logical_address', '')
        comment = str(item.get('comment', '')).strip()
        default_tbl = 'M辅助继电器表' if addr.startswith('%M') else 'IO变量表'
        tbl = table_name or str(item.get('table_name') or default_tbl).strip()

        grouped_tables.setdefault(tbl, []).append(item)

        is_safe_flag = item.get('is_safety')
        if is_safe_flag is None:
            is_safe_flag = addr.startswith('%I') and (
                "NC" in comment.upper() or "常闭" in comment or any(k in name for k in SAFETY_KEYWORDS)
            )
        is_safe_str = "是(NC)" if is_safe_flag else "否"
        confidence = str(item.get('confidence') or ("⚠待确认" if ("⚠" in comment or "?" in comment) else "已确认"))

        row_values = [seq, name, datatype, addr, comment, tbl, is_safe_str, confidence]
        ws.append(row_values)
        ws.row_dimensions[row_idx].height = 22

        is_even = (row_idx % 2 == 0)
        for col_idx in range(1, 9):
            c = ws.cell(row=row_idx, column=col_idx)
            c.font = Font(name="微软雅黑", size=9)
            c.border = border_all
            if is_even:
                c.fill = fill_alt
            if col_idx in (1, 3, 4, 6, 7, 8):
                c.alignment = align_center
            else:
                c.alignment = align_left

    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for c in col:
            val = str(c.value or '')
            line_len = max([len(line.encode('gbk', errors='ignore')) for line in val.split('\n')]) if val else 0
            if line_len > max_len:
                max_len = line_len
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 55)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.print_title_rows = "1:1"

    os.makedirs(os.path.dirname(os.path.abspath(excel_path)), exist_ok=True)
    wb.save(excel_path)

    # 2. 按表名分组输出 SimaticML XML（主表写入 xml_path，避免同名覆盖）
    if not grouped_tables:
        _write_tag_table_xml(table_name or "IO变量表", [], xml_path)
    else:
        primary_tbl = "IO变量表" if "IO变量表" in grouped_tables else next(iter(grouped_tables))
        xml_dir = os.path.dirname(os.path.abspath(xml_path))
        for tbl_name, items in grouped_tables.items():
            out_p = xml_path if tbl_name == primary_tbl else os.path.join(xml_dir, f"TagTable_{tbl_name}.xml")
            _write_tag_table_xml(tbl_name, items, out_p)

    return True, []
