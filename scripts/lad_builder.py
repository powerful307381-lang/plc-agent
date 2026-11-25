# -*- coding: utf-8 -*-
"""
模块名称: lad_builder
功能说明: 基于导出的官方样板 XML 执行“克隆 + 填充”，纯梯形图生成器：
         1. 生成业务功能块 FC 的 LAD XML (纯中文标题与中文调试注释)
         2. 生成全局定时器 DB 的 XML
         3. 生成主组织块 Main (OB1) 的 LAD XML (顺序调用各个 FC)
符合规范: eplan-to-tia-lad SKILL.md 阶段 3, references/05, references/06
"""

import os
from typing import List, Dict, Any, Optional, Union, Tuple

try:
    from common_utils import escape_xml as _escape_xml, write_utf8_bom_crlf
except ImportError:
    from scripts.common_utils import escape_xml as _escape_xml, write_utf8_bom_crlf  # type: ignore


def _normalize_contact(c: Any) -> Dict[str, Any]:
    """兼容 dict {'name': ..., 'negated': ...} 与元组 (name, negated) 写法"""
    if isinstance(c, dict):
        return c
    if isinstance(c, (tuple, list)) and len(c) >= 1:
        return {
            "name": str(c[0]),
            "negated": bool(c[1]) if len(c) > 1 else False,
            "scope": str(c[2]) if len(c) > 2 else "GlobalVariable",
        }
    return {"name": str(c), "negated": False}


class LadNetworkBuilder:
    """单个梯形图程序段 (Network / CompileUnit) 构建器"""

    def __init__(self, nw_index: int, title: str, comment: str):
        self.nw_index = nw_index
        self.title = title
        self.comment = comment
        self.parts = []
        self.wires = []
        self.uid_counter = 21

    def alloc_uid(self) -> str:
        uid = str(self.uid_counter)
        self.uid_counter += 1
        return uid

    def add_access(self, var_name: str, scope: str = "GlobalVariable") -> str:
        """添加变量访问 Access，自动将带 '.' 的成员路径（如 全局定时器.T1.Q）拆分为多层 Component"""
        uid = self.alloc_uid()
        parts_seg = [seg.strip() for seg in str(var_name).split(".") if seg.strip()]
        if not parts_seg:
            parts_seg = [str(var_name)]
        comps_xml = "".join(f'<Component Name="{_escape_xml(seg)}" />' for seg in parts_seg)
        self.parts.append(
            f'    <Access Scope="{_escape_xml(scope)}" UId="{uid}"><Symbol>{comps_xml}</Symbol></Access>'
        )
        return uid

    def add_typed_time_constant(self, time_val: str) -> str:
        """添加时间类型常量 Access (TypedConstant)"""
        uid = self.alloc_uid()
        self.parts.append(
            f'    <Access Scope="TypedConstant" UId="{uid}"><Constant><ConstantValue>{_escape_xml(time_val)}</ConstantValue></Constant></Access>'
        )
        return uid

    def add_literal_constant(self, const_type: str, val: Any) -> str:
        """添加指定类型字面常量 Access (如 Int / Real)"""
        uid = self.alloc_uid()
        self.parts.append(
            f'    <Access Scope="LiteralConstant" UId="{uid}"><Constant><ConstantType>{_escape_xml(const_type)}</ConstantType><ConstantValue>{_escape_xml(val)}</ConstantValue></Constant></Access>'
        )
        return uid

    def add_literal_int_constant(self, val: int) -> str:
        """添加整型字面常量 Access"""
        return self.add_literal_constant("Int", val)


def build_simple_series_network(nw_index: int, title: str, comment: str,
                                contacts: List[Dict[str, Any]], coil_var: str, coil_type: str = "Coil",
                                coil_scope: str = "GlobalVariable") -> str:
    """
    构建标准的串联回路程序段 (支持常开/常闭触点 + 线圈/置位/复位)
    contacts: [{'name': 'SB1_启动', 'negated': False}, {'name': 'SB2_停止', 'negated': True}]
    """
    builder = LadNetworkBuilder(nw_index, title, comment)
    
    access_uids = []
    contact_uids = []
    
    for raw_c in contacts:
        c = _normalize_contact(raw_c)
        acc_uid = builder.add_access(c['name'], c.get('scope', 'GlobalVariable'))
        part_uid = builder.alloc_uid()
        access_uids.append(acc_uid)
        contact_uids.append(part_uid)
        if c.get('negated', False):
            builder.parts.append(f'    <Part Name="Contact" UId="{part_uid}"><Negated Name="operand" /></Part>')
        else:
            builder.parts.append(f'    <Part Name="Contact" UId="{part_uid}" />')
            
    # 线圈
    coil_acc_uid = builder.add_access(coil_var, coil_scope)
    coil_uid = builder.alloc_uid()
    builder.parts.append(f'    <Part Name="{_escape_xml(coil_type)}" UId="{coil_uid}" />')
    
    # 导线连接
    # 1. 电源母线连到第一个触点
    wire_id = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{wire_id}"><Powerrail /><NameCon UId="{contact_uids[0]}" Name="in" /></Wire>')
    
    # 2. 触点与变量绑定
    for idx, (p_uid, a_uid) in enumerate(zip(contact_uids, access_uids)):
        w_id = builder.alloc_uid()
        builder.wires.append(f'    <Wire UId="{w_id}"><IdentCon UId="{a_uid}" /><NameCon UId="{p_uid}" Name="operand" /></Wire>')
        if idx < len(contact_uids) - 1:
            next_p_uid = contact_uids[idx + 1]
            w_chain = builder.alloc_uid()
            builder.wires.append(f'    <Wire UId="{w_chain}"><NameCon UId="{p_uid}" Name="out" /><NameCon UId="{next_p_uid}" Name="in" /></Wire>')
            
    # 3. 最后一个触点连接线圈
    w_last = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_last}"><NameCon UId="{contact_uids[-1]}" Name="out" /><NameCon UId="{coil_uid}" Name="in" /></Wire>')
    
    # 4. 线圈绑定操作数
    w_coil = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_coil}"><IdentCon UId="{coil_acc_uid}" /><NameCon UId="{coil_uid}" Name="operand" /></Wire>')
    
    return render_network_xml(builder)


def build_latch_network(nw_index: int, title: str, comment: str,
                        start_var: str, latch_var: str, stop_contacts: List[Dict[str, Any]], coil_var: str,
                        pre_contacts: Optional[List[Dict[str, Any]]] = None,
                        start_scope: str = "GlobalVariable",
                        latch_scope: str = "GlobalVariable",
                        coil_scope: str = "GlobalVariable") -> str:
    """
    构建标准的启保停并联自锁回路（支持可选的前置串联触点，如星三角角形接触器的 KM1 前置触点）
    [pre_contacts →] (start_var ∥ latch_var) → stop_contacts → coil_var
    """
    builder = LadNetworkBuilder(nw_index, title, comment)

    # 可选前置串联触点
    pre_acc_uids = []
    pre_part_uids = []
    for raw_c in (pre_contacts or []):
        c = _normalize_contact(raw_c)
        acc = builder.add_access(c['name'], c.get('scope', 'GlobalVariable'))
        part = builder.alloc_uid()
        pre_acc_uids.append(acc)
        pre_part_uids.append(part)
        if c.get('negated', False):
            builder.parts.append(f'    <Part Name="Contact" UId="{part}"><Negated Name="operand" /></Part>')
        else:
            builder.parts.append(f'    <Part Name="Contact" UId="{part}" />')

    # 启动按钮与自锁触点
    start_acc = builder.add_access(start_var, start_scope)
    latch_acc = builder.add_access(latch_var, latch_scope)
    start_contact = builder.alloc_uid()
    latch_contact = builder.alloc_uid()
    builder.parts.append(f'    <Part Name="Contact" UId="{start_contact}" />')
    builder.parts.append(f'    <Part Name="Contact" UId="{latch_contact}" />')
    
    # 并联节点 O
    or_node = builder.alloc_uid()
    builder.parts.append(f'    <Part Name="O" UId="{or_node}"><TemplateValue Name="Card" Type="Cardinality">2</TemplateValue></Part>')
    
    # 串联后续触点 (停止/热继/互锁)
    stop_acc_uids = []
    stop_part_uids = []
    for raw_c in stop_contacts:
        c = _normalize_contact(raw_c)
        acc = builder.add_access(c['name'], c.get('scope', 'GlobalVariable'))
        part = builder.alloc_uid()
        stop_acc_uids.append(acc)
        stop_part_uids.append(part)
        if c.get('negated', False):
            builder.parts.append(f'    <Part Name="Contact" UId="{part}"><Negated Name="operand" /></Part>')
        else:
            builder.parts.append(f'    <Part Name="Contact" UId="{part}" />')
            
    # 线圈
    coil_acc = builder.add_access(coil_var, coil_scope)
    coil_part = builder.alloc_uid()
    builder.parts.append(f'    <Part Name="Coil" UId="{coil_part}" />')
    
    # 接线 Wires
    # 1. 前置串联触点（如有），或电源母线直接分两路并联连入启动/自锁触点
    if pre_part_uids:
        w_rail = builder.alloc_uid()
        builder.wires.append(f'    <Wire UId="{w_rail}"><Powerrail /><NameCon UId="{pre_part_uids[0]}" Name="in" /></Wire>')
        for idx, (p_uid, a_uid) in enumerate(zip(pre_part_uids, pre_acc_uids)):
            w_op = builder.alloc_uid()
            builder.wires.append(f'    <Wire UId="{w_op}"><IdentCon UId="{a_uid}" /><NameCon UId="{p_uid}" Name="operand" /></Wire>')
            if idx < len(pre_part_uids) - 1:
                w_chain = builder.alloc_uid()
                builder.wires.append(f'    <Wire UId="{w_chain}"><NameCon UId="{p_uid}" Name="out" /><NameCon UId="{pre_part_uids[idx + 1]}" Name="in" /></Wire>')
        w_split = builder.alloc_uid()
        builder.wires.append(
            f'    <Wire UId="{w_split}"><NameCon UId="{pre_part_uids[-1]}" Name="out" /><NameCon UId="{start_contact}" Name="in" /><NameCon UId="{latch_contact}" Name="in" /></Wire>'
        )
    else:
        w_rail = builder.alloc_uid()
        builder.wires.append(f'    <Wire UId="{w_rail}"><Powerrail /><NameCon UId="{start_contact}" Name="in" /><NameCon UId="{latch_contact}" Name="in" /></Wire>')
    
    # 2. 绑定两触点操作数
    w_start = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_start}"><IdentCon UId="{start_acc}" /><NameCon UId="{start_contact}" Name="operand" /></Wire>')
    w_start_out = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_start_out}"><NameCon UId="{start_contact}" Name="out" /><NameCon UId="{or_node}" Name="in1" /></Wire>')
    
    w_latch = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_latch}"><IdentCon UId="{latch_acc}" /><NameCon UId="{latch_contact}" Name="operand" /></Wire>')
    w_latch_out = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_latch_out}"><NameCon UId="{latch_contact}" Name="out" /><NameCon UId="{or_node}" Name="in2" /></Wire>')
    
    # 3. O 节点汇流输出连入第一个停止触点
    prev_out_node = or_node
    prev_out_pin = "out"
    
    for p_uid, a_uid in zip(stop_part_uids, stop_acc_uids):
        w_in = builder.alloc_uid()
        builder.wires.append(f'    <Wire UId="{w_in}"><NameCon UId="{prev_out_node}" Name="{prev_out_pin}" /><NameCon UId="{p_uid}" Name="in" /></Wire>')
        w_op = builder.alloc_uid()
        builder.wires.append(f'    <Wire UId="{w_op}"><IdentCon UId="{a_uid}" /><NameCon UId="{p_uid}" Name="operand" /></Wire>')
        prev_out_node = p_uid
        prev_out_pin = "out"
        
    # 4. 连入线圈
    w_to_coil = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_to_coil}"><NameCon UId="{prev_out_node}" Name="{prev_out_pin}" /><NameCon UId="{coil_part}" Name="in" /></Wire>')
    w_coil_op = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_coil_op}"><IdentCon UId="{coil_acc}" /><NameCon UId="{coil_part}" Name="operand" /></Wire>')

    return render_network_xml(builder)


