# -*- coding: utf-8 -*-
"""
模块名称: tia_connect
功能说明:
  1. 兼容原有离线/脚本调用接口 (run_ps_bridge, list_processes, check_environment, compare_eplan_with_tia_hw);
  2. 按 mcp-builder 规范封装为标准 Python FastMCP Server，提供 8 个开放接口与 Pydantic 输入/输出 Schema;
  3. 落实 OT 安全加固（严禁 shell=True、防命令注入/路径穿越白名单校验、敏感 Token 脱敏与高危下载/强制门禁）。
"""

import io
import json
import os
import re
import socket
import subprocess
import sys
from typing import Any, Dict, List, Optional

try:
    from common_utils import ensure_utf8_stdout, parse_address_bit_span
except ImportError:
    from scripts.common_utils import ensure_utf8_stdout, parse_address_bit_span  # type: ignore

try:
    from pydantic import BaseModel, Field
    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False

    class BaseModel:  # type: ignore
        """无 pydantic 环境下的轻量降级兼容基类"""
        def __init__(self, **data: Any) -> None:
            for k, v in data.items():
                setattr(self, k, v)

        def model_dump(self) -> Dict[str, Any]:
            return dict(self.__dict__)

    def Field(default: Any = None, **kwargs: Any) -> Any:  # type: ignore
        return default

try:
    from fastmcp import FastMCP  # type: ignore
    FASTMCP_AVAILABLE = True
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore
        FASTMCP_AVAILABLE = True
    except ImportError:
        FASTMCP_AVAILABLE = False

        class FastMCP:  # type: ignore
            """无 mcp/fastmcp 依赖时的轻量级本地 Shim，保障模块可直接导入与单测执行"""
            def __init__(self, name: str, instructions: str = "") -> None:
                self.name = name
                self.instructions = instructions
                self.tools: Dict[str, Any] = {}

            def tool(self, *args: Any, **kwargs: Any):
                def _decorator(fn: Any):
                    self.tools[fn.__name__] = fn
                    return fn
                return _decorator

            def run(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("当前 Python 环境未安装 fastmcp 或 mcp，无法启动 stdio MCP 服务；请运行 `pip install fastmcp mcp pydantic`。")


# =============================================================================
# OT 安全加固：输入防注入、路径穿越校验、敏感信息脱敏与可行动报错转译
# =============================================================================
_DANGEROUS_SHELL_RE = re.compile(r"[;&|`$><\r\n\x00]")
_ALLOWED_NAME_RE = re.compile(r"^[\w\-. \u4e00-\u9fa5:/\\]+$")
_SENSITIVE_KEY_RE = re.compile(r"(token|password|secret|credential|api_key|master_secret)", re.IGNORECASE)
_ALLOWED_CAX_OPTIONS = {"RetainTiaDevice", "OverwriteTiaDevice", "MoveToParkingLot"}
_ALLOWED_DOWNLOAD_MODES = {"Hardware", "Software", "SoftwareOnlyChanges"}
_ALLOWED_EPLAN_ACTIONS = {"inspect-eplan", "export-all", "export-aml", "export-tables", "export-pdf"}
_ALLOWED_SCHEME_RE = re.compile(r"^[A-Za-z0-9_\-.\u4e00-\u9fa5 ]{1,128}$")


def _validate_safe_arg(key: str, val: Any) -> str:
    """校验非路径参数字符串，拦截空字节、换行及 PowerShell/Shell 命令注入元字符"""
    s = str(val if val is not None else "")
    if not s:
        return ""
    if _DANGEROUS_SHELL_RE.search(s):
        raise ValueError(f"安全拦截: 参数 '{key}' 包含非法控制字符或命令注入元字符")
    if len(s) > 4096:
        raise ValueError(f"安全拦截: 参数 '{key}' 长度超过 4096 字符上限")
    return s


def _validate_safe_path(path_str: str, param_name: str = "path") -> str:
    """校验工程/XML/AML 路径合法性，禁止空字节、注入字符及 '..' 目录穿越"""
    s = _validate_safe_arg(param_name, path_str).strip()
    if not s:
        return ""
    parts = re.split(r"[\\/]+", s)
    if ".." in parts:
        raise ValueError(f"安全拦截: 路径参数 '{param_name}' 存在目录穿越片段 '..': {s}")
    return os.path.abspath(s)


def sanitize_for_log(data: Any) -> Any:
    """对日志、审计输出与异常消息中的 Token/密码/密钥字段做统一脱敏"""
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for k, v in data.items():
            if _SENSITIVE_KEY_RE.search(str(k)) and v:
                out[k] = "***REDACTED***"
            else:
                out[k] = sanitize_for_log(v)
        return out
    if isinstance(data, list):
        return [sanitize_for_log(item) for item in data]
    if isinstance(data, str):
        # 掩码类似 Bearer/token=xxx 的片段
        return re.sub(r"(?i)(token|password|secret)\s*[=:]\s*([^\s,;]+)", r"\1=***REDACTED***", data)
    return data


def _to_actionable_error(raw_err: str, context: str = "") -> str:
    """将底层 C#/PowerShell/Openness 报错转译为中文可行动修复建议"""
    msg = (raw_err or "").strip()
    low = msg.lower()
    if "engineeringsecurityexception" in low or "防火墙" in msg:
        return "【可行动建议】Openness 安全弹窗拒绝访问：请在博途桌面弹出的 Openness 防火墙提示中点击【是/全部允许】，并确认当前用户属于 'Siemens TIA Openness' 组后重新运行。"
    if "licensenotfoundexception" in low or "license" in low or "许可证" in msg:
        return "【可行动建议】缺少 STEP 7 授权：请打开 Automation License Manager 检查 STEP 7 Basic/Professional 授权。"
    if "未找到运行中的博途进程" in msg or "no running" in low:
        return "【可行动建议】未检测到已启动的 TIA Portal 进程：请先手动启动 TIA Portal (V16-V20) 并打开目标工程（严禁依赖脚本自动后台拉起无头实例）。"
    if "seriallock" in low or "互斥锁" in msg:
        return "【可行动建议】Openness 单实例写入排队超时：博途 COM 接口为单实例独占，请等待前序导入/编译任务完成后重试。"
    if "cax" in low and ("unsupported" in low or "exclude" in low or "失败" in msg):
        return "【可行动建议】CAx 导入受阻：请先运行 cax_hardware_builder.inspect_aml_hardware() 剔除变频器/伺服/非按键HMI及受限 Scalance 交换机后重试。"
    if "comparetoonline" in low or "offline" in low:
        return "【可行动建议】无法建立在线连接比对：请确认网线连接、PG/PC 接口设置以及目标 PLC IP 可达（或使用 PLCSIM 仿真器）。"
    prefix = f"[{context}] " if context else ""
    return f"【可行动建议】{prefix}操作返回异常：{sanitize_for_log(msg)}。请核对输入参数与博途状态后重试。"


# =============================================================================
# 保留原有核心接口（100% 向后兼容）
# =============================================================================
def _run_ps_script(script_name: str, args_list: List[str]) -> Dict[str, Any]:
    """安全调用同目录下的 PowerShell 脚本（强制 shell=False 列表传参）"""
    ps_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_name)
    if not os.path.exists(ps_script):
        return {"Success": False, "error": f"缺少核心脚本: {ps_script}"}

    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_script] + args_list
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            shell=False,
        )
        stdout = (proc.stdout or "").strip()
        idx = stdout.find("{")
        idx2 = stdout.find("[")
        if idx != -1 and (idx2 == -1 or idx < idx2):
            return json.loads(stdout[idx:])
        if idx2 != -1:
            parsed = json.loads(stdout[idx2:])
            return {"items": parsed} if isinstance(parsed, list) else parsed
        return {"error": stdout}
    except subprocess.CalledProcessError as e:
        out_txt = (e.stdout or "").strip()
        idx = out_txt.find("{")
        if idx != -1:
            try:
                return json.loads(out_txt[idx:])
            except Exception:
                pass
        err_txt = sanitize_for_log(e.stderr or out_txt or str(e))
        return {"Success": False, "error": f"PowerShell 执行失败: {err_txt}", "ActionableErrorMessage": _to_actionable_error(err_txt, script_name)}
    except Exception as ex:
        err_txt = sanitize_for_log(str(ex))
        return {"Success": False, "error": f"调用异常: {err_txt}", "ActionableErrorMessage": _to_actionable_error(err_txt, script_name)}


