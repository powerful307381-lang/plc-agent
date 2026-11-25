# -*- coding: utf-8 -*-
"""
模块名称: common_utils
功能说明: 提取 eplan-to-tia-lad 工具链的跨模块共享基础工具，消除重复实现：
  1. 支持的博途版本常量 SUPPORTED_TIA_VERSIONS (V16–V20)
  2. XML 属性/文本安全转义 escape_xml
  3. 西门子 %I/%Q/%M 地址位级跨度解析 parse_address_bit_span
  4. 统一带单 UTF-8 BOM + CRLF 写盘 write_utf8_bom_crlf
  5. 控制台 UTF-8 输出保护 ensure_utf8_stdout
  6. 从 CPU 型号字符串识别西门子控制器家族标签 infer_plc_family_label
"""

import io
import os
import re
import sys
from typing import Any, Optional, Tuple

SUPPORTED_TIA_VERSIONS: Tuple[str, ...] = ("V16", "V17", "V18", "V19", "V20")


def escape_xml(val: Any) -> str:
    """转义 XML 文本与属性中的特殊字符 (&, <, >, \")"""
    return (
        str(val if val is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def parse_address_bit_span(
    logical_addr: str,
    data_type: str = "Bool",
) -> Optional[Tuple[str, int, int]]:
    """
    将西门子逻辑地址解析为 (区域, 起始绝对位偏移, 占用位数)。
    例如:
      %I0.0   -> ('I', 0, 1)
      %MB10   -> ('M', 80, 8)
      %MW30   -> ('M', 240, 16)
      %MD200  -> ('M', 1600, 32)
    """
    addr = (logical_addr or "").strip().upper()
    if not addr:
        return None
    if not addr.startswith("%") and re.match(r"^[IQM](?:[BWD]?\d+(?:\.\d+)?)$", addr):
        addr = "%" + addr

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


def write_utf8_bom_crlf(file_path: str, content: str) -> str:
    """以单个 UTF-8 BOM (0xEF 0xBB 0xBF) + CRLF 换行规范写入文件，返回绝对路径"""
    abs_path = os.path.abspath(file_path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    clean_text = content.lstrip("\ufeff")
    crlf_content = clean_text.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(abs_path, "wb") as f:
        f.write(b"\xef\xbb\xbf")
        f.write(crlf_content.encode("utf-8"))
    return abs_path


def ensure_utf8_stdout() -> None:
    """确保 Windows 控制台或管道下标准输出采用 UTF-8 编码"""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            enc = (getattr(sys.stdout, "encoding", "") or "").lower()
            if enc != "utf-8":
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            return
    except Exception:
        pass

    if hasattr(sys.stdout, "buffer"):
        enc = (getattr(sys.stdout, "encoding", "") or "").lower()
        if enc != "utf-8":
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer,
                encoding="utf-8",
                errors="replace",
            )


def infer_plc_family_label(cpu_model: str) -> str:
    """根据 CPU 型号/订货号字符串识别西门子控制器家族展示前缀"""
    txt = (cpu_model or "").upper()
    compact = re.sub(r"\s+", "", txt)
    if "ET200SP" in compact or "6ES7510" in compact or "6ES7512" in compact or "1510" in compact or "1512" in compact:
        return "SIMATIC ET 200SP"
    if "S7-1500" in txt or "6ES751" in compact or "CPU151" in compact or re.search(r"\b151\d", compact):
        return "SIMATIC S7-1500"
    if "S7-1200" in txt or "6ES721" in compact or "CPU121" in compact or re.search(r"\b121\d", compact):
        return "SIMATIC S7-1200"
    return "SIMATIC PLC"
