# -*- coding: utf-8 -*-
"""安全规则 S2 ~ S5 与拓扑串联判定回归测试"""

from conftest import build_parallel_then_series_network, default_io_tags
from lad_builder import build_latch_network, build_simple_series_network
from validate_lad_xml import validate_project_xmls


def test_self_latch_km1_does_not_bypass_s3(make_project):
    """KM1_1号电机正转 仅有自身自锁触点而无 M_安全条件满足 时，不得通过 S3"""
    nw1 = build_latch_network(
        1,
        "NW1 无安全许可的正转自锁",
        "【功能】缺失 M_安全条件满足 的错误回路",
        start_var="SB1_1号电机正转启动",
        latch_var="KM1_1号电机正转",
        stop_contacts=[],
        coil_var="KM1_1号电机正转",
    )
    xml_dir = make_project(fc2_networks=[nw1])
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("S3" in e for e in res.errors), f"预期报 S3 错误，实际: {res.errors}"


def test_parallel_safety_contact_rejected_by_s3(make_project):
    """安全许可 M_安全条件满足 放在并联支路上（不能切断线圈）时，S3 必须报错"""
    nw1 = build_parallel_then_series_network(
        1,
        "NW1 并联安全条件",
        "【功能】安全触点误放进并联支路",
        branch_a={"name": "SB1_1号电机正转启动", "negated": False},
        branch_b={"name": "M_安全条件满足", "negated": False},
        series_after=[],
        coil_var="KM1_1号电机正转",
    )
    xml_dir = make_project(fc2_networks=[nw1])
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("S3" in e for e in res.errors), f"预期报 S3 错误，实际: {res.errors}"


def test_parallel_interlock_contact_rejected_by_s4(make_project):
    """正反转互锁常闭触点放在并联支路上时，无法真正互锁，S4 必须报错"""
    nw1 = build_parallel_then_series_network(
        1,
        "NW1 正转并联互锁错误",
        "【功能】反转常闭互锁被放进并联支路",
        branch_a={"name": "SB1_1号电机正转启动", "negated": False},
        branch_b={"name": "KM2_1号电机反转", "negated": True},
        series_after=[
            {"name": "SB3_1号电机停止", "negated": False},
            {"name": "FR1_1号电机热继", "negated": False},
            {"name": "M_安全条件满足", "negated": False},
        ],
        coil_var="KM1_1号电机正转",
    )
    nw2 = build_latch_network(
        2,
        "NW2 反转正常回路",
        "【功能】反转串入 KM1 常闭互锁",
        start_var="SB2_1号电机反转启动",
        latch_var="KM2_1号电机反转",
        stop_contacts=[
            {"name": "SB3_1号电机停止", "negated": False},
            {"name": "FR1_1号电机热继", "negated": False},
            {"name": "KM1_1号电机正转", "negated": True},
            {"name": "M_安全条件满足", "negated": False},
        ],
        coil_var="KM2_1号电机反转",
    )
    xml_dir = make_project(fc2_networks=[nw1, nw2])
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("S4" in e for e in res.errors), f"预期报 S4 错误，实际: {res.errors}"


def test_parallel_thermal_relay_rejected_by_s5(make_project):
    """热继 FR1 放在并联支路上时，热继跳闸不能停机，S5 必须报错"""
    # 让 FC1 不含 FR1，这样 M_安全条件满足 不间接携带 FR1
    fc1_nws = [
        build_simple_series_network(
            1,
            "NW1 急停扫描",
            "【功能】急停允许",
            [{"name": "SB0_急停按钮", "negated": False}],
            "M_安全条件满足",
        )
    ]
    nw1 = build_parallel_then_series_network(
        1,
        "NW1 热继并联错误",
        "【功能】热继误放并联支路",
        branch_a={"name": "SB1_1号电机正转启动", "negated": False},
        branch_b={"name": "FR1_1号电机热继", "negated": False},
        series_after=[
            {"name": "SB3_1号电机停止", "negated": False},
            {"name": "M_安全条件满足", "negated": False},
        ],
        coil_var="KM1_1号电机正转",
    )
    xml_dir = make_project(fc1_networks=fc1_nws, fc2_networks=[nw1])
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("S5" in e for e in res.errors), f"预期报 S5 错误，实际: {res.errors}"


def test_safety_input_negated_in_enable_rung_rejected_by_s2(make_project):
    """NC 安全输入在使能控制回路中被写成常闭触点 (Negated) 时，S2 必须报错"""
    nw1 = build_simple_series_network(
        1,
        "NW1 急停极性错误",
        "【功能】急停误用常闭触点",
        [
            {"name": "SB0_急停按钮", "negated": True},
            {"name": "M_安全条件满足", "negated": False},
        ],
        "KM1_1号电机正转",
    )
    xml_dir = make_project(fc2_networks=[nw1])
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("S2" in e for e in res.errors)