def run_ps_bridge(action: str, process_index: int = 0, **kwargs: Any) -> Dict[str, Any]:
    """调用底层的 PowerShell 桥接脚本执行诊断、附加、导入与受控下载"""
    safe_action = _validate_safe_arg("Action", action)
    args_list: List[str] = ["-Action", safe_action, "-ProcessIndex", str(int(process_index))]
    for k, v in kwargs.items():
        if isinstance(v, bool):
            if v:
                args_list.append(f"-{k}")
        elif v is not None and v != "":
            safe_k = _validate_safe_arg("param_key", k)
            if "path" in safe_k.lower() or "dir" in safe_k.lower():
                safe_v = _validate_safe_path(str(v), safe_k)
            else:
                safe_v = _validate_safe_arg(safe_k, v)
            args_list.extend([f"-{safe_k}", safe_v])

    res = _run_ps_script("tia_bridge_core.ps1", args_list)
    if isinstance(res, dict) and "items" in res and safe_action == "list-processes":
        return res["items"]  # type: ignore
    return res


def list_processes() -> List[Dict[str, Any]]:
    """列出当前运行中的博途进程与工程"""
    data = run_ps_bridge("list-processes")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "error" not in data and not data.get("Success") is False:
        return [data]
    return []


def check_environment(process_index: int = 0) -> Dict[str, Any]:
    """执行阶段 0 的完整环境自检与硬件模块深度导出"""
    return run_ps_bridge("inspect-env", process_index)


def _parse_addr_bit_span(addr: str):
    """将 %I/%Q 地址解析为 (io_type, start_bit, end_bit)"""
    addr_u = (addr or "").strip().upper()
    if not addr_u.startswith(("%I", "%Q")):
        return "Other", -1, -1
    io_type = "Input" if addr_u.startswith("%I") else "Output"
    span = parse_address_bit_span(addr_u)
    if span is not None:
        _, sb, bit_len = span
        return io_type, sb, sb + bit_len - 1
    return io_type, -1, -1


