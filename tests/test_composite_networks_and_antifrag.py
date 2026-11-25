# -*- coding: utf-8 -*-
"""复合程序段构建器、S2 能流精准判定与反碎片化门禁测试"""

import json
import subprocess
import sys
from pathlib import Path

from conftest import SKILL_ROOT, default_io_tags, default_m_tags
from lad_builder import (
    build_composite_network,
    build_dual_interlocked_actuators_network,
    build_fc_xml,
    build_indicator_lamps_network,
    build_line_handover_networks,
    build_mode_select_network,
    build_ob1_xml,
    build_simple_series_network,
    build_sr_latch_network,
    build_star_delta_networks,
    build_step_with_timer_network,
    build_timeout_alarm_network,
    build_timer_db_xml,
    write_xml_file,
)
from parse_engineering_drawings import run_local_safety_critic
from plcsim_closed_loop_runner import LadScanCycleSimulator
from sim_feedback_builder import build_sim_feedback_fc_xml
from tag_builder import _write_tag_table_xml
from validate_lad_xml import validate_project_xmls


def test_composite_builders_and_star_delta_pass_strict_compact(tmp_path: Path):
    """验证高阶复合程序段构建器（模式/SR/超时报警/步序+延时/双执行器/2段星三角/指示灯）0错误0告警通过严格门禁"""
    tag_dir = tmp_path / "02_变量表"
    xml_dir = tmp_path / "03_梯形图XML"
    tag_dir.mkdir()
    xml_dir.mkdir()

    io_tags = [
        t for t in default_io_tags()
        if t["name"] not in ("SB2_1号电机反转启动", "KM1_1号电机正转", "KM2_1号电机反转")
    ] + [
        {"name": "SA1_手自动选择", "data_type": "Bool", "logical_address": "%I0.3", "comment": "[1.4 | -SA1 | 13-14] 手自动(NO)"},
        {"name": "SB4_故障复位", "data_type": "Bool", "logical_address": "%I0.5", "comment": "[1.6 | -SB4 | 13-14] 复位(NO)"},
        {"name": "B1_动作到位", "data_type": "Bool", "logical_address": "%I0.6", "comment": "[1.7 | -B1 | BK] 到位(NO)"},
        {"name": "KM1_主接触器", "data_type": "Bool", "logical_address": "%Q0.0", "comment": "[2.1 | -KM1 | Q0.0] 主接触器(NO)"},
        {"name": "KM2_星形接触器", "data_type": "Bool", "logical_address": "%Q0.1", "comment": "[2.2 | -KM2 | Q0.1] 星形接触器(NO)"},
        {"name": "KM3_角形接触器", "data_type": "Bool", "logical_address": "%Q0.2", "comment": "[2.3 | -KM3 | Q0.2] 角形接触器(NO)"},
        {"name": "YV1_送料伸出", "data_type": "Bool", "logical_address": "%Q0.3", "comment": "[2.4 | -YV1 | Q0.3] 伸出(NO)"},
        {"name": "YV2_送料缩回", "data_type": "Bool", "logical_address": "%Q0.4", "comment": "[2.5 | -YV2 | Q0.4] 缩回(NO)"},
        {"name": "HL1_运行指示灯", "data_type": "Bool", "logical_address": "%Q0.5", "comment": "[2.6 | -HL1 | Q0.5] 运行灯(NO)"},
        {"name": "HL2_故障指示灯", "data_type": "Bool", "logical_address": "%Q0.6", "comment": "[2.7 | -HL2 | Q0.6] 故障灯(NO)"},
    ]
    m_tags = default_m_tags() + [
        {"name": "M_急停报警", "data_type": "Bool", "logical_address": "%M10.2", "comment": "[FC1/NW1] 急停报警"},
        {"name": "M_自动模式", "data_type": "Bool", "logical_address": "%M10.3", "comment": "[FC1/NW2] 自动模式"},
        {"name": "M_手动模式", "data_type": "Bool", "logical_address": "%M10.4", "comment": "[FC1/NW2] 手动模式"},
        {"name": "M_报警_动作超时", "data_type": "Bool", "logical_address": "%M10.5", "comment": "[FC1/NW3] 超时报警"},
        {"name": "M_报警汇总", "data_type": "Bool", "logical_address": "%M10.6", "comment": "[FC1/NW3] 报警汇总"},
        {"name": "M_步序_S10_延时", "data_type": "Bool", "logical_address": "%M11.0", "comment": "[FC2/NW3] 步序10"},
        {"name": "M_请求_送料缩回", "data_type": "Bool", "logical_address": "%M11.1", "comment": "[FC2/NW3] 缩回请求"},
    ]
    _write_tag_table_xml("IO变量表", io_tags, str(tag_dir / "TagTable_IO变量表.xml"))
    _write_tag_table_xml("M辅助继电器表", m_tags, str(tag_dir / "TagTable_M辅助继电器表.xml"))

    fc1_nws = [
        # 同段混合：安全常开使能 + 急停取反报警置位 + 急停正常按复位清除（验证 S2 不误杀）
        build_composite_network(
            1,
            "NW1 安全准入与急停报警锁存",
            "【功能】安全条件汇总及急停S/R报警锁存",
            rungs=[
                {
                    "contacts": [{"name": "SB0_急停按钮", "negated": False}],
                    "coil_var": "M_急停正常",
                },
                {
                    "contacts": [{"name": "M_急停正常", "negated": False}, {"name": "FR1_1号电机热继", "negated": False}],
                    "coil_var": "M_安全条件满足",
                },
                {
                    "contacts": [{"name": "SB0_急停按钮", "negated": True}],
                    "coil_var": "M_急停报警",
                    "coil_type": "SCoil",
                },
                {
                    "contacts": [{"name": "SB0_急停按钮", "negated": False}, {"name": "SB4_故障复位", "negated": False}],
                    "coil_var": "M_急停报警",
                    "coil_type": "RCoil",
                },
            ],
        ),
        build_mode_select_network(
            2,
            "NW2 手自动模式切换",
            "【功能】单旋钮 SA1 切换自动与手动模式",
            selector_var="SA1_手自动选择",
        ),
        build_timeout_alarm_network(
            3,
            "NW3 动作超时监控与报警汇总",
            "【功能】S10 超时 T#3S 报警置位/复位与报警汇总",
            enable_contacts=[{"name": "M_步序_S10_延时", "negated": False}, {"name": "B1_动作到位", "negated": True}],
            timer_db_member="T3_超时",
            time_pt="T#3S",
            alarm_var="M_报警_动作超时",
            reset_contacts=[{"name": "SB4_故障复位", "negated": False}],
            summary_alarms=["M_急停报警", "M_报警_动作超时"],
            summary_var="M_报警汇总",
        ),
    ]
    write_xml_file(str(xml_dir / "FC1_安全模式与报警.xml"), build_fc_xml("FC1_安全模式与报警", 1, fc1_nws))

    sd_nws = build_star_delta_networks(
        start_nw_index=1,
        device_label="主电机",
        start_contacts=[{"name": "SB1_1号电机正转启动", "negated": False}],
        stop_contacts=[{"name": "SB3_1号电机停止", "negated": False}, {"name": "FR1_1号电机热继", "negated": False}],
        km_main="KM1_主接触器",
        km_star="KM2_星形接触器",
        km_delta="KM3_角形接触器",
        timer_switch_member="T1_星三角切换",
        timer_delay_member="T2_换接延时",
        switch_pt="T#1S",
        delay_pt="T#250MS",
    )
    assert len(sd_nws) == 2

    fc2_nws = sd_nws + [
        build_step_with_timer_network(
            3,
            "NW3 S10 自动步序与延时退步",
            "【功能】S10 自动步序自锁及 T4 延时 1S 自动结束",
            step_var="M_步序_S10_延时",
            trigger_contacts=[{"name": "B1_动作到位", "negated": False}],
            timer_db_member="T4_步序延时",
            time_pt="T#1S",
        ),
        build_sr_latch_network(
            4,
            "NW4 缩回请求成对置位复位",
            "【功能】S10 置位缩回请求，B1 断开时复位",
            target_var="M_请求_送料缩回",
            set_branches=[[{"name": "M_步序_S10_延时", "negated": False}]],
            reset_branches=[[{"name": "B1_动作到位", "negated": True}]],
        ),
        build_dual_interlocked_actuators_network(
            5,
            "NW5 送料双电控电磁阀互锁驱动",
            "【功能】YV1 伸出与 YV2 缩回同段互锁",
            actuator_a={
                "coil_var": "YV1_送料伸出",
                "manual_trigger_var": "SB1_1号电机正转启动",
                "auto_request_var": "M_步序_S10_延时",
            },
            actuator_b={"coil_var": "YV2_送料缩回", "auto_request_var": "M_请求_送料缩回"},
        ),
        build_indicator_lamps_network(
            6,
            "NW6 状态指示灯组",
            "【功能】运行灯与故障灯同段输出",
            lamp_rungs=[
                {"contacts": [{"name": "KM1_主接触器", "negated": False}, {"name": "M_报警汇总", "negated": True}], "coil_var": "HL1_运行指示灯"},
                {"contacts": [{"name": "M_报警汇总", "negated": False}], "coil_var": "HL2_故障指示灯"},
            ],
        ),
    ]
    write_xml_file(str(xml_dir / "FC2_工艺与输出.xml"), build_fc_xml("FC2_工艺与输出", 2, fc2_nws))
    write_xml_file(
        str(xml_dir / "DB_全局定时器.xml"),
        build_timer_db_xml("全局定时器", ["T1_星三角切换", "T2_换接延时", "T3_超时", "T4_步序延时"]),
    )
    write_xml_file(str(xml_dir / "OB1_Main.xml"), build_ob1_xml(["FC1_安全模式与报警", "FC2_工艺与输出"]))

    res = validate_project_xmls(str(xml_dir), check_compact=True, strict_compact=True)
    assert res.ok, f"复合程序段严格校验失败: {res.errors}"
    assert not res.warnings, f"存在意外告警: {res.warnings}"

    # 仿真验证 2 段式星三角切换（主接触器 -> 星形 -> 死区 -> 角形）
    sim = LadScanCycleSimulator(str(xml_dir))
    sim.reset({"SB0_急停按钮": True, "FR1_1号电机热继": True, "SB3_1号电机停止": True})
    st_start = sim.step_cycle(0.25, {"SB1_1号电机正转启动": True})
    assert st_start["KM1_主接触器"] is True
    assert st_start["KM2_星形接触器"] is True
    assert st_start["KM3_角形接触器"] is False

    # 推进 1.5s 以上，应切换为角形吸合、星形断开
    st_delta = {}
    for _ in range(8):
        st_delta = sim.step_cycle(0.25, {"SB1_1号电机正转启动": False})
    assert st_delta["KM1_主接触器"] is True
    assert st_delta["KM2_星形接触器"] is False
    assert st_delta["KM3_角形接触器"] is True