def build_ton_timer_network(nw_index: int, title: str, comment: str,
                            in_contacts: List[Dict[str, Any]], timer_db_member: str,
                            time_pt: str = "T#5S", coil_var: Optional[str] = None, coil_type: str = "Coil",
                            coil_scope: str = "GlobalVariable") -> str:
    """
    构建内嵌图形化 TON 定时器方框指令的程序段
    in_contacts → TON ("全局定时器".timer_db_member, PT=time_pt) → coil_var (可选)
    """
    builder = LadNetworkBuilder(nw_index, title, comment)
    
    # 输入触点
    in_acc_uids = []
    in_part_uids = []
    for raw_c in in_contacts:
        c = _normalize_contact(raw_c)
        acc = builder.add_access(c['name'], c.get('scope', 'GlobalVariable'))
        part = builder.alloc_uid()
        in_acc_uids.append(acc)
        in_part_uids.append(part)
        if c.get('negated', False):
            builder.parts.append(f'    <Part Name="Contact" UId="{part}"><Negated Name="operand" /></Part>')
        else:
            builder.parts.append(f'    <Part Name="Contact" UId="{part}" />')
            
    # 时间常量 (TypedConstant)
    time_acc = builder.add_typed_time_constant(time_pt)
    
    # TON 指令元件与 Instance
    ton_part = builder.alloc_uid()
    ton_inst = builder.alloc_uid()
    
    ton_xml = f'''    <Part Name="TON" Version="1.0" UId="{ton_part}">
      <Instance Scope="GlobalVariable" UId="{ton_inst}">
        <Component Name="全局定时器" />
        <Component Name="{_escape_xml(timer_db_member)}" />
      </Instance>
      <TemplateValue Name="time_type" Type="Type">Time</TemplateValue>
    </Part>'''
    builder.parts.append(ton_xml)
    
    # 导线接线
    # 1. 电源母线连到第一个触点
    w_rail = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_rail}"><Powerrail /><NameCon UId="{in_part_uids[0]}" Name="in" /></Wire>')
    
    prev_node = in_part_uids[0]
    for idx, (p_uid, a_uid) in enumerate(zip(in_part_uids, in_acc_uids)):
        w_op = builder.alloc_uid()
        builder.wires.append(f'    <Wire UId="{w_op}"><IdentCon UId="{a_uid}" /><NameCon UId="{p_uid}" Name="operand" /></Wire>')
        if idx > 0:
            w_c = builder.alloc_uid()
            builder.wires.append(f'    <Wire UId="{w_c}"><NameCon UId="{prev_node}" Name="out" /><NameCon UId="{p_uid}" Name="in" /></Wire>')
        prev_node = p_uid
        
    # 2. 最后一个触点连接 TON 的 IN 引脚
    w_to_in = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_to_in}"><NameCon UId="{prev_node}" Name="out" /><NameCon UId="{ton_part}" Name="IN" /></Wire>')
    
    # 3. PT 设定时间连接
    w_pt = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_pt}"><IdentCon UId="{time_acc}" /><NameCon UId="{ton_part}" Name="PT" /></Wire>')
    
    # 4. ET 悬空连接 (西门子 Schema 严格要求 ET 未接线时显式指定 OpenCon)
    w_et = builder.alloc_uid()
    open_uid = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_et}"><NameCon UId="{ton_part}" Name="ET" /><OpenCon UId="{open_uid}" /></Wire>')
    
    # 5. 线圈 (支持普通线圈或复位线圈)
    if coil_var:
        coil_acc = builder.add_access(coil_var, coil_scope)
        coil_part = builder.alloc_uid()
        builder.parts.append(f'    <Part Name="{_escape_xml(coil_type)}" UId="{coil_part}" />')
        
        w_q = builder.alloc_uid()
        builder.wires.append(f'    <Wire UId="{w_q}"><NameCon UId="{ton_part}" Name="Q" /><NameCon UId="{coil_part}" Name="in" /></Wire>')
        w_coil_op = builder.alloc_uid()
        builder.wires.append(f'    <Wire UId="{w_coil_op}"><IdentCon UId="{coil_acc}" /><NameCon UId="{coil_part}" Name="operand" /></Wire>')
        
    return render_network_xml(builder)


def build_pbox_network(nw_index: int, title: str, comment: str,
                       in_contacts: List[Dict[str, Any]], edge_bit_var: str,
                       coil_var: str, coil_type: str = "Coil",
                       edge_type: str = "PBox",
                       edge_bit_scope: str = "GlobalVariable",
                       coil_scope: str = "GlobalVariable") -> str:
    """
    构建边沿检测程序段 (PBox 上升沿 / NBox 下降沿)
    in_contacts → PBox(bit=edge_bit_var) → coil_var
    """
    builder = LadNetworkBuilder(nw_index, title, comment)

    in_acc_uids = []
    in_part_uids = []
    for raw_c in in_contacts:
        c = _normalize_contact(raw_c)
        acc = builder.add_access(c['name'], c.get('scope', 'GlobalVariable'))
        part = builder.alloc_uid()
        in_acc_uids.append(acc)
        in_part_uids.append(part)
        if c.get('negated', False):
            builder.parts.append(f'    <Part Name="Contact" UId="{part}"><Negated Name="operand" /></Part>')
        else:
            builder.parts.append(f'    <Part Name="Contact" UId="{part}" />')

    edge_acc = builder.add_access(edge_bit_var, edge_bit_scope)
    edge_part = builder.alloc_uid()
    builder.parts.append(f'    <Part Name="{_escape_xml(edge_type)}" UId="{edge_part}" />')

    coil_acc = builder.add_access(coil_var, coil_scope)
    coil_part = builder.alloc_uid()
    builder.parts.append(f'    <Part Name="{_escape_xml(coil_type)}" UId="{coil_part}" />')

    w_rail = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_rail}"><Powerrail /><NameCon UId="{in_part_uids[0]}" Name="in" /></Wire>')

    prev_node = in_part_uids[0]
    for idx, (p_uid, a_uid) in enumerate(zip(in_part_uids, in_acc_uids)):
        w_op = builder.alloc_uid()
        builder.wires.append(f'    <Wire UId="{w_op}"><IdentCon UId="{a_uid}" /><NameCon UId="{p_uid}" Name="operand" /></Wire>')
        if idx > 0:
            w_c = builder.alloc_uid()
            builder.wires.append(f'    <Wire UId="{w_c}"><NameCon UId="{prev_node}" Name="out" /><NameCon UId="{p_uid}" Name="in" /></Wire>')
        prev_node = p_uid

    w_to_edge = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_to_edge}"><NameCon UId="{prev_node}" Name="out" /><NameCon UId="{edge_part}" Name="in" /></Wire>')

    w_edge_bit = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_edge_bit}"><IdentCon UId="{edge_acc}" /><NameCon UId="{edge_part}" Name="bit" /></Wire>')

    w_edge_out = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_edge_out}"><NameCon UId="{edge_part}" Name="out" /><NameCon UId="{coil_part}" Name="in" /></Wire>')

    w_coil_op = builder.alloc_uid()
    builder.wires.append(f'    <Wire UId="{w_coil_op}"><IdentCon UId="{coil_acc}" /><NameCon UId="{coil_part}" Name="operand" /></Wire>')

    return render_network_xml(builder)