def compare_eplan_with_tia_hw(
    expected_addresses: List[str],
    env_info: Optional[Dict[str, Any]] = None,
    process_index: int = 0,
) -> Dict[str, Any]:
    """
    【功能模块 2 工业级实现】
    比对 EPLAN 图纸识别出的 I/O 地址列表与博途已组态硬件的实际地址范围（精确到起止位偏移）。
    """
    if env_info is None or "Project" not in env_info:
        env_info = check_environment(process_index)

    project_data = env_info.get("Project") or {}
    hw_modules = project_data.get("HardwareModules") or []
    proj_name = project_data.get("Name", "未知工程")

    configured_ranges = []
    for m in hw_modules:
        m_name = m.get("Name", "")
        slot = m.get("PositionNumber", "")
        for r in m.get("IoRanges", []):
            start_b = int(r.get("StartAddress", 0))
            eff_bits = r.get("EffectiveBits") or r.get("LengthBits")
            if eff_bits is not None and int(eff_bits) > 0:
                start_bit = start_b * 8
                end_bit = start_bit + int(eff_bits) - 1
                end_b = end_bit // 8
            else:
                end_b = int(r.get("EndAddress", start_b))
                start_bit = start_b * 8
                end_bit = (end_b + 1) * 8 - 1
            configured_ranges.append({
                "module": m_name,
                "slot": slot,
                "ioType": r.get("IoType", ""),
                "start": start_b,
                "end": end_b,
                "start_bit": start_bit,
                "end_bit": end_bit,
            })

    details = []
    matched_count = 0
    mismatch_count = 0

    for raw_addr in expected_addresses:
        addr = raw_addr.strip().upper()
        if not addr:
            continue

        io_type, start_bit, end_bit = _parse_addr_bit_span(addr)
        byte_idx = start_bit // 8 if start_bit >= 0 else -1

        matched_module = None
        matched_slot = None

        if io_type in ("Input", "Output") and start_bit >= 0:
            for cr in configured_ranges:
                if cr["ioType"] == io_type and cr["start_bit"] <= start_bit and end_bit <= cr["end_bit"]:
                    matched_module = cr["module"]
                    matched_slot = cr["slot"]
                    break

        if matched_module is not None:
            matched_count += 1
            details.append({
                "address": addr,
                "ioType": io_type,
                "byteIndex": byte_idx,
                "status": "MATCHED",
                "statusCn": "匹配成功",
                "moduleName": matched_module,
                "slotNumber": matched_slot,
                "notes": f"归属于槽位 {matched_slot} 模块 '{matched_module}'",
            })
        else:
            mismatch_count += 1
            details.append({
                "address": addr,
                "ioType": io_type,
                "byteIndex": byte_idx,
                "status": "OUT_OF_RANGE",
                "statusCn": "越界/未组态对应硬件",
                "moduleName": "-",
                "slotNumber": "-",
                "notes": f"地址 {addr} 超出博途当前组态的所有 {io_type} 模块地址区间，需在博途设备组态中补插模块或调整起始地址！",
            })

    md_lines = [
        f"### 📋 EPLAN 图纸 I/O vs 博途硬件组态对账差异表（工程: {proj_name}）",
        f"- **核验地址总数**: {len(details)} 点",
        f"- **对账匹配成功**: {matched_count} 点",
        f"- **组态越界/缺失**: {mismatch_count} 点 " + ("🔴 (存在严重硬件组态不一致！)" if mismatch_count > 0 else "🟢 (全部硬件通道吻合)"),
        "",
        "| 序号 | 图纸 PLC 地址 | 信号类型 | 状态 | 对应博途硬件模块 | 槽位号 | 对账诊断说明 |",
        "|---|---|---|---|---|---|---|",
    ]

    for idx, d in enumerate(details, start=1):
        status_badge = "🟢 匹配" if d["status"] == "MATCHED" else "🔴 **越界缺失**"
        md_lines.append(
            f"| {idx} | `{d['address']}` | {d['ioType']} | {status_badge} | {d['moduleName']} | {d['slotNumber']} | {d['notes']} |"
        )

    return {
        "projectName": proj_name,
        "totalChecked": len(details),
        "matchedCount": matched_count,
        "mismatchCount": mismatch_count,
        "details": details,
        "markdownReport": "\n".join(md_lines),
    }


# =============================================================================
# FastMCP Server 与 Pydantic 输入/输出 Schema 封装（8 大核心工具）
# =============================================================================
mcp = FastMCP(
    "tia-portal-commissioning-mcp",
    instructions="西门子 TIA Portal V16-V20 产线级闭环调试 MCP 服务（内置安全门禁与 OT 防注入校验）。",
)


class ImportCaxInput(BaseModel):
    aml_path: str = Field(..., description="EPLAN 导出的 .aml 硬件拓扑文件路径")
    process_index: int = Field(0, description="TIA Portal 进程索引")
    plc_name: str = Field("", description="目标 PLC 名称")
    import_option: str = Field("RetainTiaDevice", description="CAx 导入选项")
    allow_filtered: bool = Field(False, description="是否忽略排除设备预检告警并放行导入")


