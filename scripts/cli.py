# -*- coding: utf-8 -*-
"""
模块名称: cli
功能说明: eplan-to-tia-lad 统一命令行入口，封装高频工程动作（对账、IR 提取、AML 预检、
          监控表生成、仿真自反馈块生成、Safety Critic 审查、闭环仿真、交付三表生成），
          避免 Agent 每次临时手写胶水脚本。
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common_utils import ensure_utf8_stdout  # noqa: E402


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _write_json(path: str, data: Any) -> str:
    abs_p = os.path.abspath(path)
    parent = os.path.dirname(abs_p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(abs_p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return abs_p


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_reconcile(args: argparse.Namespace) -> int:
    from eplan_parser import generate_reconciliation_report

    rep = generate_reconciliation_report(
        project_name=args.project_name,
        plc_table_path=args.plc_table,
        bom_table_path=args.bom,
        process_spec_paths=args.spec or [],
        tia_status_json_path=args.tia_status_json,
    )
    out_md_saved = ""
    if args.out_md:
        abs_md = os.path.abspath(args.out_md)
        os.makedirs(os.path.dirname(abs_md), exist_ok=True)
        with open(abs_md, "w", encoding="utf-8") as f:
            f.write(rep["markdown"])
        out_md_saved = abs_md

    _print_json({
        "success": True,
        "project_name": args.project_name,
        "io_count": len(rep.get("io_items", [])),
        "bom_count": len(rep.get("bom", {})),
        "red_alerts": rep.get("red_alerts", []),
        "spec_warnings": rep.get("spec_warnings", []),
        "hw_mismatch_count": rep.get("hw_check", {}).get("mismatchCount", 0),
        "out_md": out_md_saved,
    })
    return 0


def cmd_extract_ir(args: argparse.Namespace) -> int:
    from parse_engineering_drawings import (
        export_fds_docx,
        extract_process_ir_from_drawings,
    )

    ir = extract_process_ir_from_drawings(
        pdf_paths=args.pdf or [],
        xlsx_io_path=args.io,
        bom_path=args.bom,
        spec_paths=args.spec or [],
        safety_matrix_path=args.safety_matrix,
    )
    out_json_saved = _write_json(args.out_json, ir) if args.out_json else ""
    out_fds_saved = (
        export_fds_docx(ir, args.export_fds, project_name=args.project_name)
        if args.export_fds
        else ""
    )
    _print_json({
        "success": True,
        "extraction_mode": ir.get("extraction_mode"),
        "fallback_reason": ir.get("fallback_reason", ""),
        "stations": ir.get("stations", []),
        "safety_input_count": len(ir.get("safety_inputs", [])),
        "actuator_count": len(ir.get("actuators", [])),
        "step_count": len(ir.get("steps", [])),
        "interlock_count": len(ir.get("interlocks", [])),
        "missing_clarifications": ir.get("missing_clarifications", []),
        "out_json": out_json_saved,
        "out_fds_docx": out_fds_saved,
    })
    return 0


def cmd_inspect_aml(args: argparse.Namespace) -> int:
    from cax_hardware_builder import build_to_params_template, inspect_aml_hardware

    res = inspect_aml_hardware(args.aml_path)
    to_path = ""
    if args.to_template:
        to_path = build_to_params_template(res.get("unsupported_devices", []), args.to_template)
    res["to_template_path"] = to_path
    _print_json(res)
    return 0


def cmd_build_watch_tables(args: argparse.Namespace) -> int:
    from eplan_parser import parse_eplan_plc_table
    from watch_table_builder import build_station_watch_tables_from_tags

    tags = parse_eplan_plc_table(args.io_table)
    res = build_station_watch_tables_from_tags(
        tags,
        args.out_dir,
        timer_members=args.timer_member or [],
    )
    _print_json({"success": True, **res})
    return 0


def cmd_build_sim_feedback(args: argparse.Namespace) -> int:
    from eplan_parser import parse_eplan_plc_table
    from sim_feedback_builder import build_sim_feedback_fc_xml, infer_actuator_sensor_pairs

    io_tags = parse_eplan_plc_table(args.io_table)
    ir_data = _load_json(args.ir_json) if args.ir_json else None
    pairs = infer_actuator_sensor_pairs(io_tags, ir_data=ir_data, default_travel_time=args.travel_time)
    _, timer_members, sim_m_tags = build_sim_feedback_fc_xml(
        pairs,
        timer_db_name=args.timer_db,
        out_xml_path=args.out_xml,
    )
    _print_json({
        "success": True,
        "out_xml": os.path.abspath(args.out_xml),
        "pair_count": len(pairs),
        "pairs": pairs,
        "timer_members": timer_members,
        "sim_m_tags": sim_m_tags,
    })
    return 0


def cmd_critic(args: argparse.Namespace) -> int:
    from parse_engineering_drawings import run_local_safety_critic

    ir_data = _load_json(args.ir_json)
    res = run_local_safety_critic(
        args.xml_dir,
        ir_data,
        eplan_io_path=args.eplan_io,
        tia_status_json_path=args.tia_status_json,
        strict_compact=(not args.allow_fragmented),
    )
    _print_json(res)
    return 0 if res.get("passed") else 1


def cmd_simulate(args: argparse.Namespace) -> int:
    from plcsim_closed_loop_runner import run_closed_loop_evaluation

    ir_data = _load_json(args.ir_json)
    res = run_closed_loop_evaluation(
        args.xml_dir,
        ir_data,
        cpu_model=args.cpu_model,
        plcsim_adv_dll_path=args.plcsim_dll,
    )
    _print_json(res)
    return 0 if res.get("passed") else 1


def cmd_deliver(args: argparse.Namespace) -> int:
    from reverse_to_delivery_tables import reverse_generate

    res = reverse_generate(
        args.xml_dir,
        args.out_dir,
        from_tia=args.from_tia,
        process_index=args.process_index,
        project_path=args.project_path,
        plc_name=args.plc_name,
    )
    _print_json(res)
    return 0 if res.get("success") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="eplan-to-tia-lad 统一工程流水线 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_rec = sub.add_parser("reconcile", help="四方对账并输出 01_识图清单.md")
    p_rec.add_argument("--project-name", required=True, help="项目名称")
    p_rec.add_argument("--plc-table", required=True, help="PLC I/O 表 (.xlsx/.csv)")
    p_rec.add_argument("--bom", default=None, help="设备 BOM 表 (.xlsx/.csv)")
    p_rec.add_argument("--spec", action="append", help="工艺步序/联锁文档 (.md/.json)")
    p_rec.add_argument("--tia-status-json", default=None, help="博途环境快照 JSON")
    p_rec.add_argument("--out-md", default=None, help="输出 01_识图清单.md 路径")
    p_rec.set_defaults(func=cmd_reconcile)

    p_ir = sub.add_parser("extract-ir", help="提取工艺状态机 JSON IR 并可选导出 FDS.docx")
    p_ir.add_argument("--io", default=None, help="PLC I/O 表 (.xlsx/.csv)")
    p_ir.add_argument("--bom", default=None, help="设备 BOM 表 (.xlsx/.csv)")
    p_ir.add_argument("--pdf", action="append", help="原理图 PDF 路径（可多次传入）")
    p_ir.add_argument("--spec", action="append", help="工艺步序文档 (.md/.json)")
    p_ir.add_argument("--safety-matrix", default=None, help="安全联锁矩阵 (.md/.xlsx)")
    p_ir.add_argument("--out-json", default=None, help="保存 IR JSON 路径")
    p_ir.add_argument("--export-fds", default=None, help="导出 FDS.docx 路径")
    p_ir.add_argument("--project-name", default="自动化产线控制工程", help="FDS 标题项目名")
    p_ir.set_defaults(func=cmd_extract_ir)

    p_aml = sub.add_parser("inspect-aml", help="CAx .aml 硬件拓扑预检并可选生成 TO_Params.xlsx")
    p_aml.add_argument("aml_path", help="EPLAN 导出的 .aml 文件路径")
    p_aml.add_argument("--to-template", default=None, help="输出 TO_Params.xlsx 路径")
    p_aml.set_defaults(func=cmd_inspect_aml)

    p_wt = sub.add_parser("build-watch-tables", help="批量生成工位监控表与冷态强制测试表 XML")
    p_wt.add_argument("--io-table", required=True, help="PLC I/O 表 (.xlsx/.csv)")
    p_wt.add_argument("--out-dir", required=True, help="监控/强制表 XML 输出目录")
    p_wt.add_argument("--timer-member", action="append", help="需监控的全局定时器成员名")
    p_wt.set_defaults(func=cmd_build_watch_tables)

    p_simfb = sub.add_parser("build-sim-feedback", help="生成 FC99_仿真自反馈.xml")
    p_simfb.add_argument("--io-table", required=True, help="PLC I/O 表 (.xlsx/.csv)")
    p_simfb.add_argument("--out-xml", required=True, help="输出 FC99_仿真自反馈.xml 路径")
    p_simfb.add_argument("--ir-json", default=None, help="工艺 IR JSON 路径")
    p_simfb.add_argument("--timer-db", default="全局定时器", help="定时器 DB 名称")
    p_simfb.add_argument("--travel-time", default="T#1S", help="默认行程延时")
    p_simfb.set_defaults(func=cmd_build_sim_feedback)

    p_crt = sub.add_parser("critic", help="运行本地 Safety Critic 安全联锁与反碎片化形式化审查")
    p_crt.add_argument("xml_dir", help="LAD XML 目录（如 03_梯形图XML）")
    p_crt.add_argument("--ir-json", required=True, help="工艺 IR JSON 路径")
    p_crt.add_argument("--eplan-io", default=None, help="01_识图清单.md 路径")
    p_crt.add_argument("--tia-status-json", default=None, help="博途环境快照 JSON")
    p_crt.add_argument("--allow-fragmented", action="store_true", help="仅告警不阻断碎段/过渡%M位（仅兼容旧工程时使用）")
    p_crt.set_defaults(func=cmd_critic)

    p_sim = sub.add_parser("simulate", help="运行离散扫描周期闭环虚拟调试评测")
    p_sim.add_argument("xml_dir", help="LAD XML 目录（如 03_梯形图XML）")
    p_sim.add_argument("--ir-json", required=True, help="工艺 IR JSON 路径")
    p_sim.add_argument("--cpu-model", default="CPU 1214C DC/DC/DC", help="目标 CPU 型号")
    p_sim.add_argument("--plcsim-dll", default=None, help="可选 PLCSIM Advanced Runtime DLL 路径")
    p_sim.set_defaults(func=cmd_simulate)

    p_del = sub.add_parser("deliver", help="生成 05_现场调试交付三表.xlsx/.md 与 PLC变量表_中文.xlsx")
    p_del.add_argument("xml_dir", help="LAD XML 目录（或项目目录）")
    p_del.add_argument("--out-dir", required=True, help="交付物输出目录")
    p_del.add_argument("--from-tia", action="store_true", help="直接从运行中的博途进程反向导出")
    p_del.add_argument("--process-index", type=int, default=-1, help="博途进程索引")
    p_del.add_argument("--project-path", default="", help="博途项目路径")
    p_del.add_argument("--plc-name", default="", help="目标 PLC 名称")
    p_del.set_defaults(func=cmd_deliver)

    return parser


def main() -> int:
    ensure_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
