# -*- coding: utf-8 -*-
"""
模块名称: delivery_builder
功能说明: 负责生成规范的《现场调试交付三表》Excel (05_现场调试交付三表.xlsx) 及 Markdown 预览文件。
         包含四个工作表：
         1. 首页 Sheet「说明」 (项目信息、安全声明、待确认清单)
         2. 表一 Sheet「I_O打点表」 (通电前冷态与动作态打点测试，严格区分 DI/DO/AI/AQ)
         3. 表二 Sheet「单机空载调试步骤」 (分阶段试车流程)
         4. 表三 Sheet「联锁排障速查表」 (常见故障速查)
符合规范: eplan-to-tia-lad SKILL.md 阶段 5, references/08_调试交付三表模板.md
"""

from datetime import datetime
import os
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from typing import List, Dict, Any


def _build_io_test_guidance(addr: str, contact_type: str, for_markdown: bool = False) -> tuple:
    """根据 PLC 地址类型（%I/%Q/%IW/%QW）与触点极性生成准确的打点测试方法与预期现象"""
    addr_u = (addr or "").strip().upper()
    if addr_u.startswith("%IW") or addr_u.startswith("%ID"):
        return (
            "使用信号发生器注入 4~20mA / 0~10V 标准信号，在博途监控表观测通道原始值",
            f"0% 量程对应 0，100% 量程对应 27648（{addr} 数值随输入线性变化）",
        )
    if addr_u.startswith("%QW") or addr_u.startswith("%QD"):
        return (
            "【断开主动力电源】在博途监控表中分别向通道写入 0 / 13824 / 27648，万用表实测输出",
            f"万用表测量 {addr} 对应模拟量端子输出 0%/50%/100% 物理信号",
        )
    if addr_u.startswith("%Q"):
        return (
            f"【先断开主回路电源，仅保留控制电源】在博途监控表中强制 {addr} 置 1 → 置 0",
            f"强制为 1 时 PLC {addr} 灯亮，对应接触器/电磁阀/指示灯动作；置 0 时可靠释放",
        )
    # 默认 %I 数字量输入
    if contact_type == "NC":
        if for_markdown:
            exp = f"常态 {addr} 指示灯**常亮** (1) → 动作时**熄灭** (0)"
        else:
            exp = f"常态 {addr} 指示灯常亮 (1) → 动作时熄灭 (0)"
        return (
            "常态不动作 → 手动触发动作（按下急停/停止/拨动热继 TEST/触发限位）",
            exp,
        )
    if for_markdown:
        exp = f"常态 {addr} 指示灯**灭** (0) → 动作时**点亮** (1)"
    else:
        exp = f"常态 {addr} 指示灯灭 (0) → 动作时点亮 (1)"
    return (
        "常态不动作 → 手动触发动作（按下按钮/金属片靠近传感器/到位触发）",
        exp,
    )