def build_analog_scale_network(nw_index: int, title: str, comment: str,
                               value_var: str, norm_temp_var: str, out_var: str,
                               norm_min: Any = 0, norm_max: Any = 27648,
                               scale_min: Any = 0.0, scale_max: Any = 50.0,
                               norm_src_type: str = "Int", scale_dest_type: str = "Real") -> str:
    """
    构建模拟量标定链程序段 (NORM_X + SCALE_X)，严格对齐 V16 画廊样板：
    - 模拟量输入默认: Int(0..27648) -> Real(0.0..50.0)
    - 模拟量输出可传: norm_src_type="Real", norm_min=0.0, norm_max=50.0, scale_dest_type="Int", scale_min=0, scale_max=27648
    """
    builder = LadNetworkBuilder(nw_index, title, comment)

    n_min_acc = builder.add_literal_constant(norm_src_type, norm_min)
    val_acc = builder.add_access(value_var)
    n_max_acc = builder.add_literal_constant(norm_src_type, norm_max)
    norm_out_acc = builder.add_access(norm_temp_var)

    s_min_acc = builder.add_literal_constant(scale_dest_type, scale_min)
    s_in_acc = builder.add_access(norm_temp_var)
    s_max_acc = builder.add_literal_constant(scale_dest_type, scale_max)
    out_acc = builder.add_access(out_var)

    norm_part = builder.alloc_uid()
    scale_part = builder.alloc_uid()
    builder.parts.append(f'''    <Part Name="Normalize" UId="{norm_part}" DisabledENO="true">
      <TemplateValue Name="SrcType" Type="Type">{_escape_xml(norm_src_type)}</TemplateValue>
      <TemplateValue Name="DestType" Type="Type">Real</TemplateValue>
    </Part>''')
    builder.parts.append(f'''    <Part Name="Scale_X" UId="{scale_part}" DisabledENO="true">
      <TemplateValue Name="SrcType" Type="Type">Real</TemplateValue>
      <TemplateValue Name="DestType" Type="Type">{_escape_xml(scale_dest_type)}</TemplateValue>
    </Part>''')

    builder.wires.append(f'    <Wire UId="{builder.alloc_uid()}"><Powerrail /><NameCon UId="{norm_part}" Name="en" /></Wire>')
    builder.wires.append(f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{n_min_acc}" /><NameCon UId="{norm_part}" Name="min" /></Wire>')
    builder.wires.append(f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{val_acc}" /><NameCon UId="{norm_part}" Name="value" /></Wire>')
    builder.wires.append(f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{n_max_acc}" /><NameCon UId="{norm_part}" Name="max" /></Wire>')
    builder.wires.append(f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{norm_part}" Name="eno" /><NameCon UId="{scale_part}" Name="en" /></Wire>')
    builder.wires.append(f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{norm_part}" Name="out" /><IdentCon UId="{norm_out_acc}" /></Wire>')
    builder.wires.append(f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{s_min_acc}" /><NameCon UId="{scale_part}" Name="min" /></Wire>')
    builder.wires.append(f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{s_in_acc}" /><NameCon UId="{scale_part}" Name="value" /></Wire>')
    builder.wires.append(f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{s_max_acc}" /><NameCon UId="{scale_part}" Name="max" /></Wire>')
    builder.wires.append(f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{scale_part}" Name="out" /><IdentCon UId="{out_acc}" /></Wire>')

    return render_network_xml(builder)


def _append_contact_chain(builder: LadNetworkBuilder, raw_contacts: List[Any]) -> tuple:
    """在 builder 中依次创建串联触点并完成内部连线，返回 (首触点UId, 末触点UId)"""
    first_uid = None
    prev_uid = None
    for raw_c in raw_contacts:
        c = _normalize_contact(raw_c)
        acc_uid = builder.add_access(c["name"], c.get("scope", "GlobalVariable"))
        part_uid = builder.alloc_uid()
        if c.get("negated", False):
            builder.parts.append(f'    <Part Name="Contact" UId="{part_uid}"><Negated Name="operand" /></Part>')
        else:
            builder.parts.append(f'    <Part Name="Contact" UId="{part_uid}" />')
        builder.wires.append(
            f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{acc_uid}" /><NameCon UId="{part_uid}" Name="operand" /></Wire>'
        )
        if first_uid is None:
            first_uid = part_uid
        if prev_uid is not None:
            builder.wires.append(
                f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{prev_uid}" Name="out" /><NameCon UId="{part_uid}" Name="in" /></Wire>'
            )
        prev_uid = part_uid
    return first_uid, prev_uid


def build_or_branches_series_network(
    nw_index: int,
    title: str,
    comment: str,
    branches: List[List[Any]],
    series_after: Optional[List[Any]],
    coil_var: str,
    coil_type: str = "Coil",
    coil_scope: str = "GlobalVariable",
) -> str:
    """
    构建通用“多支路并联 (O) → 公共安全/联锁串联主干路 → 输出线圈”程序段：
      (branch_1 || branch_2 || ...) -> series_after -> coil_var
    当仅传入 1 条支路时，自动退化为标准串联回路（不插入多余 O 节点）。
    """
    valid_branches = [b for b in branches if b]
    if not valid_branches:
        raise ValueError("build_or_branches_series_network 至少需要 1 条非空输入支路")

    common_series = list(series_after or [])
    if len(valid_branches) == 1:
        return build_simple_series_network(
            nw_index=nw_index,
            title=title,
            comment=comment,
            contacts=valid_branches[0] + common_series,
            coil_var=coil_var,
            coil_type=coil_type,
            coil_scope=coil_scope,
        )

    builder = LadNetworkBuilder(nw_index, title, comment)

    branch_firsts: List[str] = []
    branch_lasts: List[str] = []
    for br in valid_branches:
        first_uid, last_uid = _append_contact_chain(builder, br)
        branch_firsts.append(first_uid)
        branch_lasts.append(last_uid)

    or_uid = builder.alloc_uid()
    card_n = len(valid_branches)
    builder.parts.append(
        f'    <Part Name="O" UId="{or_uid}"><TemplateValue Name="Card" Type="Cardinality">{card_n}</TemplateValue></Part>'
    )

    # 左母线并行连入所有支路的首个触点
    rail_targets = "".join(f'<NameCon UId="{u}" Name="in" />' for u in branch_firsts)
    builder.wires.append(f'    <Wire UId="{builder.alloc_uid()}"><Powerrail />{rail_targets}</Wire>')

    # 各支路末触点汇流至 O 节点 in1..inN
    for idx, last_uid in enumerate(branch_lasts, start=1):
        builder.wires.append(
            f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{last_uid}" Name="out" /><NameCon UId="{or_uid}" Name="in{idx}" /></Wire>'
        )

    prev_uid = or_uid
    prev_pin = "out"
    if common_series:
        s_first, s_last = _append_contact_chain(builder, common_series)
        builder.wires.append(
            f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{prev_uid}" Name="{prev_pin}" /><NameCon UId="{s_first}" Name="in" /></Wire>'
        )
        prev_uid = s_last
        prev_pin = "out"

    coil_acc = builder.add_access(coil_var, coil_scope)
    coil_part = builder.alloc_uid()
    builder.parts.append(f'    <Part Name="{_escape_xml(coil_type)}" UId="{coil_part}" />')
    builder.wires.append(
        f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{prev_uid}" Name="{prev_pin}" /><NameCon UId="{coil_part}" Name="in" /></Wire>'
    )
    builder.wires.append(
        f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{coil_acc}" /><NameCon UId="{coil_part}" Name="operand" /></Wire>'
    )

    return render_network_xml(builder)


def build_manual_auto_interlocked_actuator_network(
    nw_index: int,
    title: str,
    comment: str,
    coil_var: str,
    manual_trigger_var: Optional[str] = None,
    auto_request_var: Optional[str] = None,
    manual_mode_var: str = "M_手动模式",
    auto_mode_var: str = "M_自动模式",
    safety_var: str = "M_安全条件满足",
    interlock_contacts: Optional[List[Any]] = None,
    extra_branches: Optional[List[List[Any]]] = None,
    manual_contacts: Optional[List[Any]] = None,
    auto_contacts: Optional[List[Any]] = None,
    coil_type: str = "Coil",
    coil_scope: str = "GlobalVariable",
) -> str:
    """
    构建产线级“手自动分流并联 + 公共安全与机械防撞串联主干路”执行器控制程序段：
      ([手动点动 -> 手动模式] || [自动请求 -> 自动模式] || [可选回原位等分支])
      -> M_安全条件满足 -> 公共机械联锁/互斥常闭/热继 -> 执行器 %Q 线圈
    所有联锁条件均位于 O 节点汇流之后，确保手动与自动模式下均 100% 受安全条件与机械防撞保护。
    """
    branches: List[List[Any]] = []
    if manual_contacts:
        branches.append(list(manual_contacts))
    elif manual_trigger_var:
        branches.append([
            {"name": manual_trigger_var, "negated": False},
            {"name": manual_mode_var, "negated": False},
        ])

    if auto_contacts:
        branches.append(list(auto_contacts))
    elif auto_request_var:
        branches.append([
            {"name": auto_request_var, "negated": False},
            {"name": auto_mode_var, "negated": False},
        ])

    for eb in (extra_branches or []):
        if eb:
            branches.append(list(eb))

    norm_interlocks = [_normalize_contact(c) for c in (interlock_contacts or [])]
    if safety_var and not any(c.get("name") == safety_var and not c.get("negated", False) for c in norm_interlocks):
        norm_interlocks.insert(0, {"name": safety_var, "negated": False})

    return build_or_branches_series_network(
        nw_index=nw_index,
        title=title,
        comment=comment,
        branches=branches,
        series_after=norm_interlocks,
        coil_var=coil_var,
        coil_type=coil_type,
        coil_scope=coil_scope,
    )


def build_step_latch_network(
    nw_index: int,
    title: str,
    comment: str,
    step_var: str,
    trigger_contacts: List[Any],
    next_step_vars: Optional[List[str]] = None,
    mode_var: str = "M_自动模式",
    safety_var: str = "M_安全条件满足",
    alarm_summary_var: Optional[str] = "M_报警汇总",
    extra_hold_contacts: Optional[List[Any]] = None,
    step_scope: str = "GlobalVariable",
) -> str:
    """
    构建产线自动步序“触发进入 || 本步自锁 -> 安全/模式/报警/下步互锁 -> 本步线圈”程序段：
      (trigger_contacts || step_var)
      -> mode_var -> safety_var -> -|/|- alarm_summary_var -> -|/|- next_step_vars -> step_var
    保障每个步序 M 位仅有唯一 Coil 写入（零双线圈），并在急停/切手动/故障时安全清零。
    """
    series_after: List[Dict[str, Any]] = []
    if mode_var:
        series_after.append({"name": mode_var, "negated": False})
    if safety_var:
        series_after.append({"name": safety_var, "negated": False})
    if alarm_summary_var:
        series_after.append({"name": alarm_summary_var, "negated": True})
    for ns in (next_step_vars or []):
        if ns:
            series_after.append({"name": ns, "negated": True})
    for ec in (extra_hold_contacts or []):
        series_after.append(_normalize_contact(ec))

    return build_or_branches_series_network(
        nw_index=nw_index,
        title=title,
        comment=comment,
        branches=[list(trigger_contacts), [{"name": step_var, "negated": False, "scope": step_scope}]],
        series_after=series_after,
        coil_var=step_var,
        coil_type="Coil",
        coil_scope=step_scope,
    )


def _append_branch_or_series(
    builder: LadNetworkBuilder,
    branches: Optional[List[List[Any]]],
    pre_contacts: Optional[List[Any]],
    series_after: Optional[List[Any]],
    rail_targets: List[Tuple[str, str]],
) -> Tuple[Optional[str], Optional[str]]:
    """
    在已有 LadNetworkBuilder 中构建可选前置触点、多支路并联(O)与后置串联触点，
    返回末端输出节点 (tail_uid, tail_pin)；若全为空则返回 (None, None)。
    """
    tail_uid: Optional[str] = None
    tail_pin: Optional[str] = None

    pre_list = list(pre_contacts or [])
    if pre_list:
        p_first, p_last = _append_contact_chain(builder, pre_list)
        rail_targets.append((p_first, "in"))
        tail_uid, tail_pin = p_last, "out"

    valid_branches = [list(b) for b in (branches or []) if b]
    if len(valid_branches) == 1:
        b_first, b_last = _append_contact_chain(builder, valid_branches[0])
        if tail_uid is not None and tail_pin is not None:
            builder.wires.append(
                f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{tail_uid}" Name="{tail_pin}" /><NameCon UId="{b_first}" Name="in" /></Wire>'
            )
        else:
            rail_targets.append((b_first, "in"))
        tail_uid, tail_pin = b_last, "out"
    elif len(valid_branches) > 1:
        branch_firsts: List[str] = []
        branch_lasts: List[str] = []
        for br in valid_branches:
            bf, bl = _append_contact_chain(builder, br)
            branch_firsts.append(bf)
            branch_lasts.append(bl)
        if tail_uid is not None and tail_pin is not None:
            targets_xml = "".join(f'<NameCon UId="{u}" Name="in" />' for u in branch_firsts)
            builder.wires.append(
                f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{tail_uid}" Name="{tail_pin}" />{targets_xml}</Wire>'
            )
        else:
            for bf in branch_firsts:
                rail_targets.append((bf, "in"))

        or_uid = builder.alloc_uid()
        builder.parts.append(
            f'    <Part Name="O" UId="{or_uid}"><TemplateValue Name="Card" Type="Cardinality">{len(valid_branches)}</TemplateValue></Part>'
        )
        for idx, bl in enumerate(branch_lasts, start=1):
            builder.wires.append(
                f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{bl}" Name="out" /><NameCon UId="{or_uid}" Name="in{idx}" /></Wire>'
            )
        tail_uid, tail_pin = or_uid, "out"

    after_list = list(series_after or [])
    if after_list:
        s_first, s_last = _append_contact_chain(builder, after_list)
        if tail_uid is not None and tail_pin is not None:
            builder.wires.append(
                f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{tail_uid}" Name="{tail_pin}" /><NameCon UId="{s_first}" Name="in" /></Wire>'
            )
        else:
            rail_targets.append((s_first, "in"))
        tail_uid, tail_pin = s_last, "out"

    return tail_uid, tail_pin


def build_composite_network(
    nw_index: int,
    title: str,
    comment: str,
    rungs: List[Dict[str, Any]],
) -> str:
    """
    通用单程序段多梯级/多分支构建底座：
    在一个 CompileUnit (<FlgNet>) 内按 rungs 顺序构建多条关联梯级（Multi-Rung），
    统一汇总至唯一一根 <Powerrail /> Wire，并支持内联 TON/TOF/TP、PBox/NBox 与 T 型分叉多线圈输出。

    每条 rung 字典支持字段:
      - pre_contacts: 可选前置串联触点列表（位于 O 分叉之前）
      - branches: 可选多支路并联列表 List[List[contact]]
      - contacts / series_after: 可选串联触点列表（若未传 branches 则直接串联）
      - edge: 可选 {'bit_var': str, 'type': 'PBox'|'NBox', 'scope': 'GlobalVariable'}
      - ton: 可选 {'member': str, 'pt': str, 'db_name': '全局定时器', 'type': 'TON'|'TOF'|'TP',
                   'scope': 'GlobalVariable', 'pt_scope': 'TypedConstant', 'et_var': None,
                   'et_scope': 'GlobalVariable', 'post_contacts': []}
      - outputs: 可选输出列表 [{'var': str, 'type': 'Coil'|'SCoil'|'RCoil'|'NCoil'|'PCoil',
                                'scope': 'GlobalVariable', 'negated': False, 'bit_var': None,
                                'branch_contacts': [...]}]
      - coil_var / coil_type / coil_scope / coil_negated: 单输出简写形式
    """
    if not rungs:
        raise ValueError("build_composite_network 至少需要 1 条 rung 梯级")

    builder = LadNetworkBuilder(nw_index, title, comment)
    rail_targets: List[Tuple[str, str]] = []

    for rung in rungs:
        pre_contacts = rung.get("pre_contacts")
        branches = rung.get("branches")
        if "contacts" in rung and "series_after" in rung:
            series_after = list(rung.get("contacts") or []) + list(rung.get("series_after") or [])
        else:
            series_after = rung.get("series_after") if rung.get("series_after") is not None else rung.get("contacts")

        tail_uid, tail_pin = _append_branch_or_series(
            builder,
            branches=branches,
            pre_contacts=pre_contacts,
            series_after=series_after,
            rail_targets=rail_targets,
        )

        # 可选边沿检测指令 PBox / NBox
        edge_cfg = rung.get("edge")
        if edge_cfg:
            edge_type = str(edge_cfg.get("type", "PBox")).strip() or "PBox"
            edge_bit = str(edge_cfg["bit_var"])
            edge_scope = str(edge_cfg.get("scope", "GlobalVariable")).strip() or "GlobalVariable"
            edge_acc = builder.add_access(edge_bit, edge_scope)
            edge_uid = builder.alloc_uid()
            builder.parts.append(f'    <Part Name="{_escape_xml(edge_type)}" UId="{edge_uid}" />')
            if tail_uid is not None and tail_pin is not None:
                builder.wires.append(
                    f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{tail_uid}" Name="{tail_pin}" /><NameCon UId="{edge_uid}" Name="in" /></Wire>'
                )
            else:
                rail_targets.append((edge_uid, "in"))
            builder.wires.append(
                f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{edge_acc}" /><NameCon UId="{edge_uid}" Name="bit" /></Wire>'
            )
            tail_uid, tail_pin = edge_uid, "out"

        # 可选内联定时器指令 TON / TOF / TP
        ton_cfg = rung.get("ton")
        if ton_cfg:
            t_type = str(ton_cfg.get("type", "TON")).strip() or "TON"
            t_member = str(ton_cfg["member"]).strip()
            t_db = str(ton_cfg.get("db_name", "全局定时器")).strip()
            t_scope = str(ton_cfg.get("scope", "GlobalVariable")).strip() or "GlobalVariable"
            t_pt = str(ton_cfg.get("pt", "T#5S")).strip()
            t_pt_scope = str(ton_cfg.get("pt_scope", "TypedConstant")).strip() or "TypedConstant"
            et_var = ton_cfg.get("et_var")
            et_scope = str(ton_cfg.get("et_scope", "GlobalVariable")).strip() or "GlobalVariable"

            if t_pt_scope == "TypedConstant":
                pt_acc = builder.add_typed_time_constant(t_pt)
            else:
                pt_acc = builder.add_access(t_pt, t_pt_scope)

            ton_uid = builder.alloc_uid()
            inst_uid = builder.alloc_uid()
            member_segs = [seg.strip() for seg in t_member.split(".") if seg.strip()]
            inst_comps: List[str] = []
            if t_scope == "GlobalVariable" and t_db and (not member_segs or member_segs[0] != t_db):
                inst_comps.append(f'<Component Name="{_escape_xml(t_db)}" />')
            for seg in member_segs:
                inst_comps.append(f'<Component Name="{_escape_xml(seg)}" />')
            ton_xml = (
                f'    <Part Name="{_escape_xml(t_type)}" Version="1.0" UId="{ton_uid}">\n'
                f'      <Instance Scope="{_escape_xml(t_scope)}" UId="{inst_uid}">\n'
                f'        {"".join(inst_comps)}\n'
                f'      </Instance>\n'
                f'      <TemplateValue Name="time_type" Type="Type">Time</TemplateValue>\n'
                f'    </Part>'
            )
            builder.parts.append(ton_xml)

            if tail_uid is not None and tail_pin is not None:
                builder.wires.append(
                    f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{tail_uid}" Name="{tail_pin}" /><NameCon UId="{ton_uid}" Name="IN" /></Wire>'
                )
            else:
                rail_targets.append((ton_uid, "IN"))

            builder.wires.append(
                f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{pt_acc}" /><NameCon UId="{ton_uid}" Name="PT" /></Wire>'
            )
            if et_var:
                et_acc = builder.add_access(str(et_var), et_scope)
                builder.wires.append(
                    f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{ton_uid}" Name="ET" /><IdentCon UId="{et_acc}" /></Wire>'
                )
            else:
                open_uid = builder.alloc_uid()
                builder.wires.append(
                    f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{ton_uid}" Name="ET" /><OpenCon UId="{open_uid}" /></Wire>'
                )
            tail_uid, tail_pin = ton_uid, "Q"

            post_contacts = list(ton_cfg.get("post_contacts") or [])
            if post_contacts:
                p_first, p_last = _append_contact_chain(builder, post_contacts)
                builder.wires.append(
                    f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{tail_uid}" Name="{tail_pin}" /><NameCon UId="{p_first}" Name="in" /></Wire>'
                )
                tail_uid, tail_pin = p_last, "out"

        # 收集本梯级输出线圈（支持 T 型分叉多线圈）
        outputs = list(rung.get("outputs") or [])
        if not outputs and rung.get("coil_var"):
            outputs = [{
                "var": rung["coil_var"],
                "type": rung.get("coil_type", "Coil"),
                "scope": rung.get("coil_scope", "GlobalVariable"),
                "negated": bool(rung.get("coil_negated", False)),
            }]

        if outputs:
            entry_pins: List[Tuple[str, str]] = []
            for out_cfg in outputs:
                out_var = str(out_cfg["var"])
                out_type = str(out_cfg.get("type", "Coil")).strip() or "Coil"
                out_scope = str(out_cfg.get("scope", "GlobalVariable")).strip() or "GlobalVariable"
                out_neg = bool(out_cfg.get("negated", False))
                sub_contacts = list(out_cfg.get("branch_contacts") or [])

                coil_acc = builder.add_access(out_var, out_scope)
                coil_uid = builder.alloc_uid()
                if out_neg:
                    builder.parts.append(
                        f'    <Part Name="{_escape_xml(out_type)}" UId="{coil_uid}"><Negated Name="operand" /></Part>'
                    )
                else:
                    builder.parts.append(f'    <Part Name="{_escape_xml(out_type)}" UId="{coil_uid}" />')
                builder.wires.append(
                    f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{coil_acc}" /><NameCon UId="{coil_uid}" Name="operand" /></Wire>'
                )
                if out_type in ("NCoil", "PCoil") and out_cfg.get("bit_var"):
                    bit_acc = builder.add_access(str(out_cfg["bit_var"]), out_cfg.get("bit_scope", "GlobalVariable"))
                    builder.wires.append(
                        f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{bit_acc}" /><NameCon UId="{coil_uid}" Name="bit" /></Wire>'
                    )

                if sub_contacts:
                    sf, sl = _append_contact_chain(builder, sub_contacts)
                    builder.wires.append(
                        f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{sl}" Name="out" /><NameCon UId="{coil_uid}" Name="in" /></Wire>'
                    )
                    entry_pins.append((sf, "in"))
                else:
                    entry_pins.append((coil_uid, "in"))

            if tail_uid is not None and tail_pin is not None:
                dst_xml = "".join(f'<NameCon UId="{u}" Name="{p}" />' for u, p in entry_pins)
                builder.wires.append(
                    f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{tail_uid}" Name="{tail_pin}" />{dst_xml}</Wire>'
                )
            else:
                rail_targets.extend(entry_pins)

    if not rail_targets:
        raise ValueError("build_composite_network 生成的梯级未产生任何左母线连接目标")

    rail_targets_xml = "".join(f'<NameCon UId="{u}" Name="{p}" />' for u, p in rail_targets)
    builder.wires.insert(0, f'    <Wire UId="{builder.alloc_uid()}"><Powerrail />{rail_targets_xml}</Wire>')

    return render_network_xml(builder)


def build_sr_latch_network(
    nw_index: int,
    title: str,
    comment: str,
    target_var: str,
    set_branches: List[List[Any]],
    reset_branches: List[List[Any]],
    set_series_after: Optional[List[Any]] = None,
    reset_series_after: Optional[List[Any]] = None,
    target_scope: str = "GlobalVariable",
) -> str:
    """将同一变量的成对 SCoil (置位) 与 RCoil (复位) 合并在同一个复合程序段中"""
    return build_composite_network(
        nw_index=nw_index,
        title=title,
        comment=comment,
        rungs=[
            {
                "branches": set_branches,
                "series_after": list(set_series_after or []),
                "coil_var": target_var,
                "coil_type": "SCoil",
                "coil_scope": target_scope,
            },
            {
                "branches": reset_branches,
                "series_after": list(reset_series_after or []),
                "coil_var": target_var,
                "coil_type": "RCoil",
                "coil_scope": target_scope,
            },
        ],
    )


def build_timeout_alarm_network(
    nw_index: int,
    title: str,
    comment: str,
    enable_contacts: List[Any],
    timer_db_member: str,
    time_pt: str,
    alarm_var: str,
    reset_contacts: List[Any],
    timer_db_name: str = "全局定时器",
    summary_alarms: Optional[List[Any]] = None,
    summary_var: Optional[str] = None,
) -> str:
    """
    构建“TON 计时直连 SCoil(报警) + 复位 RCoil(报警) [+ 可选报警汇总]”完整微功能闭环段，
    彻底消灭如 M_超时脉冲到达 等一次性过渡 %M 桥接变量。
    """
    rungs: List[Dict[str, Any]] = [
        {
            "contacts": list(enable_contacts),
            "ton": {
                "member": timer_db_member,
                "pt": time_pt,
                "db_name": timer_db_name,
            },
            "coil_var": alarm_var,
            "coil_type": "SCoil",
        },
        {
            "contacts": list(reset_contacts),
            "coil_var": alarm_var,
            "coil_type": "RCoil",
        },
    ]
    if summary_var and summary_alarms:
        branches = [
            b if isinstance(b, list) else [b]
            for b in summary_alarms
            if b
        ]
        if branches:
            rungs.append({
                "branches": branches,
                "coil_var": summary_var,
                "coil_type": "Coil",
            })
    return build_composite_network(nw_index=nw_index, title=title, comment=comment, rungs=rungs)


def build_mode_select_network(
    nw_index: int,
    title: str,
    comment: str,
    selector_var: str,
    auto_mode_var: str = "M_自动模式",
    manual_mode_var: str = "M_手动模式",
    safety_var: Optional[str] = "M_安全条件满足",
    single_di_selector: bool = True,
    manual_selector_var: Optional[str] = None,
    extra_rungs: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """将手自动模式切换双梯级（及可选系统运行/点动梯级）合并为同一个模式管理程序段"""
    auto_contacts: List[Any] = [{"name": selector_var, "negated": False}]
    if safety_var:
        auto_contacts.append({"name": safety_var, "negated": False})

    if single_di_selector:
        manual_contacts: List[Any] = [{"name": selector_var, "negated": True}]
    else:
        man_sel = manual_selector_var or selector_var
        manual_contacts = [
            {"name": man_sel, "negated": False},
            {"name": auto_mode_var, "negated": True},
        ]
    if safety_var:
        manual_contacts.append({"name": safety_var, "negated": False})

    rungs: List[Dict[str, Any]] = [
        {"contacts": auto_contacts, "coil_var": auto_mode_var, "coil_type": "Coil"},
        {"contacts": manual_contacts, "coil_var": manual_mode_var, "coil_type": "Coil"},
    ]
    for er in (extra_rungs or []):
        if er:
            rungs.append(er)
    return build_composite_network(nw_index=nw_index, title=title, comment=comment, rungs=rungs)


def build_step_with_timer_network(
    nw_index: int,
    title: str,
    comment: str,
    step_var: str,
    trigger_contacts: List[Any],
    timer_db_member: str,
    time_pt: str,
    next_step_vars: Optional[List[str]] = None,
    mode_var: str = "M_自动模式",
    safety_var: str = "M_安全条件满足",
    alarm_summary_var: Optional[str] = "M_报警汇总",
    extra_hold_contacts: Optional[List[Any]] = None,
    timer_db_name: str = "全局定时器",
    auto_exit_on_timer_done: bool = True,
    timer_done_coil_var: Optional[str] = None,
) -> str:
    """
    将“单步步序自锁 + 本步延时 TON 定时器”合并在同一个程序段中：
      Rung 1: (trigger || step_var) -> mode -> safety -> -|/|- alarm -> -|/|- next_steps -> [-|/|- T.Q] -> step_var
      Rung 2: step_var -> TON(timer_db_member, PT=time_pt) [-> timer_done_coil_var (可选)]
    默认直接通过 全局定时器.<member>.Q 自复位步序，无需任何过渡 %M 标志位。
    """
    series_after: List[Dict[str, Any]] = []
    if mode_var:
        series_after.append({"name": mode_var, "negated": False})
    if safety_var:
        series_after.append({"name": safety_var, "negated": False})
    if alarm_summary_var:
        series_after.append({"name": alarm_summary_var, "negated": True})
    for ns in (next_step_vars or []):
        if ns:
            series_after.append({"name": ns, "negated": True})
    if auto_exit_on_timer_done:
        series_after.append({"name": f"{timer_db_name}.{timer_db_member}.Q", "negated": True})
    for ec in (extra_hold_contacts or []):
        series_after.append(_normalize_contact(ec))

    rung_step: Dict[str, Any] = {
        "branches": [list(trigger_contacts), [{"name": step_var, "negated": False}]],
        "series_after": series_after,
        "coil_var": step_var,
        "coil_type": "Coil",
    }
    rung_ton: Dict[str, Any] = {
        "contacts": [{"name": step_var, "negated": False}],
        "ton": {
            "member": timer_db_member,
            "pt": time_pt,
            "db_name": timer_db_name,
        },
    }
    if timer_done_coil_var:
        rung_ton["coil_var"] = timer_done_coil_var
        rung_ton["coil_type"] = "Coil"

    return build_composite_network(
        nw_index=nw_index,
        title=title,
        comment=comment,
        rungs=[rung_step, rung_ton],
    )


def build_dual_interlocked_actuators_network(
    nw_index: int,
    title: str,
    comment: str,
    actuator_a: Dict[str, Any],
    actuator_b: Dict[str, Any],
    safety_var: str = "M_安全条件满足",
    manual_mode_var: str = "M_手动模式",
    auto_mode_var: str = "M_自动模式",
) -> str:
    """
    将同一台物理设备的成对互斥执行器（如电机正转+反转、双电控气缸伸出+缩回）合并为 1 个复合程序段，
    自动在各自 O 汇流节点后的公共串联主干路注入安全许可、外部防撞条件与对方常闭软件互锁（满足 S3/S4/S5）。

    actuator_a / actuator_b 字典格式:
      - coil_var: str (必填)
      - manual_trigger_var / auto_request_var / manual_contacts / auto_contacts / extra_branches
      - interlock_contacts: 可选列表
      - coil_type / coil_scope
    """
    coil_a = str(actuator_a["coil_var"])
    coil_b = str(actuator_b["coil_var"])

    def _to_rung(cfg: Dict[str, Any], other_coil: str) -> Dict[str, Any]:
        branches: List[List[Any]] = []
        if cfg.get("manual_contacts"):
            branches.append(list(cfg["manual_contacts"]))
        elif cfg.get("manual_trigger_var"):
            branches.append([
                {"name": cfg["manual_trigger_var"], "negated": False},
                {"name": manual_mode_var, "negated": False},
            ])
        if cfg.get("auto_contacts"):
            branches.append(list(cfg["auto_contacts"]))
        elif cfg.get("auto_request_var"):
            branches.append([
                {"name": cfg["auto_request_var"], "negated": False},
                {"name": auto_mode_var, "negated": False},
            ])
        for eb in (cfg.get("extra_branches") or []):
            if eb:
                branches.append(list(eb))

        interlocks = [_normalize_contact(c) for c in (cfg.get("interlock_contacts") or [])]
        if safety_var and not any(c.get("name") == safety_var and not c.get("negated", False) for c in interlocks):
            interlocks.insert(0, {"name": safety_var, "negated": False})
        if not any(c.get("name") == other_coil and c.get("negated", False) for c in interlocks):
            interlocks.append({"name": other_coil, "negated": True})

        return {
            "branches": branches,
            "series_after": interlocks,
            "coil_var": cfg["coil_var"],
            "coil_type": cfg.get("coil_type", "Coil"),
            "coil_scope": cfg.get("coil_scope", "GlobalVariable"),
        }

    return build_composite_network(
        nw_index=nw_index,
        title=title,
        comment=comment,
        rungs=[_to_rung(actuator_a, coil_b), _to_rung(actuator_b, coil_a)],
    )


def build_star_delta_networks(
    start_nw_index: int,
    device_label: str,
    start_contacts: List[Any],
    stop_contacts: List[Any],
    km_main: str,
    km_star: str,
    km_delta: str,
    timer_switch_member: str,
    timer_delay_member: str,
    switch_pt: str = "T#6S",
    delay_pt: str = "T#300MS",
    safety_var: str = "M_安全条件满足",
    timer_db_name: str = "全局定时器",
) -> List[str]:
    """
    2 段式紧凑星三角降压启动模板（由传统 5 段压至 2 段，0 过渡 %M 变量）：
      NW1: 主接触器启保停自锁 + 星形切换定时器 TON(T1) + 换接死区延时定时器 TON(T2)
      NW2: 星形接触器(KM_Star)与角形接触器(KM_Delta)互锁输出双梯级（直接引用 T1.Q / T2.Q）
    """
    stop_chain = [_normalize_contact(c) for c in stop_contacts]
    if safety_var and not any(c.get("name") == safety_var and not c.get("negated", False) for c in stop_chain):
        stop_chain.append({"name": safety_var, "negated": False})

    t_switch_q = f"{timer_db_name}.{timer_switch_member}.Q"
    t_delay_q = f"{timer_db_name}.{timer_delay_member}.Q"

    nw1 = build_composite_network(
        nw_index=start_nw_index,
        title=f"NW{start_nw_index} {device_label} —— 主接触器启保停与星三角切换计时",
        comment=(
            f"【功能】控制主接触器 {km_main} 启保停自锁，并在吸合后驱动星形计时({switch_pt})与换接死区延时({delay_pt})。\n"
            f"【动作说明】主接触器 {km_main} 吸合且 {km_delta} 未吸合时计时 {timer_switch_member}；"
            f"{switch_pt} 到期且 {km_star} 断开后触发 {timer_delay_member} 防电弧死区延时。\n"
            "【调试提示】星形运行时间可在 4~10S 间按电机功率与惯量微调。"
        ),
        rungs=[
            {
                "branches": [list(start_contacts), [{"name": km_main, "negated": False}]],
                "series_after": stop_chain,
                "coil_var": km_main,
            },
            {
                "contacts": [{"name": km_main, "negated": False}, {"name": km_delta, "negated": True}],
                "ton": {"member": timer_switch_member, "pt": switch_pt, "db_name": timer_db_name},
            },
            {
                "contacts": [
                    {"name": km_main, "negated": False},
                    {"name": t_switch_q, "negated": False},
                    {"name": km_star, "negated": True},
                ],
                "ton": {"member": timer_delay_member, "pt": delay_pt, "db_name": timer_db_name},
            },
        ],
    )

    nw2 = build_composite_network(
        nw_index=start_nw_index + 1,
        title=f"NW{start_nw_index + 1} {device_label} —— 星形/角形接触器互锁切换输出",
        comment=(
            f"【功能】在 {km_main} 吸合期间先接通星形 {km_star}，经 {switch_pt}+{delay_pt} 死区切换后自锁接通角形 {km_delta}。\n"
            f"【互锁说明】{km_star} 与 {km_delta} 严格软件常闭互锁（须配合硬件常闭互锁），角形自锁支路位于 {km_main} 之后，停机或安全断开瞬间同步失电。"
        ),
        rungs=[
            {
                "contacts": [
                    {"name": km_main, "negated": False},
                    {"name": t_switch_q, "negated": True},
                    {"name": km_delta, "negated": True},
                ],
                "coil_var": km_star,
            },
            {
                "pre_contacts": [{"name": km_main, "negated": False}],
                "branches": [
                    [{"name": t_delay_q, "negated": False}],
                    [{"name": km_delta, "negated": False}],
                ],
                "series_after": [{"name": km_star, "negated": True}],
                "coil_var": km_delta,
            },
        ],
    )
    return [nw1, nw2]


def build_indicator_lamps_network(
    nw_index: int,
    title: str,
    comment: str,
    lamp_rungs: List[Dict[str, Any]],
) -> str:
    """将同工位或全线状态指示灯组（运行灯、故障灯、急停灯等）合并为 1 个复合程序段"""
    rungs: List[Dict[str, Any]] = []
    for lr in lamp_rungs:
        rungs.append({
            "branches": lr.get("branches"),
            "contacts": lr.get("contacts"),
            "series_after": lr.get("series_after"),
            "coil_var": lr["coil_var"],
            "coil_type": lr.get("coil_type", "Coil"),
            "coil_scope": lr.get("coil_scope", "GlobalVariable"),
        })
    return build_composite_network(nw_index=nw_index, title=title, comment=comment, rungs=rungs)


def render_network_xml(builder: LadNetworkBuilder) -> str:
    """将程序段组装为标准 SimaticML CompileUnit XML"""
    hex_id = hex(builder.nw_index * 5 + 3)[2:]
    safe_title = builder.title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    safe_comment = builder.comment.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    lines = [
        f'      <SW.Blocks.CompileUnit ID="{hex_id}" CompositionName="CompileUnits">',
        '        <AttributeList>',
        '          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">',
        '  <Parts>',
        '\n'.join(builder.parts),
        '  </Parts>',
        '  <Wires>',
        '\n'.join(builder.wires),
        '  </Wires>',
        '</FlgNet></NetworkSource>',
        '          <ProgrammingLanguage>LAD</ProgrammingLanguage>',
        '        </AttributeList>',
        '        <ObjectList>',
        f'          <MultilingualText ID="{hex_id}1" CompositionName="Comment">',
        '            <ObjectList>',
        f'              <MultilingualTextItem ID="{hex_id}2" CompositionName="Items">',
        '                <AttributeList>',
        '                  <Culture>zh-CN</Culture>',
        f'                  <Text>{safe_comment}</Text>',
        '                </AttributeList>',
        '              </MultilingualTextItem>',
        '            </ObjectList>',
        '          </MultilingualText>',
        f'          <MultilingualText ID="{hex_id}3" CompositionName="Title">',
        '            <ObjectList>',
        f'              <MultilingualTextItem ID="{hex_id}4" CompositionName="Items">',
        '                <AttributeList>',
        '                  <Culture>zh-CN</Culture>',
        f'                  <Text>{safe_title}</Text>',
        '                </AttributeList>',
        '              </MultilingualTextItem>',
        '            </ObjectList>',
        '          </MultilingualText>',
        '        </ObjectList>',
        '      </SW.Blocks.CompileUnit>'
    ]
    return '\n'.join(lines)


def build_fc_xml(fc_name: str, fc_number: int, networks_xml: Union[List[str], str], comment: str = "") -> str:
    """组装完整的 FC 功能块 XML"""
    if isinstance(networks_xml, str):
        networks_xml = [networks_xml]
    safe_name = fc_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    safe_comment = comment.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    xml_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Document>',
        '  <Engineering version="V16" />',
        '  <SW.Blocks.FC ID="0">',
        '    <AttributeList>',
        '      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v4">',
        '        <Section Name="Input" />',
        '        <Section Name="Output" />',
        '        <Section Name="InOut" />',
        '        <Section Name="Temp" />',
        '        <Section Name="Constant" />',
        '        <Section Name="Return"><Member Name="Ret_Val" Datatype="Void" Accessibility="Public" /></Section>',
        '      </Sections></Interface>',
        '      <MemoryLayout>Optimized</MemoryLayout>',
        f'      <Name>{safe_name}</Name>',
        f'      <Number>{fc_number}</Number>',
        '      <ProgrammingLanguage>LAD</ProgrammingLanguage>',
        '    </AttributeList>',
        '    <ObjectList>',
        '      <MultilingualText ID="1" CompositionName="Comment">',
        '        <ObjectList>',
        '          <MultilingualTextItem ID="2" CompositionName="Items">',
        '            <AttributeList>',
        '              <Culture>zh-CN</Culture>',
        f'              <Text>{safe_comment}</Text>',
        '            </AttributeList>',
        '          </MultilingualTextItem>',
        '        </ObjectList>',
        '      </MultilingualText>',
        '\n'.join(networks_xml),
        '    </ObjectList>',
        '  </SW.Blocks.FC>',
        '</Document>'
    ]
    return '\r\n'.join(xml_lines)


def build_timer_db_xml(db_name: str, timer_members: List[str]) -> str:
    """生成包含 IEC_TIMER 成员的全局定时器 DB XML"""
    safe_name = db_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    member_lines = []
    for m in timer_members:
        member_lines.append(f'          <Member Name="{_escape_xml(m)}" Datatype="IEC_TIMER" Remanence="NonRetain" Accessibility="Public" />')
        
    xml_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Document>',
        '  <Engineering version="V16" />',
        '  <SW.Blocks.GlobalDB ID="0">',
        '    <AttributeList>',
        '      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v4">',
        '        <Section Name="Static">',
        '\n'.join(member_lines),
        '        </Section>',
        '      </Sections></Interface>',
        '      <MemoryLayout>Optimized</MemoryLayout>',
        f'      <Name>{safe_name}</Name>',
        '      <Number>1</Number>',
        '      <ProgrammingLanguage>DB</ProgrammingLanguage>',
        '    </AttributeList>',
        '  </SW.Blocks.GlobalDB>',
        '</Document>'
    ]
    return '\r\n'.join(xml_lines)


def build_fb_call_network(
    nw_index: int,
    title: str,
    comment: str,
    fb_name: str,
    instance_name: str,
    parameters: List[Dict[str, Any]],
    instance_scope: str = "GlobalVariable",
    en_contacts: Optional[List[Any]] = None,
) -> str:
    """
    构建标准 SimaticML V16 功能块 (FB) 带参数调用程序段（对齐 templates/FB_多重背景样板.xml）：
      parameters: [{'name': 形参名, 'section': 'Input|Output|InOut', 'type': 'Bool|Time|Int...',
                    'value': 实参名或字面值或None, 'scope': 'GlobalVariable|LocalVariable|TypedConstant|LiteralConstant'}]
    未连接实参（value 为 None 或空串）的引脚将显式绑定 <OpenCon />。
    """
    builder = LadNetworkBuilder(nw_index, title, comment)

    en_first: Optional[str] = None
    en_last: Optional[str] = None
    if en_contacts:
        en_first, en_last = _append_contact_chain(builder, en_contacts)

    param_bindings: List[Tuple[str, Optional[str]]] = []
    param_defs: List[str] = []

    for p in parameters:
        p_name = str(p.get("name", "")).strip()
        p_sec = str(p.get("section", "Input")).strip()
        p_type = str(p.get("type", "Bool")).strip()
        if not p_name:
            continue

        param_defs.append(
            f'        <Parameter Name="{_escape_xml(p_name)}" Section="{_escape_xml(p_sec)}" Type="{_escape_xml(p_type)}" />'
        )

        val = p.get("value")
        if val is None or (isinstance(val, str) and val.strip() == ""):
            param_bindings.append((p_name, None))
            continue

        val_str = str(val).strip()
        scope = str(p.get("scope", "")).strip()
        if scope == "TypedConstant" or (not scope and val_str.upper().startswith(("T#", "TIME#"))):
            acc_uid = builder.add_typed_time_constant(val_str)
        elif scope == "LiteralConstant":
            acc_uid = builder.add_literal_constant(p_type, val_str)
        else:
            acc_uid = builder.add_access(val_str, scope or "GlobalVariable")
        param_bindings.append((p_name, acc_uid))

    call_uid = builder.alloc_uid()
    inst_uid = builder.alloc_uid()
    inst_segs = [seg.strip() for seg in str(instance_name).split(".") if seg.strip()]
    if not inst_segs:
        inst_segs = [str(instance_name)]
    inst_comps_xml = "".join(
        f'          <Component Name="{_escape_xml(seg)}" />\n' for seg in inst_segs
    )
    param_xml_block = "\n".join(param_defs)
    if param_xml_block:
        param_xml_block = "\n" + param_xml_block

    call_xml = (
        f'    <Call UId="{call_uid}">\n'
        f'      <CallInfo Name="{_escape_xml(fb_name)}" BlockType="FB">\n'
        f'        <Instance Scope="{_escape_xml(instance_scope)}" UId="{inst_uid}">\n'
        f'{inst_comps_xml}'
        f'        </Instance>{param_xml_block}\n'
        f'      </CallInfo>\n'
        f'    </Call>'
    )
    builder.parts.append(call_xml)

    if en_first and en_last:
        builder.wires.append(
            f'    <Wire UId="{builder.alloc_uid()}"><Powerrail /><NameCon UId="{en_first}" Name="in" /></Wire>'
        )
        builder.wires.append(
            f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{en_last}" Name="out" /><NameCon UId="{call_uid}" Name="en" /></Wire>'
        )
    else:
        builder.wires.append(
            f'    <Wire UId="{builder.alloc_uid()}"><Powerrail /><NameCon UId="{call_uid}" Name="en" /></Wire>'
        )

    for p_name, acc_uid in param_bindings:
        if acc_uid is not None:
            builder.wires.append(
                f'    <Wire UId="{builder.alloc_uid()}"><IdentCon UId="{acc_uid}" /><NameCon UId="{call_uid}" Name="{_escape_xml(p_name)}" /></Wire>'
            )
        else:
            open_uid = builder.alloc_uid()
            builder.wires.append(
                f'    <Wire UId="{builder.alloc_uid()}"><NameCon UId="{call_uid}" Name="{_escape_xml(p_name)}" /><OpenCon UId="{open_uid}" /></Wire>'
            )

    return render_network_xml(builder)


def _render_idb_member_xml(mem: Any) -> str:
    """将 (name, datatype) 或 dict 渲染为 InstanceDB Interface Member XML"""
    if isinstance(mem, dict):
        m_name = str(mem.get("name", "")).strip()
        m_type = str(mem.get("datatype", "Bool")).strip()
        m_ver = str(mem.get("version", "")).strip()
    else:
        m_name = str(mem[0]).strip()
        m_type = str(mem[1]).strip() if len(mem) > 1 else "Bool"
        m_ver = str(mem[2]).strip() if len(mem) > 2 else ""

    ver_attr = f' Version="{_escape_xml(m_ver)}"' if m_ver else ""
    if m_type.upper() in ("TON_TIME", "TOF_TIME", "TP_TIME", "IEC_TIMER") and not m_ver:
        ver_attr = ' Version="1.0"'

    extra_sections = ""
    if m_type.upper() in ("TON_TIME", "TOF_TIME", "TP_TIME", "IEC_TIMER"):
        extra_sections = (
            '<Sections><Section Name="None">'
            '<Member Name="PT" Datatype="Time" />'
            '<Member Name="ET" Datatype="Time" />'
            '<Member Name="IN" Datatype="Bool" />'
            '<Member Name="Q" Datatype="Bool" />'
            '</Section></Sections>'
        )

    return (
        f'    <Member Name="{_escape_xml(m_name)}" Datatype="{_escape_xml(m_type)}"{ver_attr} '
        f'Remanence="NonRetain" Accessibility="Public">'
        '<AttributeList>'
        '<BooleanAttribute Name="ExternalAccessible" SystemDefined="true">true</BooleanAttribute>'
        '<BooleanAttribute Name="ExternalVisible" SystemDefined="true">true</BooleanAttribute>'
        '<BooleanAttribute Name="ExternalWritable" SystemDefined="true">true</BooleanAttribute>'
        '</AttributeList>'
        f'{extra_sections}</Member>'
    )


def build_instance_db_xml(
    idb_name: str,
    idb_number: int,
    fb_name: str,
    comment: str = "",
    input_members: Optional[List[Any]] = None,
    output_members: Optional[List[Any]] = None,
    inout_members: Optional[List[Any]] = None,
    static_members: Optional[List[Any]] = None,
) -> str:
    """生成对齐 templates/InstanceDB_样板.xml 的背景数据块 InstanceDB XML"""
    def _render_sec(sec_name: str, items: Optional[List[Any]]) -> str:
        if not items:
            return f'  <Section Name="{sec_name}" />'
        body = "\n".join(_render_idb_member_xml(it) for it in items)
        return f'  <Section Name="{sec_name}">\n{body}\n  </Section>'

    sections_xml = "\n".join([
        _render_sec("Input", input_members),
        _render_sec("Output", output_members),
        _render_sec("InOut", inout_members),
        _render_sec("Static", static_members),
    ])

    safe_comment = _escape_xml(comment)
    xml_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Document>',
        '  <Engineering version="V16" />',
        '  <SW.Blocks.InstanceDB ID="0">',
        '    <AttributeList>',
        '      <AutoNumber>true</AutoNumber>',
        '      <DBAccessibleFromOPCUA>true</DBAccessibleFromOPCUA>',
        '      <HeaderAuthor />',
        '      <HeaderFamily />',
        '      <HeaderName />',
        '      <HeaderVersion>1.0</HeaderVersion>',
        f'      <InstanceOfName>{_escape_xml(fb_name)}</InstanceOfName>',
        '      <InstanceOfType>FB</InstanceOfType>',
        '      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v4">',
        sections_xml,
        '</Sections></Interface>',
        f'      <Name>{_escape_xml(idb_name)}</Name>',
        f'      <Number>{int(idb_number)}</Number>',
        '      <ProgrammingLanguage>DB</ProgrammingLanguage>',
        '    </AttributeList>',
        '    <ObjectList>',
        '      <MultilingualText ID="1" CompositionName="Comment">',
        '        <ObjectList>',
        '          <MultilingualTextItem ID="2" CompositionName="Items">',
        '            <AttributeList>',
        '              <Culture>zh-CN</Culture>',
        f'              <Text>{safe_comment}</Text>' if safe_comment else '              <Text />',
        '            </AttributeList>',
        '          </MultilingualTextItem>',
        '        </ObjectList>',
        '      </MultilingualText>',
        '      <MultilingualText ID="3" CompositionName="Title">',
        '        <ObjectList>',
        '          <MultilingualTextItem ID="4" CompositionName="Items">',
        '            <AttributeList>',
        '              <Culture>zh-CN</Culture>',
        f'              <Text>{_escape_xml(idb_name)}</Text>',
        '            </AttributeList>',
        '          </MultilingualTextItem>',
        '        </ObjectList>',
        '      </MultilingualText>',
        '    </ObjectList>',
        '  </SW.Blocks.InstanceDB>',
        '</Document>',
    ]
    return "\r\n".join(xml_lines)


def build_line_handover_networks(
    start_nw_index: int,
    upstream_station: str,
    downstream_station: str,
    handover_prefix: str,
    upstream_done_var: str,
    downstream_ready_var: str,
    downstream_arrived_var: str,
    transfer_actuator_req_var: str,
    safety_var: str = "M_安全条件满足",
    fault_var: str = "M_报警汇总",
    compact: bool = False,
) -> List[str]:
    """
    依据 references/10_Line_Coordination_Patterns.md 第 4 节生成跨工位四步闭环交接握手 LAD 程序段：
      Step 1: 上游发起请求 ReqToNext
      Step 2: 交接传输激活 TransferActive
      Step 3: 下游到位确认 TransferDone
      Step 4: 输出交接传输执行器请求位
    compact=False（默认向后兼容）：输出 4 个独立程序段；
    compact=True（紧凑合段模式）：合并为 2 个复合程序段（NW1 请求与传输激活，NW2 到位确认与执行请求输出）。
    支持 handover_prefix 为结构体路径（如 DB_工位交接.OP10_OP20）或变量前缀。
    """
    prefix = str(handover_prefix).strip().rstrip(".")
    req_var = f"{prefix}.ReqToNext"
    active_var = f"{prefix}.TransferActive"
    done_var = f"{prefix}.TransferDone"
    fault_stop_var = f"{prefix}.FaultStop"

    if compact:
        nw_req_active = build_composite_network(
            nw_index=start_nw_index,
            title=f"NW{start_nw_index} {upstream_station}->{downstream_station} 交接请求与传输激活",
            comment=(
                f"【工位交接 Step 1+2】\n"
                f"支路1：{upstream_station} 完成且 {downstream_station} 就绪时自锁 ReqToNext。\n"
                f"支路2：双方就绪且安全无故障时置位 TransferActive 准备传输。"
            ),
            rungs=[
                {
                    "branches": [
                        [
                            {"name": upstream_done_var, "negated": False},
                            {"name": downstream_ready_var, "negated": False},
                        ],
                        [{"name": req_var, "negated": False}],
                    ],
                    "series_after": [
                        {"name": safety_var, "negated": False},
                        {"name": fault_var, "negated": True},
                        {"name": done_var, "negated": True},
                    ],
                    "coil_var": req_var,
                },
                {
                    "contacts": [
                        {"name": req_var, "negated": False},
                        {"name": downstream_ready_var, "negated": False},
                        {"name": safety_var, "negated": False},
                        {"name": fault_var, "negated": True},
                        {"name": done_var, "negated": True},
                    ],
                    "coil_var": active_var,
                },
            ],
        )
        nw_done_out = build_composite_network(
            nw_index=start_nw_index + 1,
            title=f"NW{start_nw_index + 1} {upstream_station}->{downstream_station} 接料确认与传输请求输出",
            comment=(
                f"【工位交接 Step 3+4】\n"
                f"支路1：下游传感器 {downstream_arrived_var} 到位自锁 TransferDone；撤除 {downstream_ready_var} 后释放解锁下一循环。\n"
                f"支路2：TransferActive 有效且无故障挂起时输出执行器请求。"
            ),
            rungs=[
                {
                    "branches": [
                        [
                            {"name": active_var, "negated": False},
                            {"name": downstream_arrived_var, "negated": False},
                        ],
                        [{"name": done_var, "negated": False}],
                    ],
                    "series_after": [
                        {"name": downstream_ready_var, "negated": False},
                        {"name": safety_var, "negated": False},
                        {"name": fault_var, "negated": True},
                    ],
                    "coil_var": done_var,
                },
                {
                    "contacts": [
                        {"name": active_var, "negated": False},
                        {"name": safety_var, "negated": False},
                        {"name": fault_stop_var, "negated": True},
                    ],
                    "coil_var": transfer_actuator_req_var,
                },
            ],
        )
        return [nw_req_active, nw_done_out]

    nw1 = build_or_branches_series_network(
        nw_index=start_nw_index,
        title=f"NW{start_nw_index} {upstream_station}->{downstream_station} 交接请求 (ReqToNext)",
        comment=f"【工位交接 Step 1】{upstream_station} 加工完成且 {downstream_station} 就绪时发起交接请求，完成后自动清零。",
        branches=[
            [
                {"name": upstream_done_var, "negated": False},
                {"name": downstream_ready_var, "negated": False},
            ],
            [{"name": req_var, "negated": False}],
        ],
        series_after=[
            {"name": safety_var, "negated": False},
            {"name": fault_var, "negated": True},
            {"name": done_var, "negated": True},
        ],
        coil_var=req_var,
    )

    nw2 = build_simple_series_network(
        nw_index=start_nw_index + 1,
        title=f"NW{start_nw_index + 1} {upstream_station}->{downstream_station} 传输激活 (TransferActive)",
        comment=f"【工位交接 Step 2】双方就绪且安全无故障时置位传输激活标志，接料完成后断开。",
        contacts=[
            {"name": req_var, "negated": False},
            {"name": downstream_ready_var, "negated": False},
            {"name": safety_var, "negated": False},
            {"name": fault_var, "negated": True},
            {"name": done_var, "negated": True},
        ],
        coil_var=active_var,
    )

    nw3 = build_or_branches_series_network(
        nw_index=start_nw_index + 2,
        title=f"NW{start_nw_index + 2} {downstream_station} 接料到位确认 (TransferDone)",
        comment=f"【工位交接 Step 3】下游进料传感器 {downstream_arrived_var} 到位后置位自锁 TransferDone；待下游收到确认并撤除 {downstream_ready_var}=0 时自动复位以解锁下一工件循环。",
        branches=[
            [
                {"name": active_var, "negated": False},
                {"name": downstream_arrived_var, "negated": False},
            ],
            [{"name": done_var, "negated": False}],
        ],
        series_after=[
            {"name": downstream_ready_var, "negated": False},
            {"name": safety_var, "negated": False},
            {"name": fault_var, "negated": True},
        ],
        coil_var=done_var,
    )

    nw4 = build_simple_series_network(
        nw_index=start_nw_index + 3,
        title=f"NW{start_nw_index + 3} {upstream_station}->{downstream_station} 交接传输请求输出",
        comment=f"【工位交接 Step 4】仅在传输激活、全局安全满足且无交接故障挂起时驱动传输执行器请求。",
        contacts=[
            {"name": active_var, "negated": False},
            {"name": safety_var, "negated": False},
            {"name": fault_stop_var, "negated": True},
        ],
        coil_var=transfer_actuator_req_var,
    )

    return [nw1, nw2, nw3, nw4]


def build_ob1_xml(fc_names: List[Union[str, Dict[str, Any]]]) -> str:
    """生成主循环组织块 Main (OB1) 的 LAD XML，顺序调用各 FC（亦兼容 dict 形式的 FB 调用）"""
    cu_lines = []

    for idx, item in enumerate(fc_names, start=1):
        hex_id = hex(idx * 5 + 3)[2:]
        if isinstance(item, dict):
            blk_name = str(item.get("name", "")).strip()
            blk_type = str(item.get("block_type", "FC")).strip()
            inst_name = str(item.get("instance", "")).strip()
            params = item.get("parameters") or []
            if blk_type == "FB" and inst_name:
                cu_lines.append(
                    build_fb_call_network(
                        nw_index=idx,
                        title=f"程序段 {idx}: 调用功能块 {blk_name}",
                        comment=f"调用 {blk_name} (背景数据块 {inst_name})",
                        fb_name=blk_name,
                        instance_name=inst_name,
                        parameters=params,
                    )
                )
                continue
            fc = blk_name
        else:
            fc = str(item)

        safe_fc = _escape_xml(fc)
        cu_lines.append(f'''      <SW.Blocks.CompileUnit ID="{hex_id}" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">
  <Parts>
    <Call UId="21">
      <CallInfo Name="{safe_fc}" BlockType="FC" />
    </Call>
  </Parts>
  <Wires>
    <Wire UId="22"><Powerrail /><NameCon UId="21" Name="en" /></Wire>
  </Wires>
</FlgNet></NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
        <ObjectList>
          <MultilingualText ID="{hex_id}1" CompositionName="Title">
            <ObjectList>
              <MultilingualTextItem ID="{hex_id}2" CompositionName="Items">
                <AttributeList>
                  <Culture>zh-CN</Culture>
                  <Text>程序段 {idx}: 调用功能块 {safe_fc}</Text>
                </AttributeList>
              </MultilingualTextItem>
            </ObjectList>
          </MultilingualText>
        </ObjectList>
      </SW.Blocks.CompileUnit>''')
      
    xml_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Document>',
        '  <Engineering version="V16" />',
        '  <SW.Blocks.OB ID="0">',
        '    <AttributeList>',
        '      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v4">',
        '        <Section Name="Input" />',
        '        <Section Name="Temp" />',
        '        <Section Name="Constant" />',
        '      </Sections></Interface>',
        '      <MemoryLayout>Optimized</MemoryLayout>',
        '      <Name>Main</Name>',
        '      <Number>1</Number>',
        '      <ProgrammingLanguage>LAD</ProgrammingLanguage>',
        '      <SecondaryType>ProgramCycle</SecondaryType>',
        '    </AttributeList>',
        '    <ObjectList>',
        '\n'.join(cu_lines),
        '    </ObjectList>',
        '  </SW.Blocks.OB>',
        '</Document>'
    ]
    return '\r\n'.join(xml_lines)


def write_xml_file(file_path: str, xml_content: str):
    """确保带单 UTF-8 BOM + CRLF 写入 XML 文件"""
    write_utf8_bom_crlf(file_path, xml_content)