class CompileDiagnoseInput(BaseModel):
    import_dir: str = Field("", description="XML/变量表导入目录；留空时仅执行 compile-plc")
    confirm_token: str = Field("", description="Gate 3 语义指纹确认令牌")
    backup_dir: str = Field("", description="导入前 .zap16 与块级回滚备份目录")
    diff_only: bool = Field(False, description="是否仅执行只读指纹对比预检")
    process_index: int = Field(0, description="TIA Portal 进程索引")
    plc_name: str = Field("", description="目标 PLC 名称")
    skip_existing_blocks: bool = Field(False, description="增量保护：跳过现场已有对象")


class WatchForceInput(BaseModel):
    watch_xml_dir: str = Field(..., description="PlcWatchTable/PlcForceTable XML 目录")
    bind_web_server: bool = Field(False, description="是否绑定至 CPU Web Server")
    show_in_editor: bool = Field(False, description="是否在博途编辑器弹出窗口")
    process_index: int = Field(0, description="TIA Portal 进程索引")
    plc_name: str = Field("", description="目标 PLC 名称")


class CompareOnlineInput(BaseModel):
    process_index: int = Field(0, description="TIA Portal 进程索引")
    plc_name: str = Field("", description="目标 PLC 名称")


class DownloadPlcInput(BaseModel):
    download_mode: str = Field("Software", description="Hardware | Software | SoftwareOnlyChanges")
    confirm_online_token: str = Field(..., description="tia_compare_online 返回的在线确认令牌")
    human_approved: bool = Field(False, description="现场工程师显式人工确认标志")
    is_failsafe_plc: bool = Field(False, description="是否为 F-PLC 故障安全控制器")
    first_manual_download_done: bool = Field(True, description="F-PLC 是否已在博途 GUI 完成首次安全下载")
    process_index: int = Field(0, description="TIA Portal 进程索引")
    plc_name: str = Field("", description="目标 PLC 名称")


class ConfigureToInput(BaseModel):
    to_params_xlsx_path: str = Field(..., description="TO_Params.xlsx 工艺参数文件路径")
    process_index: int = Field(0, description="TIA Portal 进程索引")
    plc_name: str = Field("", description="目标 PLC 名称")


class LiveDataInput(BaseModel):
    action: str = Field("read_snapshot", description="read_snapshot | check_tcp_endpoint | force_output")
    tags: Optional[List[str]] = Field(None, description="待读取/监控的变量名列表")
    values: Optional[Dict[str, Any]] = Field(None, description="待写入或强制的值字典")
    plc_ip: str = Field("192.168.0.1", description="PLC S7CommPlus IP 地址")
    port: int = Field(102, description="S7CommPlus TCP 端口 (默认 102)")
    rack: int = Field(0, description="PLC 机架号 (默认 0)")
    slot: int = Field(1, description="PLC 槽位号 (S7-1200/1500 默认 1)")
    human_approved_force: bool = Field(False, description="强制物理 %Q 输出时必须为 True")
    snapshot_file: Optional[str] = Field(None, description="离线/仿真状态快照 JSON 路径")


class EplanExportInput(BaseModel):
    action: str = Field("export-all", description="inspect-eplan | export-all | export-aml | export-tables | export-pdf")
    eplan_project_path: str = Field("", description="EPLAN 工程文件路径（.elk/.elp）")
    output_dir: str = Field("", description="导出资产输出目录（如 00_输入图纸）")
    plc_scheme: str = Field("PLC_IO_Excel_Export", description="PLC I/O 点表导出方案名称")
    bom_scheme: str = Field("BOM_Part_Excel_Export", description="BOM 部件表导出方案名称")
    w3u_path: str = Field("", description="可选显式指定 EPLAN W3u.exe 路径")


class StandardToolOutput(BaseModel):
    Success: bool = Field(..., description="执行是否成功")
    ActionableErrorMessage: str = Field("", description="失败时的中文可行动修复建议")
    Data: Dict[str, Any] = Field(default_factory=dict, description="结构化输出载荷")


@mcp.tool()
def tia_import_cax_aml(
    aml_path: str,
    process_index: int = 0,
    plc_name: str = "",
    import_option: str = "RetainTiaDevice",
    allow_filtered: bool = False,
) -> Dict[str, Any]:
    """预检 EPLAN .aml 硬件拓扑（拦截驱动器/非按键HMI/受限Scalance）并导入 TIA Portal"""
    try:
        req = ImportCaxInput(
            aml_path=aml_path,
            process_index=process_index,
            plc_name=plc_name,
            import_option=import_option,
            allow_filtered=allow_filtered,
        )
        safe_aml = _validate_safe_path(req.aml_path, "aml_path")
        if req.import_option not in _ALLOWED_CAX_OPTIONS:
            return {
                "Success": False,
                "ActionableErrorMessage": f"非法的 import_option '{req.import_option}'，仅允许 {sorted(_ALLOWED_CAX_OPTIONS)}。",
            }
        _validate_safe_arg("plc_name", req.plc_name)

        try:
            from cax_hardware_builder import import_aml_to_tia
        except ImportError:
            from scripts.cax_hardware_builder import import_aml_to_tia  # type: ignore

        res = import_aml_to_tia(
            safe_aml,
            process_index=req.process_index,
            import_option=req.import_option,
            allow_filtered=req.allow_filtered,
        )
        if not res.get("Success"):
            hint = res.get("ActionableHint") or _to_actionable_error(str(res), "tia_import_cax_aml")
            res["ActionableErrorMessage"] = hint
        return sanitize_for_log(res)
    except Exception as exc:
        return {
            "Success": False,
            "BlockedBySecurityGuard": isinstance(exc, ValueError),
            "ActionableErrorMessage": _to_actionable_error(str(exc), "tia_import_cax_aml"),
        }


