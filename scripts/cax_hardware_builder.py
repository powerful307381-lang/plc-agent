# -*- coding: utf-8 -*-
"""
模块名称: cax_hardware_builder
功能说明: EPLAN AutomationML (.aml) 硬件拓扑预检、CAx 排除设备清单硬拦截、
         TO_Params.xlsx 轴控工艺参数模板生成，以及调用博途 Openness CaxProvider.Import() 导入。
符合规范: references/09_CAx_AML_Hardware_Rules.md
"""

import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

try:
    from tia_connect import run_ps_bridge
except ImportError:
    from scripts.tia_connect import run_ps_bridge  # type: ignore


# 西门子 Openness CAx 官方明确排除的设备清单
CAX_EXCLUDED_SCALANCE_MODELS = (
    "6GK5 602-0BA10-2AA3",
    "6GK5 612-0BA10-2AA3",
    "6GK5 623-0BA10-2AA3",
)
_CAX_EXCLUDED_SCALANCE_COMPACT = {
    re.sub(r"\s+", "", m).upper(): m for m in CAX_EXCLUDED_SCALANCE_MODELS
}

CAX_EXCLUDED_DRIVE_KEYWORDS = (
    "SINAMICS",
    "G120",
    "S120",
    "V90",
    "G110",
    "MICROMASTER",
    "6SL3",
)

CAX_EXCLUDED_HMI_KEYWORDS = (
    "TP700",
    "TP900",
    "TP1200",
    "TP1500",
    "KTP",
    "MTP",
    "COMFORT PANEL",
    "BASIC PANEL",
    "UNIFIED PANEL",
)

_HMI_KEY_PANEL_ALLOW_KEYWORDS = ("KP8", "KP32", "KEY PANEL", "KEYPANEL")


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1]


def _extract_aml_attributes(elem: ET.Element) -> Dict[str, str]:
    """提取 CAEX InternalElement 节点下的 Attribute 键值对"""
    attrs: Dict[str, str] = {}
    for child in elem:
        if _local_tag(child.tag) == "Attribute":
            attr_name = (child.attrib.get("Name") or "").strip()
            val_el = next((c for c in child if _local_tag(c.tag) == "Value"), None)
            attr_val = (val_el.text or "").strip() if val_el is not None else ""
            if attr_name:
                attrs[attr_name] = attr_val
                # 嵌套子属性平铺收集（如 TypeIdentifier/OrderNumber）
                for sub in child.iter():
                    if _local_tag(sub.tag) == "Attribute":
                        sub_name = (sub.attrib.get("Name") or "").strip()
                        sub_val_el = next((c for c in sub if _local_tag(c.tag) == "Value"), None)
                        if sub_name and sub_val_el is not None and (sub_val_el.text or "").strip():
                            attrs.setdefault(sub_name, sub_val_el.text.strip())
    return attrs


def _classify_cpu_family(combined_text: str) -> Optional[str]:
    txt = combined_text.upper()
    compact = re.sub(r"\s+", "", txt)
    if "6ES721" in compact or "S7-1200" in txt or "CPU121" in compact:
        return "S7-1200"
    if "ET200SP" in compact or "6ES7510" in compact or "6ES7512" in compact:
        return "ET 200SP"
    if "6ES751" in compact or "S7-1500" in txt or "CPU151" in compact:
        return "S7-1500"
    return None


def _is_failsafe_signature(combined_text: str) -> bool:
    txt = combined_text.upper()
    compact = re.sub(r"\s+", "", txt)
    if any(k in txt for k in ("FAILSAFE", "FAIL-SAFE", "PROFISAFE", "F-CPU")):
        return True
    return bool(re.search(r"1[25]1\dFC?|6ES7[25]1\d-\dF", compact))