def test_composite_s2_still_rejects_negated_safety_driving_actuator(make_project):
    """同段混合电路中，若常闭安全触点错误驱动执行器线圈，S2 仍必须精确拦截"""
    nw = build_composite_network(
        1,
        "NW1 混合安全违规",
        "【功能】支路1合法报警，支路2违规常闭驱动KM1",
        rungs=[
            {
                "contacts": [{"name": "SB0_急停按钮", "negated": True}],
                "coil_var": "M_急停正常",
                "coil_type": "RCoil",
            },
            {
                "contacts": [{"name": "SB0_急停按钮", "negated": True}, {"name": "M_安全条件满足", "negated": False}],
                "coil_var": "KM1_1号电机正转",
            },
        ],
    )
    xml_dir = make_project(fc2_networks=[nw])
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("S2" in e and "KM1_1号电机正转" in e for e in res.errors)


def test_anti_fragmentation_catches_transitory_m_split_sr_and_mini_fc(make_project):
    """验证反碎片化审计准确捕获：1) 同块过渡 %M 位；2) 跨段拆分 S/R；3) 单段迷你空壳 FC"""
    m_tags = default_m_tags() + [
        {"name": "M_脉冲到达过渡", "data_type": "Bool", "logical_address": "%M12.0", "comment": "[FC2/NW1] 过渡位"},
        {"name": "M_报警_测试", "data_type": "Bool", "logical_address": "%M12.1", "comment": "[FC2/NW2] 测试报警"},
    ]
    fc2_nws = [
        build_simple_series_network(
            1, "NW1 过渡写入", "【功能】写过渡位",
            [{"name": "SB1_1号电机正转启动", "negated": False}],
            "M_脉冲到达过渡",
        ),
        build_simple_series_network(
            2, "NW2 过渡读并置位", "【功能】读过渡位并 SCoil",
            [{"name": "M_脉冲到达过渡", "negated": False}],
            "M_报警_测试",
            coil_type="SCoil",
        ),
        build_simple_series_network(
            3, "NW3 拆段复位", "【功能】拆到另一段 RCoil",
            [{"name": "SB3_1号电机停止", "negated": False}],
            "M_报警_测试",
            coil_type="RCoil",
        ),
        build_simple_series_network(
            4, "NW4 驱动电机", "【功能】受控输出",
            [{"name": "M_安全条件满足", "negated": False}, {"name": "M_报警_测试", "negated": True}],
            "KM1_1号电机正转",
        ),
    ]
    mini_fc3 = build_fc_xml(
        "FC3_单段空壳",
        3,
        [build_simple_series_network(1, "NW1 仅一段", "【功能】空壳", [{"name": "M_安全条件满足", "negated": False}], "KM2_1号电机反转")],
    )
    xml_dir = make_project(
        m_tags=m_tags,
        fc2_networks=fc2_nws,
        extra_fcs={"FC3_单段空壳.xml": mini_fc3},
        ob1_calls=["FC1_安全与模式", "FC2_测试设备", "FC3_单段空壳"],
    )

    # 默认 Python 调用保持向后兼容（不报错不告警）
    compat_res = validate_project_xmls(str(xml_dir))
    assert compat_res.ok

    # check_compact=True 输出 WARN
    warn_res = validate_project_xmls(str(xml_dir), check_compact=True)
    assert warn_res.ok
    assert len(warn_res.warnings) >= 3

    # strict_compact=True 与 Critic 阻断并输出重写指令
    ir_json = xml_dir.parent / "ir.json"
    ir_json.write_text(json.dumps({"interlocks": []}, ensure_ascii=False), encoding="utf-8")

    critic_res = run_local_safety_critic(str(xml_dir), {"interlocks": []}, strict_compact=True)
    assert critic_res["passed"] is False
    codes = {v["code"] for v in critic_res["violations"]}
    assert {"FRAG_TRANSITORY_M", "FRAG_SPLIT_SR", "FRAG_MINI_FC"} <= codes

    # CLI critic 默认强拦截（返回码 != 0），加 --allow-fragmented 时放行
    cli_py = str(SKILL_ROOT / "scripts" / "cli.py")
    p_block = subprocess.run(
        [sys.executable, cli_py, "critic", str(xml_dir), "--ir-json", str(ir_json)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert p_block.returncode == 1
    assert json.loads(p_block.stdout)["passed"] is False

    p_allow = subprocess.run(
        [sys.executable, cli_py, "critic", str(xml_dir), "--ir-json", str(ir_json), "--allow-fragmented"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert p_allow.returncode == 0
    assert json.loads(p_allow.stdout)["passed"] is True


def test_compact_handover_and_sim_feedback_halve_networks():
    """验证工位交接 compact=True（4段->2段）与 FC99 默认合段（双梯级）"""
    ho_compact = build_line_handover_networks(
        1, "OP10", "OP20", "DB_工位交接.OP10_OP20",
        "M_上游完成", "M_下游就绪", "B1_到位", "M_传输请求",
        compact=True,
    )
    assert len(ho_compact) == 2

    fc99_xml, timers, _ = build_sim_feedback_fc_xml([
        {
            "actuator_group": "1号送料气缸",
            "control_mode": "single_acting",
            "work_coil": "YV1_伸出",
            "work_sensor": "B2_伸出到位",
            "home_sensor": "B1_缩回到位",
            "travel_time": "T#1S",
        }
    ])
    assert fc99_xml.count("<SW.Blocks.CompileUnit") == 1
    assert len(timers) == 2