def create_delivery_tables(project_info: Dict[str, Any],
                           io_list: List[Dict[str, Any]],
                           out_xlsx: str,
                           out_md: str,
                           fc_list: List[Dict[str, Any]] = None):
    """生成交付三表 Excel 与 Markdown 预览"""
    fc_list = fc_list or []

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # 样式配置
    font_title = Font(name="微软雅黑", size=14, bold=True, color="004C87")
    font_header = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
    font_normal = Font(name="微软雅黑", size=9)
    font_bold = Font(name="微软雅黑", size=9, bold=True)
    font_alert = Font(name="微软雅黑", size=9, bold=True, color="C00000")

    fill_header = PatternFill(start_color="004C87", end_color="004C87", fill_type="solid")
    fill_alt = PatternFill(start_color="F2F6F9", end_color="F2F6F9", fill_type="solid")
    fill_alert = PatternFill(start_color="FDE8E8", end_color="FDE8E8", fill_type="solid")

    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)
    border_thin = Side(style="thin", color="D0D0D0")
    border_all = Border(left=border_thin, right=border_thin, top=border_thin, bottom=border_thin)

    # =========================================================================
    # 1. 首页 Sheet「说明」
    # =========================================================================
    ws_intro = wb.create_sheet(title="说明")
    ws_intro.views.sheetView[0].showGridLines = True

    pending_items = project_info.get("pending_items", "无未闭环待确认项（若有请在现场复核后补充）")
    default_date = datetime.now().strftime("%Y-%m-%d")
    io_count = len(io_list)
    io_end_row = io_count + 1 if io_count > 0 else 2

    intro_data = [
        ["西门子博途自动化工程交付说明书", ""],
        ["项目名称", project_info.get("project_name", "自动化控制项目")],
        ["编制日期", project_info.get("date", default_date)],
        ["控制器硬件", project_info.get("plc_model", "SIMATIC S7-1200 (CPU 1214C DC/DC/DC)")],
        ["图纸版本", project_info.get("schematic_ver", "V1.0")],
        ["程序版本", "V1.0 (纯 LAD 梯形图)"],
        ["", ""],
        ["🔴 重大安全问题清单与电气隐患告警", ""],
        ["硬件互锁状态", project_info.get("hardware_interlock", "图纸中接触器回路已配置电气硬件常闭互锁")],
        ["急停控制接法", "急停按钮采用物理常闭 (NC) 接线，硬线串联切断总接触器回路"],
        ["", ""],
        ["【安全责任免责声明】", ""],
        ["声明条款", "1. 本 PLC 程序中的急停与联锁逻辑为辅助安全保护，绝对不能替代外部物理硬接线急停回路与专用安全继电器！"],
        ["", "2. 本控制系统不属于安全型故障安全 PLC (Fail-Safe F-CPU) 系统，现场通电调试前必须由持有电工特种作业操作证人员完成冷态硬接线复核。"],
        ["", ""],
        ["【待确认事项清单 (未闭环项)】", pending_items],
        ["", ""],
        ["📊 现场调试进度自动统计看板（Excel 公式动态联动）", ""],
        ["物理 I/O 打点总点数", f"=COUNTA('I_O打点表'!B2:B{io_end_row})" if io_count > 0 else 0],
        ["I/O 打点已通过数 (√)", f"=COUNTIF('I_O打点表'!K2:K{io_end_row},\"√\")" if io_count > 0 else 0],
        ["I/O 打点异常数 (×)", f"=COUNTIF('I_O打点表'!K2:K{io_end_row},\"×\")" if io_count > 0 else 0],
        ["I/O 打点通过率", f"=IFERROR(COUNTIF('I_O打点表'!K2:K{io_end_row},\"√\")/COUNTA('I_O打点表'!B2:B{io_end_row}),0)" if io_count > 0 else 0],
    ]

    for row in intro_data:
        ws_intro.append(row)

    ws_intro.merge_cells("A1:B1")
    cell_a1 = ws_intro["A1"]
    cell_a1.font = font_title
    cell_a1.alignment = align_center
    ws_intro.row_dimensions[1].height = 36

    ws_intro.column_dimensions["A"].width = 28
    ws_intro.column_dimensions["B"].width = 75

    for r in range(2, len(intro_data) + 1):
        ws_intro.row_dimensions[r].height = 24
        c1 = ws_intro.cell(row=r, column=1)
        c2 = ws_intro.cell(row=r, column=2)
        c1.font = font_bold
        c2.font = font_normal
        if "安全" in str(c1.value) or "🔴" in str(c1.value):
            c1.font = font_alert
            c1.fill = fill_alert

    ws_intro.page_setup.orientation = ws_intro.ORIENTATION_LANDSCAPE
    ws_intro.page_setup.paperSize = ws_intro.PAPERSIZE_A4

    # =========================================================================
    # 2. 表一 Sheet「I_O打点表」
    # =========================================================================
    ws_io = wb.create_sheet(title="I_O打点表")
    ws_io.views.sheetView[0].showGridLines = True

    io_headers = [
        "序号", "PLC地址", "变量名", "元器件代号", "图纸位置",
        "端子/线号", "NO/NC", "信号说明", "测试方法", "预期现象",
        "实测结果(√/×)", "调试人", "备注"
    ]
    ws_io.append(io_headers)
    ws_io.row_dimensions[1].height = 28

    for col_idx in range(1, len(io_headers) + 1):
        c = ws_io.cell(row=1, column=col_idx)
        c.font = font_header
        c.fill = fill_header
        c.alignment = align_center
        c.border = border_all

    for idx, item in enumerate(io_list, start=1):
        addr = item.get("logical_address", "")
        name = item.get("name", "")
        device = item.get("device_tag", "")
        pos = item.get("page_col", "")
        terminal = item.get("terminal", "")
        contact_type = item.get("contact_type", "NO")
        desc = item.get("description", "")

        method, expected = _build_io_test_guidance(addr, contact_type, for_markdown=False)

        # 实测结果与调试人严格留空，等待现场实际打点后手工填写
        row_data = [
            idx, addr, name, device, pos, terminal, contact_type,
            desc, method, expected, "", "", ""
        ]
        ws_io.append(row_data)
        curr_row = idx + 1
        ws_io.row_dimensions[curr_row].height = 26
        is_even = (curr_row % 2 == 0)

        for c_idx in range(1, len(row_data) + 1):
            cell = ws_io.cell(row=curr_row, column=c_idx)
            cell.font = font_normal
            cell.border = border_all
            if is_even:
                cell.fill = fill_alt
            if c_idx in [1, 2, 4, 5, 6, 7, 11, 12]:
                cell.alignment = align_center
            else:
                cell.alignment = align_left

    if io_count > 0:
        summary_row = io_count + 2
        ws_io.append([
            "汇总",
            f"=COUNTA(B2:B{io_end_row})",
            "以上为有效物理 I/O 点位（含公式实时统计）",
            "", "", "", "", "", "",
            "通过(√) / 异常(×)",
            f"=COUNTIF(K2:K{io_end_row},\"√\")",
            f"=COUNTIF(K2:K{io_end_row},\"×\")",
            f"=IFERROR(COUNTIF(K2:K{io_end_row},\"√\")/COUNTA(B2:B{io_end_row}),0)",
        ])
        ws_io.row_dimensions[summary_row].height = 24
        for c_idx in range(1, len(io_headers) + 1):
            sc = ws_io.cell(row=summary_row, column=c_idx)
            sc.font = font_bold
            sc.fill = fill_alt
            sc.border = border_all
            sc.alignment = align_center if c_idx in (1, 2, 11, 12, 13) else align_left
        ws_io.cell(row=summary_row, column=13).number_format = "0.0%"

    for col in ws_io.columns:
        col_letter = get_column_letter(col[0].column)
        max_l = max([len(str(c.value or '').encode('gbk', errors='ignore')) for c in col])
        ws_io.column_dimensions[col_letter].width = min(max(max_l + 3, 11), 46)
    ws_io.freeze_panes = "A2"
    ws_io.auto_filter.ref = f"A1:M{io_end_row}"
    ws_io.page_setup.orientation = ws_io.ORIENTATION_LANDSCAPE
    ws_io.page_setup.paperSize = ws_io.PAPERSIZE_A4
    ws_io.print_title_rows = "1:1"

    # =========================================================================
    # 3. 表二 Sheet「单机空载调试步骤」
    # =========================================================================
    ws_step = wb.create_sheet(title="单机空载调试步骤")
    ws_step.views.sheetView[0].showGridLines = True

    step_headers = ["阶段", "步骤号", "调试作业内容", "操作指引与技术标准", "安全防范措施", "实测结果(√/×)", "确认签字"]
    ws_step.append(step_headers)
    ws_step.row_dimensions[1].height = 28

    for col_idx in range(1, len(step_headers) + 1):
        c = ws_step.cell(row=1, column=col_idx)
        c.font = font_header
        c.fill = fill_header
        c.alignment = align_center
        c.border = border_all

    steps_data = [
        ["阶段一：断电绝缘检查", "1.1", "主回路相间与对地绝缘", "使用 500V 兆欧表测量电机主进线及相间绝缘阻抗 >= 5MΩ", "切断主配电箱电源开关并上锁挂牌 (LOTO)", "", ""],
        ["阶段一：断电绝缘检查", "1.2", "保护接地 PE 通断复核", "测量各电机金属外壳、电磁阀地线与控制柜 PE 铜排导通电阻 <= 0.1Ω", "严禁带电测量接地电阻", "", ""],
        ["阶段二：控制回路通电", "2.1", "控制电源电压校准", "合上 DC 24V 开关电源空开，实测直流输出电压应在 23.8V ~ 24.5V 范围内", "确保动力主回路空开处于断开状态", "", ""],
        ["阶段二：控制回路通电", "2.2", "急停与安全回路闭锁", "逐个按下操作台与现场急停按钮，验证 PLC 对应输入变 0，切断所有运行准备标志", "核查急停复位后必须按启动按钮方可再启动", "", ""],
        ["阶段三：手动微点动试车", "3.1", "电机转向与相序核验", "合上动力断路器，切换至手动模式瞬间点动电机 0.5s，观察转动方向与机械标识一致", "电机脱开机械负载，人员远离传动机构", "", ""],
        ["阶段三：手动微点动试车", "3.2", "双向互锁物理切断核对", "在正转接触器吸合时手动用绝缘棒轻推反转接触器，反转线圈应被硬件常闭触点彻底切断", "严禁金属物件接触带电部位", "", ""],
        ["阶段四：自动时序测试", "4.1", "降压/换向延时时序观测", "启动自动流程，在博途启用监视实时查看 TON 定时器经过时间 ET 递增至预设值", "现场专人手持急停按钮监视试车", "", ""],
        ["阶段四：自动时序测试", "4.2", "热继脱扣动作模拟", "按下热继电器 TEST 拨片，验证 PLC 瞬间报警并无延时复位接触器线圈", "测试完毕及时按复位按钮复位热继", "", ""]
    ]

    for fc_idx, fc in enumerate(fc_list, start=1):
        fc_name = fc.get("name", f"FC{fc_idx}")
        nw_summaries = []
        for nw in fc.get("networks", []):
            coils_str = "、".join(nw.get("coils", []))
            timers_str = "、".join(nw.get("timers", []))
            desc_part = nw.get("title", f"程序段{nw.get('index', 1)}")
            if coils_str:
                desc_part += f" (输出: {coils_str})"
            if timers_str:
                desc_part += f" [定时: {timers_str}]"
            nw_summaries.append(desc_part)
        if nw_summaries:
            steps_data.append([
                "阶段五：程序块专项联调",
                f"5.{fc_idx}",
                f"功能块 {fc_name} 逻辑验证",
                "逐段核验：" + "；".join(nw_summaries[:6]),
                "在线监控对应程序段能流与输出状态",
                "",
                ""
            ])

    for s_idx, s_row in enumerate(steps_data, start=2):
        ws_step.append(s_row)
        ws_step.row_dimensions[s_idx].height = 28
        is_even = (s_idx % 2 == 0)
        for col_i in range(1, len(s_row) + 1):
            cell = ws_step.cell(row=s_idx, column=col_i)
            cell.font = font_normal
            cell.border = border_all
            if is_even:
                cell.fill = fill_alt
            if col_i in [2, 6, 7]:
                cell.alignment = align_center
            else:
                cell.alignment = align_left

    for col in ws_step.columns:
        col_letter = get_column_letter(col[0].column)
        max_l = max([len(str(c.value or '').encode('gbk', errors='ignore')) for c in col])
        ws_step.column_dimensions[col_letter].width = min(max(max_l + 3, 12), 50)
    step_end_row = len(steps_data) + 1
    ws_intro.append(["单机空载调试总步数", f"=COUNTA('单机空载调试步骤'!B2:B{step_end_row})"])
    ws_intro.append(["单机空载调试通过率", f"=IFERROR(COUNTIF('单机空载调试步骤'!F2:F{step_end_row},\"√\")/COUNTA('单机空载调试步骤'!B2:B{step_end_row}),0)"])

    ws_step.freeze_panes = "A2"
    ws_step.auto_filter.ref = ws_step.dimensions
    ws_step.page_setup.orientation = ws_step.ORIENTATION_LANDSCAPE
    ws_step.page_setup.paperSize = ws_step.PAPERSIZE_A4
    ws_step.print_title_rows = "1:1"

    # =========================================================================
    # 4. 表三 Sheet「联锁排障速查表」
    # =========================================================================
    ws_alarm = wb.create_sheet(title="联锁排障速查表")
    ws_alarm.views.sheetView[0].showGridLines = True

    alarm_headers = ["故障代号", "报警信息描述", "PLC 触发逻辑条件", "常见电气 / 机械原因", "现场排查与应急处置步骤", "复位方法"]
    ws_alarm.append(alarm_headers)
    ws_alarm.row_dimensions[1].height = 28

    for col_idx in range(1, len(alarm_headers) + 1):
        c = ws_alarm.cell(row=1, column=col_idx)
        c.font = font_header
        c.fill = fill_header
        c.alignment = align_center
        c.border = border_all

    alarms_data = [
        ["ERR_01", "紧急停止回路动作断开", "SB0_急停按钮 为 FALSE (0)", "1. 现场急停蘑菇头被拍下；\n2. 急停电缆松脱被拉扯断线；\n3. 控制回路 DC24V 保险烧断。", "1. 巡检现场所有急停按钮并顺时针旋转释放；\n2. 万用表测量端子对地电压；\n3. 检查安全回路熔断器。", "旋转释放急停后按控制台复位钮"],
        ["ERR_02", "电机热继电器过载脱扣", "FR_电机热继 为 FALSE (0)", "1. 机械轴承抱死或物料卡阻；\n2. 热继电流整定值过小；\n3. 动力电网缺相或电压严重不平衡。", "1. 手动盘动电机转子确认阻力；\n2. 钳形表测量三相运转电流；\n3. 校验热继电流整定拨盘刻度。", "双金属片冷却3分钟后按蓝色Reset"],
        ["ERR_03", "接触器吸合反馈超时", "Q 输出置位 3s 无输入反馈", "1. 接触器辅助常开触头氧化接触不良；\n2. 接触器驱动线圈开路或供电跌落；\n3. 中间继电器损坏。", "1. 观察接触器机械衔铁是否真正吸合；\n2. 测量辅助常开触点导通电阻；\n3. 检查线圈 A1-A2 控制接线。", "排除故障后按控制台复位钮"],
        ["ERR_04", "机构行程超程限位保护", "SQ_极限限位 为 FALSE (0)", "1. 机械机构冲出正常工作区间撞击限位；\n2. 限位开关损坏或触臂卡死；\n3. 限位接线松脱断线。", "1. 检查机械位置是否超程；\n2. 手动回退机构离开限位开关；\n3. 测试限位开关常闭接点通断。", "机构手动退回安全区后复位"]
    ]

    err_seq = 5
    for fc in fc_list:
        fc_name = fc.get("name", "")
        for nw in fc.get("networks", []):
            coils = nw.get("coils", [])
            contacts = nw.get("contacts", [])
            if coils and contacts:
                coil_label = " / ".join(coils)
                cond_label = " + ".join(contacts[:5])
                alarms_data.append([
                    f"LK_{err_seq:02d}",
                    f"[{fc_name}] {nw.get('title', coil_label)} 联锁条件核查",
                    f"目标输出: {coil_label}；前置条件: {cond_label}",
                    f"任一前置联锁触点 ({cond_label}) 未满足或互锁常闭断开",
                    f"在博途监视 {fc_name} 程序段 {nw.get('index', 1)}，逐个核对 {cond_label} 的实时布尔状态",
                    "满足联锁条件或排除故障后按复位"
                ])
                err_seq += 1

    for a_idx, a_row in enumerate(alarms_data, start=2):
        ws_alarm.append(a_row)
        ws_alarm.row_dimensions[a_idx].height = 42
        is_even = (a_idx % 2 == 0)
        for col_i in range(1, len(a_row) + 1):
            cell = ws_alarm.cell(row=a_idx, column=col_i)
            cell.font = font_normal
            cell.border = border_all
            if is_even:
                cell.fill = fill_alt
            if col_i in [1, 6]:
                cell.alignment = align_center
            else:
                cell.alignment = align_left

    for col in ws_alarm.columns:
        col_letter = get_column_letter(col[0].column)
        max_l = max([len(str(c.value or '').encode('gbk', errors='ignore')) for c in col])
        ws_alarm.column_dimensions[col_letter].width = min(max(max_l + 3, 14), 50)
    alarm_end_row = len(alarms_data) + 1
    ws_intro.append(["联锁排障速查条目数", f"=COUNTA('联锁排障速查表'!A2:A{alarm_end_row})"])
    for r_i in range(len(intro_data) - 3, ws_intro.max_row + 1):
        c1 = ws_intro.cell(row=r_i, column=1)
        c2 = ws_intro.cell(row=r_i, column=2)
        c1.font = font_bold
        c2.font = font_normal
        ws_intro.row_dimensions[r_i].height = 22
        if "率" in str(c1.value):
            c2.number_format = "0.0%"

    ws_alarm.freeze_panes = "A2"
    ws_alarm.auto_filter.ref = ws_alarm.dimensions
    ws_alarm.page_setup.orientation = ws_alarm.ORIENTATION_LANDSCAPE
    ws_alarm.page_setup.paperSize = ws_alarm.PAPERSIZE_A4
    ws_alarm.print_title_rows = "1:1"

    os.makedirs(os.path.dirname(os.path.abspath(out_xlsx)), exist_ok=True)
    wb.save(out_xlsx)

    # =========================================================================
    # 生成 Markdown 预览文件
    # =========================================================================
    md_lines = [
        f"# 《现场调试交付三表》—— {project_info.get('project_name', '工程项目')}",
        "",
        "> **工程控制器**：" + project_info.get("plc_model", "S7-1200") + "  ",
        "> **编制日期**：" + project_info.get("date", default_date) + "  ",
        "> **程序版本**：纯 LAD 梯形图 V1.0  ",
        "> **安全声明**：PLC 程序中的急停逻辑**不能替代**硬接线急停回路和安全继电器；本程序不属于安全 PLC（F-CPU）程序。",
        "",
        "---",
        "",
        "## 表一：《I/O 静态打点核对表》",
        "",
        "| 序号 | PLC地址 | 变量名 | 元器件代号 | 图纸位置 | 端子/线号 | NO/NC | 信号说明 | 测试方法与预期现象 | 实测结果 |",
        "| :---: | :---: | :--- | :---: | :---: | :---: | :---: | :--- | :--- | :---: |"
    ]

    for idx, item in enumerate(io_list, start=1):
        addr = item.get("logical_address", "")
        ct = item.get("contact_type", "NO")
        method, expected = _build_io_test_guidance(addr, ct, for_markdown=True)
        md_lines.append(
            f"| {idx} | `{addr}` | `{item.get('name', '')}` | `{item.get('device_tag', '')}` | "
            f"`{item.get('page_col', '')}` | `{item.get('terminal', '')}` | **{ct}** | {item.get('description', '')} | "
            f"{method}；预期：{expected} | □ 待测 |"
        )

    md_lines.extend([
        "",
        "---",
        "",
        "## 表二：《单机空载调试步骤》（摘要）",
        r"1. **绝缘电阻测量**：使用兆欧表核对相间绝缘阻抗 $\ge 5\,\text{M}\Omega$；",
        "2. **急停与热继闭锁测试**：通电后拍下急停，验证 PLC 模块指示灯熄灭，程序切断接触器输出；",
        "3. **手动微点动转向校对**：电机脱开机械负载后瞬间微点动 0.5 秒确认旋转方向；",
        "4. **自动延时逻辑观测**：在博途启用监视实时查看 TON 定时器当前经过时间 ET 跳动至预设值。",
    ])
    for fc_idx, fc in enumerate(fc_list, start=5):
        fc_name = fc.get("name", "")
        nw_titles = "、".join([nw.get("title", "") for nw in fc.get("networks", []) if nw.get("title")])
        if nw_titles:
            md_lines.append(f"{fc_idx}. **程序块 `{fc_name}` 专项联调**：逐段验证 {nw_titles}。")

    md_lines.extend([
        "",
        "---",
        "",
        "## 表三：《联锁排障速查表》（摘要）",
        "| 故障代号 | 报警信息描述 | 触发条件 | 常见原因 | 排查步骤 |",
        "| :---: | :--- | :--- | :--- | :--- |",
        "| **ERR_01** | 急停回路动作断开 | `SB0_急停按钮 = 0` | 现场蘑菇头被拍下、断线 | 顺时针旋转释放急停并按复位钮 |",
        "| **ERR_02** | 电机热继电器过载脱扣 | `FR_电机热继 = 0` | 机械卡阻、整定值偏小、缺相 | 手动盘车排查阻力，冷却后按蓝色Reset |",
        "| **ERR_03** | 接触器动作超时故障 | 输出 3s 无输入反馈 | 辅助触点氧化、线圈接线松脱 | 检查衔铁机械吸合状态与辅助端子电阻 |",
    ])
    md_err_seq = 5
    for fc in fc_list:
        fc_name = fc.get("name", "")
        for nw in fc.get("networks", []):
            coils = nw.get("coils", [])
            contacts = nw.get("contacts", [])
            if coils and contacts:
                c_str = " / ".join(coils)
                k_str = ", ".join(contacts[:4])
                md_lines.append(
                    f"| **LK_{md_err_seq:02d}** | `[{fc_name}]` {nw.get('title', c_str)} | 输出 `{c_str}` 依赖 `{k_str}` | 前置触点未满足或互锁闭锁 | 在线监控 `{fc_name}` 程序段 {nw.get('index', 1)} 触点状态 |"
                )
                md_err_seq += 1
    md_lines.append("")

    os.makedirs(os.path.dirname(os.path.abspath(out_md)), exist_ok=True)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