@mcp.tool()
def tia_compile_and_diagnose(
    import_dir: str = "",
    confirm_token: str = "",
    backup_dir: str = "",
    diff_only: bool = False,
    process_index: int = 0,
    plc_name: str = "",
    skip_existing_blocks: bool = False,
) -> Dict[str, Any]:
    """串行执行博途导入（-DiffOnly 预检 / 事务性导入与回滚）及编译诊断错误树提取"""
    try:
        req = CompileDiagnoseInput(
            import_dir=import_dir,
            confirm_token=confirm_token,
            backup_dir=backup_dir,
            diff_only=diff_only,
            process_index=process_index,
            plc_name=plc_name,
            skip_existing_blocks=skip_existing_blocks,
        )
        safe_plc = _validate_safe_arg("plc_name", req.plc_name)

        if not req.import_dir and not req.diff_only:
            res = run_ps_bridge("compile-plc", process_index=req.process_index, PlcName=safe_plc)
            if not res.get("Success"):
                res["ActionableErrorMessage"] = _to_actionable_error(str(res.get("error") or res), "compile-plc")
            return sanitize_for_log(res)

        safe_import_dir = _validate_safe_path(req.import_dir, "import_dir")
        args = ["-ImportDir", safe_import_dir, "-ProcessIndex", str(int(req.process_index))]
        if safe_plc:
            args.extend(["-PlcName", safe_plc])
        if req.skip_existing_blocks:
            args.append("-SkipExistingBlocks")

        if req.diff_only:
            args.append("-DiffOnly")
            res = _run_ps_script("do_import_pipeline.ps1", args)
            return res

        safe_token = _validate_safe_arg("confirm_token", req.confirm_token)
        safe_backup = _validate_safe_path(req.backup_dir, "backup_dir")
        if not safe_token or not safe_backup:
            return {
                "Success": False,
                "BlockedBySafetyGate": True,
                "GateReason": "GATE3_TOKEN_OR_BACKUP_MISSING",
                "ActionableErrorMessage": "未通过 Gate 3 安全门禁：正式导入博途前必须先运行 diff_only=True 并提供 confirm_token 与 backup_dir。",
            }
        args.extend(["-ConfirmToken", safe_token, "-BackupDir", safe_backup])
        res = _run_ps_script("do_import_pipeline.ps1", args)
        if not res.get("Success"):
            res["ActionableErrorMessage"] = _to_actionable_error(str(res.get("RecoveryHint") or res.get("Error") or res), "do_import_pipeline")
        return sanitize_for_log(res)
    except Exception as exc:
        return {
            "Success": False,
            "BlockedBySecurityGuard": isinstance(exc, ValueError),
            "ActionableErrorMessage": _to_actionable_error(str(exc), "tia_compile_and_diagnose"),
        }


@mcp.tool()
def tia_manage_watch_force_tables(
    watch_xml_dir: str,
    bind_web_server: bool = False,
    show_in_editor: bool = False,
    process_index: int = 0,
    plc_name: str = "",
) -> Dict[str, Any]:
    """导入 PlcWatchTable / PlcForceTable 并可选绑定至 CPU Web Server 只读监控页"""
    try:
        req = WatchForceInput(
            watch_xml_dir=watch_xml_dir,
            bind_web_server=bind_web_server,
            show_in_editor=show_in_editor,
            process_index=process_index,
            plc_name=plc_name,
        )
        safe_dir = _validate_safe_path(req.watch_xml_dir, "watch_xml_dir")
        safe_plc = _validate_safe_arg("plc_name", req.plc_name)
        res = run_ps_bridge(
            "import-watch-tables",
            process_index=req.process_index,
            PlcName=safe_plc,
            WatchXmlDir=safe_dir,
            BindWebServer=req.bind_web_server,
            ShowInEditor=req.show_in_editor,
        )
        if not res.get("Success"):
            res["ActionableErrorMessage"] = _to_actionable_error(str(res.get("error") or res), "import-watch-tables")
        return sanitize_for_log(res)
    except Exception as exc:
        return {
            "Success": False,
            "BlockedBySecurityGuard": isinstance(exc, ValueError),
            "ActionableErrorMessage": _to_actionable_error(str(exc), "tia_manage_watch_force_tables"),
        }


@mcp.tool()
def tia_compare_online(process_index: int = 0, plc_name: str = "") -> Dict[str, Any]:
    """执行博途离线工程与在线 PLC 的 CompareToOnline 差异比对，识别 F-PLC 并生成下载确认令牌"""
    try:
        req = CompareOnlineInput(process_index=process_index, plc_name=plc_name)
        safe_plc = _validate_safe_arg("plc_name", req.plc_name)
        res = run_ps_bridge("compare-online", process_index=req.process_index, PlcName=safe_plc)
        if not res.get("Success"):
            res["ActionableErrorMessage"] = _to_actionable_error(str(res.get("error") or res), "compare-online")
        return res
    except Exception as exc:
        return {
            "Success": False,
            "BlockedBySecurityGuard": isinstance(exc, ValueError),
            "ActionableErrorMessage": _to_actionable_error(str(exc), "tia_compare_online"),
        }


