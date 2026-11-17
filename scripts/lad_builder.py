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


def _escape_xml(val: Any) -> str:
    """转义 XML 特殊字符"""
    return (
        str(val)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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
) -> List[str]:
    """
    依据 references/10_Line_Coordination_Patterns.md 第 4 节生成跨工位四步闭环交接握手 LAD 程序段：
      Step 1: 上游发起请求 ReqToNext
      Step 2: 交接传输激活 TransferActive
      Step 3: 下游到位确认 TransferDone
      Step 4: 输出交接传输执行器请求位
    支持 handover_prefix 为结构体路径（如 DB_工位交接.OP10_OP20）或变量前缀。
    """
    prefix = str(handover_prefix).strip().rstrip(".")
    req_var = f"{prefix}.ReqToNext"
    active_var = f"{prefix}.TransferActive"
    done_var = f"{prefix}.TransferDone"
    fault_stop_var = f"{prefix}.FaultStop"

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
        comment=f"【工位交接 Step 3】下游进料传感器 {downstream_arrived_var} 到位后保持 TransferDone 直到下游复位就绪位。",
        branches=[
            [
                {"name": active_var, "negated": False},
                {"name": downstream_arrived_var, "negated": False},
            ],
            [{"name": done_var, "negated": False}],
        ],
        series_after=[
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
    """确保带单 UTF-8 BOM 写入 XML 文件"""
    os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
    crlf_content = xml_content.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(file_path, "wb") as f:
        f.write(b"\xef\xbb\xbf")
        f.write(crlf_content.encode("utf-8"))