def _check_excluded_device(name: str, order_no: str, type_id: str, role: str) -> Optional[Dict[str, str]]:
    """检查单个设备是否命中 Openness CAx 官方排除清单"""
    joined = f"{name} {order_no} {type_id} {role}".upper()
    compact_joined = re.sub(r"\s+", "", joined)

    # 1. 特定 Scalance 交换机型号拦截
    for compact_model, display_model in _CAX_EXCLUDED_SCALANCE_COMPACT.items():
        if compact_model in compact_joined:
            return {
                "device_name": name or display_model,
                "order_number": order_no or display_model,
                "category": "ScalanceSwitch",
                "reason": f"西门子 Openness CAx 官方不支持特定 Scalance 交换机型号 '{display_model}' 的自动化导入，需在博途硬件目录中手动组态或维护网络拓扑清单。",
            }

    # 2. 变频器 / 伺服驱动器拦截
    if any(kw in joined or kw in compact_joined for kw in CAX_EXCLUDED_DRIVE_KEYWORDS):
        return {
            "device_name": name or order_no,
            "order_number": order_no or type_id,
            "category": "Drive",
            "reason": "Openness CAx (AML) 不支持变频器与伺服驱动器（SINAMICS G120/S120/V90 等），请通过 TO_Params.xlsx 与 Openness technology_objects 接口配置。",
        }

    # 3. 非按键面板 HMI 设备拦截（按键面板 KP8/KP32F 例外放行）
    if not any(ak in joined for ak in _HMI_KEY_PANEL_ALLOW_KEYWORDS):
        if any(hk in joined for hk in CAX_EXCLUDED_HMI_KEYWORDS):
            return {
                "device_name": name or order_no,
                "order_number": order_no or type_id,
                "category": "HMI",
                "reason": "Openness CAx 不支持非按键面板 HMI（Comfort/Basic/Unified 触摸屏）自动化导入，需使用独立 HMI 组态通道与 webapp-testing 联调。",
            }

    return None


def inspect_aml_hardware(aml_path: str) -> Dict[str, Any]:
    """
    解析 EPLAN 导出的 .aml 文件，提取机架/模块/CPU 家族，并执行 CAx 排除设备清单预检。
    """
    if not os.path.exists(aml_path):
        raise FileNotFoundError(f"未找到 AML 硬件拓扑文件: {aml_path}")

    with open(aml_path, "r", encoding="utf-8-sig", errors="replace") as f:
        xml_text = f.read()

    root = ET.fromstring(xml_text)

    stations: List[str] = []
    modules: List[Dict[str, Any]] = []
    unsupported_devices: List[Dict[str, str]] = []
    seen_unsupported = set()

    cpu_family = "Unknown"
    is_failsafe_cpu = False

    for elem in root.iter():
        if _local_tag(elem.tag) != "InternalElement":
            continue

        name = (elem.attrib.get("Name") or "").strip()
        attrs = _extract_aml_attributes(elem)
        role_el = next((c for c in elem if _local_tag(c.tag) == "RoleRequirements"), None)
        role_name = (role_el.attrib.get("RefBaseRoleClassPath") or "").strip() if role_el is not None else ""

        order_no = (
            attrs.get("OrderNumber")
            or attrs.get("ArticleNumber")
            or attrs.get("TypeIdentifier")
            or attrs.get("PartNumber")
            or ""
        )
        type_id = attrs.get("TypeIdentifier") or attrs.get("DeviceType") or ""
        slot = attrs.get("PositionNumber") or attrs.get("Slot") or ""
        start_addr = attrs.get("StartAddress") or ""

        combined = f"{name} {order_no} {type_id} {role_name}"
        detected_family = _classify_cpu_family(combined)
        if detected_family and cpu_family == "Unknown":
            cpu_family = detected_family
        if _is_failsafe_signature(combined):
            is_failsafe_cpu = True

        if "station" in role_name.lower() or "plc" in role_name.lower() or detected_family:
            if name and name not in stations:
                stations.append(name)

        if order_no or type_id or slot:
            modules.append({
                "name": name,
                "order_number": order_no,
                "type_identifier": type_id,
                "slot": slot,
                "start_address": start_addr,
                "role": role_name,
            })

        excluded = _check_excluded_device(name, order_no, type_id, role_name)
        if excluded is not None:
            sig = (excluded["device_name"], excluded["order_number"], excluded["category"])
            if sig not in seen_unsupported:
                seen_unsupported.add(sig)
                unsupported_devices.append(excluded)

    to_hints: List[str] = []
    has_drives = any(d["category"] == "Drive" for d in unsupported_devices)
    if has_drives:
        to_hints.append(
            "检测到伺服/变频驱动器，请使用 build_to_params_template() 生成 TO_Params.xlsx 并通过 Openness technology_objects 接口创建工艺对象。"
        )
    if any(d["category"] == "ScalanceSwitch" for d in unsupported_devices):
        to_hints.append(
            "检测到 CAx 排除型号的 Scalance 交换机（6GK5 602/612/623-0BA10-2AA3），请在导入前从 AML 过滤或在博途网络视图手动组态。"
        )
    if any(d["category"] == "HMI" for d in unsupported_devices):
        to_hints.append(
            "检测到非按键面板 HMI 设备，请在博途内独立配置 HMI 连接，联调阶段配合 webapp-testing 进行画面测试。"
        )
    if is_failsafe_cpu:
        to_hints.append(
            "检测到故障安全 F-PLC：首次包含安全程序的完整下载必须在 TIA Portal GUI 手动执行。"
        )

    return {
        "valid": len(unsupported_devices) == 0,
        "aml_path": os.path.abspath(aml_path),
        "cpu_family": cpu_family,
        "is_failsafe_cpu": is_failsafe_cpu,
        "stations": stations,
        "modules": modules,
        "unsupported_devices": unsupported_devices,
        "to_migration_hints": to_hints,
    }