@mcp.tool()
def tia_download_to_plc(
    download_mode: str = "Software",
    confirm_online_token: str = "",
    human_approved: bool = False,
    is_failsafe_plc: bool = False,
    first_manual_download_done: bool = True,
    process_index: int = 0,
    plc_name: str = "",
) -> Dict[str, Any]:
    """
    受控执行向 PLC 下载程序（严禁隐式触发）：
    必须同时满足 human_approved=True、有效 confirm_online_token，且 F-PLC 已完成首次 GUI 手动下载。
    """
    try:
        req = DownloadPlcInput(
            download_mode=download_mode,
            confirm_online_token=confirm_online_token,
            human_approved=human_approved,
            is_failsafe_plc=is_failsafe_plc,
            first_manual_download_done=first_manual_download_done,
            process_index=process_index,
            plc_name=plc_name,
        )
        if req.download_mode not in _ALLOWED_DOWNLOAD_MODES:
            return {
                "Success": False,
                "BlockedBySafetyGate": True,
                "GateReason": "INVALID_DOWNLOAD_MODE",
                "ActionableErrorMessage": f"非法的下载模式 '{req.download_mode}'，仅允许 {sorted(_ALLOWED_DOWNLOAD_MODES)}。",
            }

        safe_token = _validate_safe_arg("confirm_online_token", req.confirm_online_token).strip()
        safe_plc = _validate_safe_arg("plc_name", req.plc_name)

        if not req.human_approved:
            return {
                "Success": False,
                "BlockedBySafetyGate": True,
                "GateReason": "HUMAN_APPROVAL_REQUIRED",
                "ActionableErrorMessage": "高危操作拦截：严禁在未经现场工程师显式确认（human_approved=True）的情况下向真实 PLC 下载程序！请先向工程师展示 CompareResult 并确认安全清场。",
            }

        if not re.fullmatch(r"[0-9a-fA-F]{16}", safe_token):
            return {
                "Success": False,
                "BlockedBySafetyGate": True,
                "GateReason": "MISSING_COMPARE_ONLINE_TOKEN",
                "ActionableErrorMessage": "高危操作拦截：缺少有效的 16 位十六进制在线比对确认令牌（confirm_online_token）。请先调用 tia_compare_online 获取令牌并由工程师复核。",
            }

        if req.is_failsafe_plc and not req.first_manual_download_done:
            return {
                "Success": False,
                "BlockedBySafetyGate": True,
                "GateReason": "FAILSAFE_FIRST_MANUAL_DOWNLOAD_REQUIRED",
                "IsFailSafePlc": True,
                "ActionableErrorMessage": "F-PLC 物理安全边界拦截：故障安全型控制器必须先在博途 GUI 手动完成首次完整安全程序下载（含安全密码与 CRC 确认），之后才开放 Openness 常规程序自动下载。",
            }

        res = run_ps_bridge(
            "download-plc",
            process_index=req.process_index,
            PlcName=safe_plc,
            DownloadMode=req.download_mode,
            ConfirmOnlineToken=safe_token,
            HumanApproved=True,
            FirstManualDownloadDone=req.first_manual_download_done,
        )
        if not res.get("Success"):
            res["ActionableErrorMessage"] = _to_actionable_error(str(res.get("ActionableHint") or res.get("error") or res), "download-plc")
        return sanitize_for_log(res)
    except Exception as exc:
        return {
            "Success": False,
            "BlockedBySecurityGuard": isinstance(exc, ValueError),
            "ActionableErrorMessage": _to_actionable_error(str(exc), "tia_download_to_plc"),
        }


@mcp.tool()
def tia_configure_technology_objects(
    to_params_xlsx_path: str,
    process_index: int = 0,
    plc_name: str = "",
) -> Dict[str, Any]:
    """解析 TO_Params.xlsx 并将轴控/驱动工艺对象写入博途 TechnologicalObjectGroup"""
    try:
        req = ConfigureToInput(
            to_params_xlsx_path=to_params_xlsx_path,
            process_index=process_index,
            plc_name=plc_name,
        )
        safe_xlsx = _validate_safe_path(req.to_params_xlsx_path, "to_params_xlsx_path")
        safe_plc = _validate_safe_arg("plc_name", req.plc_name)
        try:
            from cax_hardware_builder import configure_to_in_tia
        except ImportError:
            from scripts.cax_hardware_builder import configure_to_in_tia  # type: ignore

        res = configure_to_in_tia(
            safe_xlsx,
            process_index=req.process_index,
            plc_name=safe_plc,
        )
        if not res.get("Success"):
            hint = res.get("ActionableHint") or _to_actionable_error(str(res.get("Errors") or res), "tia_configure_technology_objects")
            res["ActionableErrorMessage"] = hint
        return sanitize_for_log(res)
    except Exception as exc:
        return {
            "Success": False,
            "BlockedBySecurityGuard": isinstance(exc, ValueError),
            "ActionableErrorMessage": _to_actionable_error(str(exc), "tia_configure_technology_objects"),
        }