def test_missing_interlock_rejected_by_s4(make_project):
    """正反转同块内缺少任意一侧常闭互锁时，S4 必须报错"""
    nw1 = build_latch_network(
        1,
        "NW1 正转缺互锁",
        "【功能】正转未串反转常闭",
        start_var="SB1_1号电机正转启动",
        latch_var="KM1_1号电机正转",
        stop_contacts=[
            {"name": "SB3_1号电机停止", "negated": False},
            {"name": "M_安全条件满足", "negated": False},
        ],
        coil_var="KM1_1号电机正转",
    )
    nw2 = build_latch_network(
        2,
        "NW2 反转有互锁",
        "【功能】反转串正转常闭",
        start_var="SB2_1号电机反转启动",
        latch_var="KM2_1号电机反转",
        stop_contacts=[
            {"name": "SB3_1号电机停止", "negated": False},
            {"name": "KM1_1号电机正转", "negated": True},
            {"name": "M_安全条件满足", "negated": False},
        ],
        coil_var="KM2_1号电机反转",
    )
    xml_dir = make_project(fc2_networks=[nw1, nw2])
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("S4" in e for e in res.errors)


def test_missing_thermal_relay_rejected_by_s5(make_project):
    """存在热继输入但电机线圈未直接或间接串入热继时，S5 必须报错"""
    fc1_nws = [
        build_simple_series_network(
            1,
            "NW1 急停允许",
            "【功能】不含热继",
            [{"name": "SB0_急停按钮", "negated": False}],
            "M_安全条件满足",
        )
    ]
    nw1 = build_simple_series_network(
        1,
        "NW1 电机缺热继",
        "【功能】无热继串联",
        [
            {"name": "SB1_1号电机正转启动", "negated": False},
            {"name": "M_安全条件满足", "negated": False},
        ],
        "KM1_1号电机正转",
    )
    xml_dir = make_project(fc1_networks=fc1_nws, fc2_networks=[nw1])
    res = validate_project_xmls(str(xml_dir))
    assert not res.ok
    assert any("S5" in e for e in res.errors)


def test_star_delta_transitive_safety_and_fr_passes(make_project):
    """星三角电路中，主接触器串入安全条件后，串联了主接触器常开的星/角接触器应传递通过 S3/S5"""
    io_tags = [
        t for t in default_io_tags()
        if t["name"] not in ("SB2_1号电机反转启动", "KM1_1号电机正转", "KM2_1号电机反转")
    ] + [
        {"name": "KM1_主接触器", "data_type": "Bool", "logical_address": "%Q0.0", "comment": "[2.1 | -KM1 | Q0.0/201] 主接触器(NO)"},
        {"name": "KM2_星形接触器", "data_type": "Bool", "logical_address": "%Q0.1", "comment": "[2.2 | -KM2 | Q0.1/202] 星形接触器(NO)"},
        {"name": "KM3_角形接触器", "data_type": "Bool", "logical_address": "%Q0.2", "comment": "[2.3 | -KM3 | Q0.2/203] 角形接触器(NO)"},
    ]
    m_tags = [
        {"name": "M_急停正常", "data_type": "Bool", "logical_address": "%M10.0", "comment": "[FC1/NW1] 急停正常"},
        {"name": "M_安全条件满足", "data_type": "Bool", "logical_address": "%M10.1", "comment": "[FC1/NW2] 安全条件满足"},
        {"name": "M_换接完成", "data_type": "Bool", "logical_address": "%M10.2", "comment": "[FC2/NW2] 星三角切换标志"},
    ]
    nw1 = build_latch_network(
        1,
        "NW1 主接触器 KM1",
        "【功能】主接触器启保停",
        start_var="SB1_1号电机正转启动",
        latch_var="KM1_主接触器",
        stop_contacts=[
            {"name": "SB3_1号电机停止", "negated": False},
            {"name": "M_安全条件满足", "negated": False},
        ],
        coil_var="KM1_主接触器",
    )
    nw2 = build_simple_series_network(
        2,
        "NW2 星形接触器 KM2",
        "【功能】串联 KM1 与 KM3 常闭",
        [
            {"name": "KM1_主接触器", "negated": False},
            {"name": "M_换接完成", "negated": True},
            {"name": "KM3_角形接触器", "negated": True},
        ],
        "KM2_星形接触器",
    )
    nw3 = build_simple_series_network(
        3,
        "NW3 角形接触器 KM3",
        "【功能】串联 KM1 与 KM2 常闭",
        [
            {"name": "KM1_主接触器", "negated": False},
            {"name": "M_换接完成", "negated": False},
            {"name": "KM2_星形接触器", "negated": True},
        ],
        "KM3_角形接触器",
    )
    # 写 M_换接完成 避免未使用但保留在逻辑里
    nw4 = build_simple_series_network(
        4,
        "NW4 模拟切换条件",
        "【功能】切换条件占位",
        [{"name": "KM1_主接触器", "negated": False}],
        "M_换接完成",
    )
    xml_dir = make_project(io_tags=io_tags, m_tags=m_tags, fc2_networks=[nw1, nw2, nw3, nw4])
    res = validate_project_xmls(str(xml_dir))
    assert res.ok, f"星三角传递覆盖应通过校验，实际错误: {res.errors}"