def build_to_params_template(unsupported_devices: List[Dict[str, Any]], out_xlsx: str) -> str:
    """
    针对被 CAx 预检拦截的驱动器/伺服设备，自动生成标准《TO_Params.xlsx》工艺参数引导模板。
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "TO_轴控与驱动参数"

    font_header = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
    fill_header = PatternFill(start_color="004C87", end_color="004C87", fill_type="solid")
    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    border_thin = Side(style="thin", color="D0D0D0")
    border_all = Border(left=border_thin, right=border_thin, top=border_thin, bottom=border_thin)

    headers = [
        "轴/驱动名称",
        "关联设备订货号",
        "类别",
        "工艺对象类型(TO)",
        "PROFINET报文",
        "机械螺距/导程(mm)",
        "最大速度(mm/s)",
        "加减速(mm/s2)",
        "正负硬限位地址",
        "Openness配置状态",
    ]
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        c = ws.cell(row=1, column=col_idx)
        c.font = font_header
        c.fill = fill_header
        c.alignment = align_center
        c.border = border_all

    drives = [d for d in unsupported_devices if d.get("category") == "Drive"]
    if not drives:
        drives = [{"device_name": "Axis_示例伺服轴", "order_number": "6SL3210-5FE10-4UF0", "category": "Drive"}]

    for drv in drives:
        ws.append([
            drv.get("device_name", ""),
            drv.get("order_number", ""),
            drv.get("category", "Drive"),
            "TO_PositioningAxis",
            "Standard Telegram 3",
            10.0,
            500.0,
            2000.0,
            "待填写(%I限位)",
            "待通过 technology_objects 接口配置",
        ])

    ws_guide = wb.create_sheet("CAx排除设备清单")
    ws_guide.append(["设备名称", "订货号/标识", "排除类别", "拦截原因与处理指引"])
    for d in unsupported_devices:
        ws_guide.append([
            d.get("device_name", ""),
            d.get("order_number", ""),
            d.get("category", ""),
            d.get("reason", ""),
        ])

    for sheet in (ws, ws_guide):
        for col in sheet.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            sheet.column_dimensions[get_column_letter(col[0].column)].width = min(max(max_len + 6, 16), 48)

    out_dir = os.path.dirname(os.path.abspath(out_xlsx))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    wb.save(out_xlsx)
    return os.path.abspath(out_xlsx)


def import_aml_to_tia(
    aml_path: str,
    process_index: int = 0,
    import_option: str = "RetainTiaDevice",
    allow_filtered: bool = False,
    to_params_out_xlsx: Optional[str] = None,
) -> Dict[str, Any]:
    """
    预检 .aml 硬件拓扑，拦截不支持设备并按需生成 TO_Params.xlsx；
    预检通过或显式 allow_filtered=True 时，调用 tia_bridge_core.ps1 -Action import-cax 执行导入。
    """
    inspect_res = inspect_aml_hardware(aml_path)
    to_xlsx_path = ""
    if not inspect_res["valid"]:
        has_drives = any(d["category"] == "Drive" for d in inspect_res["unsupported_devices"])
        if has_drives:
            target_xlsx = to_params_out_xlsx or os.path.join(
                os.path.dirname(os.path.abspath(aml_path)), "TO_Params.xlsx"
            )
            to_xlsx_path = build_to_params_template(inspect_res["unsupported_devices"], target_xlsx)
        if not allow_filtered:
            return {
                "Success": False,
                "BlockedByCAxPrecheck": True,
                "InspectResult": inspect_res,
                "ToParamsPath": to_xlsx_path,
                "ActionableHint": "CAx 预检发现不支持自动导入的设备，请先处理排除清单或补全 TO_Params.xlsx。",
            }

    res = run_ps_bridge(
        "import-cax",
        process_index=process_index,
        AmlPath=os.path.abspath(aml_path),
        ImportOption=import_option,
    )
    return {
        "Success": bool(res.get("Success")),
        "BlockedByCAxPrecheck": False,
        "InspectResult": inspect_res,
        "ToParamsPath": to_xlsx_path,
        "TransferResult": res,
    }


def _to_float_pos(raw_val: Any, field_name: str, row_idx: int) -> float:
    try:
        v = float(raw_val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"TO_Params.xlsx 第 {row_idx} 行 '{field_name}' 不是合法数值: {raw_val!r}") from exc
    if v <= 0:
        raise ValueError(f"TO_Params.xlsx 第 {row_idx} 行 '{field_name}' 必须大于 0，当前值: {v}")
    return v


def parse_to_params_xlsx(xlsx_path: str) -> List[Dict[str, Any]]:
    """
    解析《TO_Params.xlsx》的 'TO_轴控与驱动参数' 工作表并校验机械/运动学参数合法性。
    """
    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(f"未找到工艺参数文件: {xlsx_path}")

    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["TO_轴控与驱动参数"] if "TO_轴控与驱动参数" in wb.sheetnames else wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True) if any(str(c or "").strip() for c in r)]
    if len(rows) < 2:
        return []

    headers = [str(h or "").strip() for h in rows[0]]
    axes: List[Dict[str, Any]] = []
    for row_num, row in enumerate(rows[1:], start=2):
        rmap = {headers[i]: row[i] if i < len(row) else None for i in range(len(headers))}
        axis_name = str(rmap.get("轴/驱动名称") or rmap.get("axis_name") or "").strip()
        if not axis_name:
            continue
        order_number = str(rmap.get("关联设备订货号") or rmap.get("order_number") or "").strip()
        category = str(rmap.get("类别") or rmap.get("category") or "Drive").strip()
        to_type = str(rmap.get("工艺对象类型(TO)") or rmap.get("to_type") or "TO_PositioningAxis").strip()
        telegram = str(rmap.get("PROFINET报文") or rmap.get("telegram") or "Standard Telegram 3").strip()
        lead_pitch = _to_float_pos(rmap.get("机械螺距/导程(mm)") or rmap.get("lead_pitch_mm"), "机械螺距/导程(mm)", row_num)
        max_vel = _to_float_pos(rmap.get("最大速度(mm/s)") or rmap.get("max_velocity_mms"), "最大速度(mm/s)", row_num)
        accel = _to_float_pos(rmap.get("加减速(mm/s2)") or rmap.get("acceleration_mms2"), "加减速(mm/s2)", row_num)
        hw_limit = str(rmap.get("正负硬限位地址") or rmap.get("hw_limit_addr") or "").strip()
        status = str(rmap.get("Openness配置状态") or "").strip()

        axes.append({
            "axis_name": axis_name,
            "order_number": order_number,
            "category": category,
            "to_type": to_type,
            "telegram": telegram,
            "lead_pitch_mm": lead_pitch,
            "max_velocity_mms": max_vel,
            "acceleration_mms2": accel,
            "hw_limit_addr": hw_limit,
            "status": status,
        })
    return axes


def configure_to_in_tia(
    xlsx_path: str,
    process_index: int = 0,
    plc_name: str = "",
) -> Dict[str, Any]:
    """
    解析校验 TO_Params.xlsx 并调用 tia_bridge_core.ps1 -Action configure-to
    将轴控工艺对象写入博途 TechnologicalObjectGroup。
    """
    axes = parse_to_params_xlsx(xlsx_path)
    if not axes:
        return {
            "Success": False,
            "ConfiguredAxes": [],
            "Errors": ["TO_Params.xlsx 中未解析到任何有效轴控配置行"],
            "ActionableHint": "请先在 TO_轴控与驱动参数 Sheet 中填写轴名称与机械参数后重试。",
        }

    res = run_ps_bridge(
        "configure-to",
        process_index=process_index,
        PlcName=plc_name,
        ToParamsPath=os.path.abspath(xlsx_path),
    )
    return {
        "Success": bool(res.get("Success")),
        "AxesParsed": axes,
        "ConfiguredAxes": res.get("ConfiguredAxes", []),
        "Warnings": res.get("Warnings", []),
        "Errors": res.get("Errors", []) or ([res["error"]] if res.get("error") else []),
        "BridgeResult": res,
    }