@mcp.tool()
def tia_live_data_channel(
    action: str = "read_snapshot",
    tags: Optional[List[str]] = None,
    values: Optional[Dict[str, Any]] = None,
    plc_ip: str = "192.168.0.1",
    port: int = 102,
    rack: int = 0,
    slot: int = 1,
    human_approved_force: bool = False,
    snapshot_file: Optional[str] = None,
) -> Dict[str, Any]:
    """
    双通道实时数据读写槽位（对应 TCP 102 S7CommPlus / Snap7 live_data 与离散仿真快照架构）：
    支持探活 TCP 102、读取实时 PLC / Snap7 或工程 XML 仿真快照，以及受人工门禁保护的冷态强制预检。
    """
    try:
        req = LiveDataInput(
            action=action,
            tags=tags,
            values=values,
            plc_ip=plc_ip,
            port=port,
            rack=rack,
            slot=slot,
            human_approved_force=human_approved_force,
            snapshot_file=snapshot_file,
        )
        safe_action = _validate_safe_arg("action", req.action)
        safe_ip = _validate_safe_arg("plc_ip", req.plc_ip)
        if not re.match(r"^[A-Za-z0-9.\-:]+$", safe_ip):
            raise ValueError(f"安全拦截: 非法的 PLC IP/主机名格式 '{safe_ip}'")

        if safe_action == "force_output":
            if not req.human_approved_force:
                return {
                    "Success": False,
                    "BlockedBySafetyGate": True,
                    "GateReason": "FORCE_OUTPUT_HUMAN_APPROVAL_REQUIRED",
                    "ActionableErrorMessage": "高危操作拦截：严禁静默强制物理输出 %Q！必须确认主动力电源已隔离并由工程师显式传入 human_approved_force=True。",
                }
            return {
                "Success": True,
                "DryRun": True,
                "ExecutedOnPlc": False,
                "Channel": "PlcForceTable/S7CommPlus",
                "Endpoint": f"{safe_ip}:{int(req.port)}",
                "ForcedValues": sanitize_for_log(req.values or {}),
                "Mode": "DryRunGuardPassed_ManualForceTableRequired",
                "ActionableHint": "已通过人工安全门禁核验（DryRun），但当前 MCP 接口不直接写入物理强制表；请导入 ForceTable_*.xml 并在 TIA Portal 监控表中执行在线强制。",
            }

        if safe_action == "check_tcp_endpoint":
            reachable = False
            err_detail = ""
            try:
                with socket.create_connection((safe_ip, int(req.port)), timeout=1.5):
                    reachable = True
            except Exception as sock_ex:
                err_detail = str(sock_ex)
            return {
                "Success": reachable,
                "Channel": "TCP_102_S7CommPlus",
                "Endpoint": f"{safe_ip}:{int(req.port)}",
                "Reachable": reachable,
                "ActionableErrorMessage": "" if reachable else _to_actionable_error(err_detail, "TCP 102 探测"),
            }

        # read_snapshot:
        #   1) 若 snapshot_file 为 JSON 文件，直接读取；
        #   2) 若 snapshot_file 为包含 LAD XML 的目录，执行 1 个仿真扫描周期产出快照；
        #   3) 若未传 snapshot_file，尝试通过 snap7 读取实时映像区；失败时诚实返回 Success=False 与原因。
        snapshot_data: Dict[str, Any] = {}
        data_source = "LiveDataChannelUnavailable"
        snap7_error = ""
        if req.snapshot_file:
            safe_snap = _validate_safe_path(req.snapshot_file, "snapshot_file")
            if os.path.isfile(safe_snap):
                with open(safe_snap, "r", encoding="utf-8-sig") as sf:
                    snapshot_data = json.load(sf)
                data_source = "SimulationOrOfflineSnapshot"
            elif os.path.isdir(safe_snap):
                try:
                    from plcsim_closed_loop_runner import LadScanCycleSimulator
                except ImportError:
                    from scripts.plcsim_closed_loop_runner import LadScanCycleSimulator  # type: ignore
                sim = LadScanCycleSimulator(safe_snap)
                snapshot_data = sim.step_cycle(dt_sec=0.25)
                data_source = "LadScanCycleSimulatorLiveStep"
            else:
                return {
                    "Success": False,
                    "Connected": False,
                    "Channel": "DualPipe_LiveData_Slot",
                    "Source": data_source,
                    "ActionableErrorMessage": f"【可行动建议】指定的 snapshot_file 路径不存在: {safe_snap}",
                }
        else:
            try:
                import snap7  # type: ignore

                with socket.create_connection((safe_ip, int(req.port)), timeout=0.5):
                    pass
                client = snap7.client.Client()
                client.connect(safe_ip, int(req.rack), int(req.slot), tcpport=int(req.port))
                try:
                    pe_bytes = client.read_area(snap7.type.Areas.PE, 0, 0, 8)
                    pa_bytes = client.read_area(snap7.type.Areas.PA, 0, 0, 8)
                    snapshot_data = {
                        "_raw_PE": list(pe_bytes),
                        "_raw_PA": list(pa_bytes),
                    }
                    data_source = "Snap7LivePlcTcp102"
                finally:
                    client.disconnect()
            except ImportError:
                snap7_error = "当前 Python 环境未安装 python-snap7 依赖"
            except Exception as snap_ex:
                snap7_error = str(snap_ex)

            if data_source != "Snap7LivePlcTcp102":
                return {
                    "Success": False,
                    "Connected": False,
                    "Channel": "DualPipe_LiveData_Slot",
                    "Protocol": "S7CommPlus/TCP102",
                    "Endpoint": f"{safe_ip}:{int(req.port)}",
                    "Source": data_source,
                    "Snap7Error": sanitize_for_log(snap7_error),
                    "ActionableErrorMessage": (
                        f"【可行动建议】未能读取在线 PLC 实时映像区（{sanitize_for_log(snap7_error)}）。"
                        "若连接真实 PLC，请在博途 CPU 属性中勾选【允许来自远程对象的 PUT/GET 通信访问】并确认 TCP 102 可达；"
                        "若离线仿真调试，请传入 snapshot_file（LAD XML 目录或快照 JSON 路径）。"
                    ),
                }

        req_tags = req.tags or list(snapshot_data.keys())
        values_out = {t: snapshot_data.get(t) for t in req_tags}
        return sanitize_for_log({
            "Success": True,
            "Connected": True,
            "Channel": "DualPipe_LiveData_Slot",
            "Protocol": "S7CommPlus/TCP102",
            "Endpoint": f"{safe_ip}:{int(req.port)}",
            "Source": data_source,
            "Tags": values_out,
        })
    except Exception as exc:
        return {
            "Success": False,
            "BlockedBySecurityGuard": isinstance(exc, ValueError),
            "ActionableErrorMessage": _to_actionable_error(str(exc), "tia_live_data_channel"),
        }


@mcp.tool()
def eplan_export_project_assets(
    action: str = "export-all",
    eplan_project_path: str = "",
    output_dir: str = "",
    plc_scheme: str = "PLC_IO_Excel_Export",
    bom_scheme: str = "BOM_Part_Excel_Export",
    w3u_path: str = "",
) -> Dict[str, Any]:
    """
    通过 EPLAN W3u.exe 命令行静默探测本机环境或导出 .aml / PLC I/O 点表(.xlsx) / BOM 表(.xlsx) / 原理图(.pdf)。
    内置路径穿越（'..'）、Shell 元字符及方案名白名单防注入校验。
    """
    try:
        req = EplanExportInput(
            action=action,
            eplan_project_path=eplan_project_path,
            output_dir=output_dir,
            plc_scheme=plc_scheme,
            bom_scheme=bom_scheme,
            w3u_path=w3u_path,
        )
        safe_action = _validate_safe_arg("action", req.action).strip()
        if safe_action not in _ALLOWED_EPLAN_ACTIONS:
            raise ValueError(f"安全拦截: 非法的 EPLAN 动作 '{safe_action}'，仅允许 {sorted(_ALLOWED_EPLAN_ACTIONS)}")

        safe_plc_scheme = _validate_safe_arg("plc_scheme", req.plc_scheme).strip()
        safe_bom_scheme = _validate_safe_arg("bom_scheme", req.bom_scheme).strip()
        if not safe_plc_scheme or not _ALLOWED_SCHEME_RE.match(safe_plc_scheme):
            raise ValueError(f"安全拦截: 非法的 plc_scheme 方案名称 '{safe_plc_scheme}'")
        if not safe_bom_scheme or not _ALLOWED_SCHEME_RE.match(safe_bom_scheme):
            raise ValueError(f"安全拦截: 非法的 bom_scheme 方案名称 '{safe_bom_scheme}'")

        safe_w3u = _validate_safe_path(req.w3u_path, "w3u_path") if req.w3u_path else ""
        safe_proj = _validate_safe_path(req.eplan_project_path, "eplan_project_path") if req.eplan_project_path else ""
        safe_out = _validate_safe_path(req.output_dir, "output_dir") if req.output_dir else ""

        args: List[str] = ["-Action", safe_action]
        if safe_w3u:
            args.extend(["-W3uPath", safe_w3u])

        if safe_action != "inspect-eplan":
            if not safe_proj or not safe_out:
                return {
                    "Success": False,
                    "ActionableErrorMessage": "【可行动建议】执行 EPLAN 静默导出必须同时提供 eplan_project_path（.elk/.elp）与 output_dir。",
                }
            args.extend([
                "-EplanProjectPath", safe_proj,
                "-OutputDir", safe_out,
                "-PlcScheme", safe_plc_scheme,
                "-BomScheme", safe_bom_scheme,
            ])

        res = _run_ps_script("eplan_auto_export.ps1", args)
        if not res.get("Success") and not res.get("ActionableErrorMessage"):
            res["ActionableErrorMessage"] = res.get("ActionableHint") or _to_actionable_error(
                str(res.get("Error") or res.get("error") or res),
                "eplan_export_project_assets",
            )
        return sanitize_for_log(res)
    except Exception as exc:
        return {
            "Success": False,
            "BlockedBySecurityGuard": isinstance(exc, ValueError),
            "ActionableErrorMessage": _to_actionable_error(str(exc), "eplan_export_project_assets"),
        }


if __name__ == "__main__":
    if "--mcp" in sys.argv:
        mcp.run()
    else:
        ensure_utf8_stdout()
        print("=== TIA Connect 模块与 FastMCP 槽位状态 ===")
        env = check_environment()
        print(json.dumps(sanitize_for_log(env), ensure_ascii=False, indent=2))
