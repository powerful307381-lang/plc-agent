# -*- coding: utf-8 -*-
"""
模块名称: plcsim_closed_loop_runner
功能说明:
  1. 根据目标 CPU 型号与本机安装的 DLL 自动分流仿真后端：
     - S7-1500 / ET 200SP 且已安装 S7-PLCSIM Advanced Runtime DLL -> PLCSIM_ADVANCED_API
     - S7-1200 或未安装 PLCSIM Advanced DLL -> FC_SIM_FEEDBACK_LOOP
  2. 提供真实的离散扫描周期 LAD 仿真演算引擎 (LadScanCycleSimulator)，
     直接解析 SimaticML FlgNet XML 拓扑（Contact/Negated/O/PBox/NBox/TON/Coil/SCoil/RCoil/Call），
     结合 FC99_仿真自反馈 按虚拟时钟推进 PLC 扫描周期并实时审计安全不变量，
     执行 Task Success、传感器超时故障注入、安全门与机械干涉 Safety Violation 三项闭环评测。
"""

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from validate_lad_xml import build_power_graph
    from sim_feedback_builder import infer_actuator_sensor_pairs
except ImportError:
    from scripts.validate_lad_xml import build_power_graph  # type: ignore
    from scripts.sim_feedback_builder import infer_actuator_sensor_pairs  # type: ignore


DEFAULT_PLCSIM_ADV_DLL_CANDIDATES = (
    r"C:\Program Files (x86)\Common Files\Siemens\PLCSIMADV\SimRT\Siemens.Simatic.Simulation.Runtime.Api.x64.dll",
    r"C:\Program Files\Siemens\Automation\PLCSIMADV\Bin\Siemens.Simatic.Simulation.Runtime.Api.x64.dll",
    r"D:\Program Files\Siemens\Automation\PLCSIMADV\Bin\Siemens.Simatic.Simulation.Runtime.Api.x64.dll",
)


def detect_simulation_backend(
    cpu_model: str,
    plcsim_adv_dll_path: Optional[str] = None,
) -> str:
    """
    依据西门子物理仿真边界自动选择虚拟调试后端：
      - S7-1200 不支持 S7-PLCSIM Advanced API -> 强制返回 FC_SIM_FEEDBACK_LOOP
      - S7-1500 / ET 200SP 检测到 PLCSIM Advanced API DLL 时返回 PLCSIM_ADVANCED_API，否则回退 FC_SIM_FEEDBACK_LOOP
    """
    model_u = (cpu_model or "").upper()
    compact = re.sub(r"\s+", "", model_u)
    if "S7-1200" in model_u or "CPU12" in compact or "1214" in compact or "1215" in compact or "1212" in compact:
        return "FC_SIM_FEEDBACK_LOOP"

    if plcsim_adv_dll_path and os.path.exists(plcsim_adv_dll_path):
        return "PLCSIM_ADVANCED_API"

    for cand in DEFAULT_PLCSIM_ADV_DLL_CANDIDATES:
        if os.path.exists(cand):
            return "PLCSIM_ADVANCED_API"

    return "FC_SIM_FEEDBACK_LOOP"


def _parse_iec_time_to_sec(raw_val: str) -> float:
    """将 IEC Time 常量字符串（如 T#3S, T#500MS, T#1M2S）解析为秒"""
    s = (raw_val or "").strip().upper()
    if s.startswith("T#") or s.startswith("TIME#"):
        s = s.split("#", 1)[1]
    total = 0.0
    matched = False
    for num_str, unit in re.findall(r"(\d+(?:\.\d+)?)(MS|S|M|H|D)", s):
        matched = True
        val = float(num_str)
        if unit == "MS":
            total += val / 1000.0
        elif unit == "S":
            total += val
        elif unit == "M":
            total += val * 60.0
        elif unit == "H":
            total += val * 3600.0
        elif unit == "D":
            total += val * 86400.0
    if not matched:
        try:
            return float(s)
        except Exception:
            return 1.0
    return total


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1]


def _extract_symbol_path(elem: ET.Element) -> str:
    comps = [c.attrib.get("Name", "").strip() for c in elem.findall(".//{*}Component") if c.attrib.get("Name")]
    return ".".join(comps)


class _CompiledNetwork:
    """单个 CompileUnit 解析后的可执行能流网络"""

    def __init__(self, block_name: str, nw_idx: int, title: str, cu_elem: ET.Element) -> None:
        self.block_name = block_name
        self.nw_idx = nw_idx
        self.title = title
        self.parts: Dict[str, Dict[str, Any]] = {}
        self.incoming: Dict[Tuple[str, str], List[Any]] = {}
        self.operands: Dict[str, str] = {}
        self.bits: Dict[str, str] = {}
        self.pt_constants: Dict[str, float] = {}
        self.pt_vars: Dict[str, str] = {}
        self.coils: List[Tuple[str, str, str]] = []  # (uid, part_type, symbol)
        self.calls: List[Dict[str, Any]] = []  # [{uid, name, block_type, instance, param_sections, param_bindings}]
        self._parse(cu_elem)

    def _parse(self, cu_elem: ET.Element) -> None:
        flg = cu_elem.find(".//{*}FlgNet")
        if flg is None:
            return
        parts_el = flg.find("{*}Parts")
        wires_el = flg.find("{*}Wires")
        if parts_el is None or wires_el is None:
            return

        access_sym: Dict[str, str] = {}
        access_const: Dict[str, str] = {}
        access_local: Dict[str, bool] = {}
        part_info_for_graph: Dict[str, Dict[str, Any]] = {}

        for ch in parts_el:
            uid = ch.attrib.get("UId", "")
            tag = _local_tag(ch.tag)
            if not uid:
                continue
            if tag == "Access":
                scope = ch.attrib.get("Scope", "")
                if scope in ("GlobalVariable", "LocalVariable"):
                    access_sym[uid] = _extract_symbol_path(ch)
                    access_local[uid] = (scope == "LocalVariable")
                elif scope in ("TypedConstant", "LiteralConstant"):
                    cv = ch.find(".//{*}ConstantValue")
                    access_const[uid] = (cv.text or "").strip() if cv is not None else ""
            elif tag == "Call":
                ci = ch.find("{*}CallInfo")
                ci_name = ci.attrib.get("Name", "").strip('"') if ci is not None else ""
                ci_type = ci.attrib.get("BlockType", "FC") if ci is not None else "FC"
                inst_el = ci.find("{*}Instance") if ci is not None else None
                inst_sym = _extract_symbol_path(inst_el) if inst_el is not None else ""
                param_sections: Dict[str, str] = {}
                if ci is not None:
                    for p_el in ci.findall("{*}Parameter"):
                        p_nm = p_el.attrib.get("Name", "")
                        if p_nm:
                            param_sections[p_nm] = p_el.attrib.get("Section", "Input")
                cmeta = {
                    "tag": "Call",
                    "name": ci_name,
                    "block_type": ci_type,
                    "instance": inst_sym,
                    "param_sections": param_sections,
                    "param_bindings": {},
                    "elem": ch,
                }
                self.parts[uid] = cmeta
                part_info_for_graph[uid] = cmeta
                self.calls.append(cmeta)
            elif tag == "Part":
                pname = ch.attrib.get("Name", "")
                neg = ch.find("{*}Negated[@Name='operand']") is not None
                card_el = ch.find("{*}TemplateValue[@Name='Card']")
                card = int(card_el.text) if card_el is not None and (card_el.text or "").isdigit() else 2
                inst_el = ch.find("{*}Instance")
                inst_sym = _extract_symbol_path(inst_el) if inst_el is not None else ""
                inst_scope = inst_el.attrib.get("Scope", "GlobalVariable") if inst_el is not None else "GlobalVariable"
                pmeta = {
                    "tag": "Part",
                    "name": pname,
                    "negated": neg,
                    "card": card,
                    "instance": inst_sym,
                    "instance_local": (inst_scope == "LocalVariable"),
                    "elem": ch,
                }
                self.parts[uid] = pmeta
                part_info_for_graph[uid] = pmeta

        # 构建能流边（正向表 & 反向入边表）
        edges = build_power_graph(wires_el, part_info_for_graph)
        for src, dsts in edges.items():
            for dst in dsts:
                if isinstance(dst, tuple):
                    self.incoming.setdefault(dst, []).append(src)

        # 绑定操作数、边沿位、Call 参数与 PT 设定值
        for w in wires_el:
            id_uids = [sub.attrib.get("UId", "") for sub in w if _local_tag(sub.tag) == "IdentCon"]
            ncs = [(sub.attrib.get("UId", ""), sub.attrib.get("Name", "")) for sub in w if _local_tag(sub.tag) == "NameCon"]
            for iu in id_uids:
                for pu, pin in ncs:
                    pmeta = self.parts.get(pu, {})
                    if pmeta.get("tag") == "Call":
                        if iu in access_sym:
                            pmeta["param_bindings"][pin] = {
                                "kind": "var",
                                "name": access_sym[iu],
                                "local": access_local.get(iu, False),
                            }
                        elif iu in access_const:
                            pmeta["param_bindings"][pin] = {
                                "kind": "const",
                                "value": access_const[iu],
                            }
                        continue
                    if pin == "operand" and iu in access_sym:
                        sym = access_sym[iu]
                        self.operands[pu] = sym
                        pname = pmeta.get("name", "")
                        if pname in ("Coil", "SCoil", "RCoil", "NCoil"):
                            self.coils.append((pu, pname, sym))
                    elif pin == "bit" and iu in access_sym:
                        self.bits[pu] = access_sym[iu]
                    elif pin == "PT":
                        if iu in access_const:
                            self.pt_constants[pu] = _parse_iec_time_to_sec(access_const[iu])
                        elif iu in access_sym:
                            self.pt_vars[pu] = access_sym[iu]


class LadScanCycleSimulator:
    """
    真实离散扫描周期 SimaticML LAD 仿真器：
    直接根据 XML 解析后的网络拓扑执行每周期能流传播、TON 定时器累计、PBox 上升沿、FB/FC 调用及 Coil 写入。
    """

    def __init__(self, xml_dir: str, ir_data: Optional[Dict[str, Any]] = None) -> None:
        self.xml_dir = os.path.abspath(xml_dir)
        self.ir_data = ir_data or {}
        self.tag_addrs: Dict[str, str] = {}
        self.state: Dict[str, Any] = {}
        self.timers: Dict[str, Dict[str, Any]] = {}
        self.blocks: Dict[str, List[_CompiledNetwork]] = {}
        self.block_locals: Dict[str, Set[str]] = {}
        self.ob1_calls: List[Dict[str, Any]] = []
        self.ob1_order: List[str] = []
        self.elapsed_sec: float = 0.0
        self.cycle_count: int = 0
        self.violations: List[Dict[str, Any]] = []
        self._load_project()

    def _load_project(self) -> None:
        scan_dirs = [self.xml_dir]
        parent_dir = os.path.dirname(self.xml_dir)
        sibling_tag_dir = os.path.join(parent_dir, "02_变量表")
        if os.path.isdir(sibling_tag_dir):
            scan_dirs.append(sibling_tag_dir)

        xml_files: List[str] = []
        for d in scan_dirs:
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d)):
                    if fn.lower().endswith(".xml"):
                        xml_files.append(os.path.join(d, fn))

        for fp in xml_files:
            try:
                with open(fp, "r", encoding="utf-8-sig", errors="replace") as f:
                    root = ET.fromstring(f.read())
            except Exception:
                continue

            # 1. 解析变量表
            for tag_el in root.findall(".//SW.Tags.PlcTag"):
                n_el = tag_el.find("AttributeList/Name")
                a_el = tag_el.find("AttributeList/LogicalAddress")
                if n_el is not None and (n_el.text or "").strip():
                    tname = n_el.text.strip()
                    taddr = (a_el.text or "").strip().upper() if a_el is not None else ""
                    self.tag_addrs[tname] = taddr
                    self.state.setdefault(tname, False)

            # 2. 解析全局定时器 DB
            db_el = root.find(".//SW.Blocks.GlobalDB")
            if db_el is not None:
                db_name_el = db_el.find("AttributeList/Name")
                db_name = (db_name_el.text or "").strip() if db_name_el is not None else ""
                for mem in db_el.findall(".//{*}Member"):
                    m_name = mem.attrib.get("Name", "")
                    m_type = (mem.attrib.get("Datatype") or "").strip().upper()
                    if db_name and m_name and m_type in ("IEC_TIMER", "TON_TIME"):
                        full = f"{db_name}.{m_name}"
                        self.timers[full] = {"in_prev": False, "et": 0.0, "q": False}
                        self.state[f"{full}.Q"] = False

            # 3. 解析 OB1 调用顺序（支持 FC 与带实参的 FB Call）
            ob_el = root.find(".//SW.Blocks.OB")
            if ob_el is not None:
                for cu_idx, cu in enumerate(ob_el.findall(".//SW.Blocks.CompileUnit"), start=1):
                    compiled_ob = _CompiledNetwork("Main", cu_idx, f"OB1_NW{cu_idx}", cu)
                    for call_meta in compiled_ob.calls:
                        cname = call_meta.get("name", "")
                        if cname:
                            self.ob1_calls.append(call_meta)
                            if cname not in self.ob1_order:
                                self.ob1_order.append(cname)

            # 4. 解析 FC / FB 网络与局部接口成员
            blk_el = root.find(".//SW.Blocks.FC")
            if blk_el is None:
                blk_el = root.find(".//SW.Blocks.FB")
            if blk_el is not None:
                bname_el = blk_el.find("AttributeList/Name")
                bname = (bname_el.text or "").strip() if bname_el is not None else os.path.basename(fp)
                local_set: Set[str] = set()
                for mem in blk_el.findall("AttributeList/Interface/{*}Sections/{*}Section/{*}Member"):
                    mn = mem.attrib.get("Name", "")
                    if mn:
                        local_set.add(mn)
                self.block_locals[bname] = local_set
                nws: List[_CompiledNetwork] = []
                for nw_idx, cu in enumerate(blk_el.findall(".//SW.Blocks.CompileUnit"), start=1):
                    title_el = cu.find(".//MultilingualText[@CompositionName='Title']//Text")
                    title = (title_el.text or "").strip() if title_el is not None else f"NW{nw_idx}"
                    nws.append(_CompiledNetwork(bname, nw_idx, title, cu))
                self.blocks[bname] = nws

        if not self.ob1_order:
            self.ob1_order = sorted(self.blocks.keys())
        if not self.ob1_calls:
            self.ob1_calls = [
                {"name": bn, "block_type": "FC", "instance": "", "param_sections": {}, "param_bindings": {}}
                for bn in self.ob1_order
            ]

    def reset(self, init_state: Optional[Dict[str, bool]] = None) -> None:
        for k in list(self.state.keys()):
            self.state[k] = False
        for t in self.timers.values():
            t["in_prev"] = False
            t["et"] = 0.0
            t["q"] = False
        self.elapsed_sec = 0.0
        self.cycle_count = 0
        self.violations = []
        if init_state:
            self.state.update(init_state)

    def _resolve_sym(self, sym: str, call_ctx: Optional[Dict[str, Any]]) -> str:
        """在 FB/FC 局部作用域下，将局部 Static/IO/Timer 名称映射至实例化前缀"""
        if not call_ctx or not sym:
            return sym
        inst_prefix = call_ctx.get("instance", "")
        locals_set = call_ctx.get("locals", set())
        root_name = sym.split(".", 1)[0]
        if inst_prefix and root_name in locals_set:
            return f"{inst_prefix}.{sym}"
        return sym

    def _eval_network(
        self,
        nw: _CompiledNetwork,
        dt_sec: float,
        forced_inputs: Optional[Dict[str, bool]],
        call_ctx: Optional[Dict[str, Any]] = None,
    ) -> None:
        memo: Dict[Tuple[str, str], bool] = {}
        ton_updated: Set[str] = set()

        def eval_node(node: Any) -> bool:
            if node == "RAIL":
                return True
            if isinstance(node, tuple):
                return eval_pin(node[0], node[1])
            return False

        def eval_pin(uid: str, pin: str) -> bool:
            key = (uid, pin)
            if key in memo:
                return memo[key]
            pinfo = nw.parts.get(uid)
            if not pinfo:
                return False
            pname = pinfo.get("name", "")

            if pname == "Contact" and pin == "out":
                in_pwr = any(eval_node(s) for s in nw.incoming.get((uid, "in"), []))
                sym = self._resolve_sym(nw.operands.get(uid, ""), call_ctx)
                val = bool(self.state.get(sym, False))
                if pinfo["negated"]:
                    val = not val
                res = in_pwr and val
            elif pname == "O" and pin == "out":
                card = pinfo["card"]
                res = any(
                    any(eval_node(s) for s in nw.incoming.get((uid, f"in{i}"), []))
                    for i in range(1, card + 1)
                )
            elif pname in ("PBox", "NBox") and pin == "out":
                in_pwr = any(eval_node(s) for s in nw.incoming.get((uid, "in"), []))
                bit_sym = self._resolve_sym(nw.bits.get(uid, ""), call_ctx)
                prev = bool(self.state.get(bit_sym, False))
                self.state[bit_sym] = in_pwr
                res = (in_pwr and not prev) if pname == "PBox" else ((not in_pwr) and prev)
            elif pname == "TON" and pin == "Q":
                inst = self._resolve_sym(pinfo.get("instance", ""), call_ctx)
                in_pwr = any(eval_node(s) for s in nw.incoming.get((uid, "IN"), []))
                if uid not in ton_updated:
                    ton_updated.add(uid)
                    t_rec = self.timers.setdefault(inst, {"in_prev": False, "et": 0.0, "q": False})
                    pt_sec = nw.pt_constants.get(uid)
                    if pt_sec is None:
                        pt_var = self._resolve_sym(nw.pt_vars.get(uid, ""), call_ctx)
                        raw_pt = self.state.get(pt_var, 1.0)
                        pt_sec = _parse_iec_time_to_sec(raw_pt) if isinstance(raw_pt, str) else float(raw_pt or 1.0)
                    if not in_pwr:
                        t_rec["et"] = 0.0
                        t_rec["q"] = False
                    else:
                        if t_rec["in_prev"]:
                            t_rec["et"] += dt_sec
                        if t_rec["et"] + 1e-9 >= pt_sec:
                            t_rec["q"] = True
                    t_rec["in_prev"] = in_pwr
                    self.state[f"{inst}.Q"] = t_rec["q"]
                res = bool(self.timers.get(inst, {}).get("q", False))
            else:
                res = any(eval_node(s) for s in nw.incoming.get((uid, pin), []))

            memo[key] = res
            return res

        # 触发网络中的所有 TON / Call / Coil 求值
        for uid, pinfo in nw.parts.items():
            if pinfo.get("tag") == "Call":
                en_pwr = any(eval_node(s) for s in nw.incoming.get((uid, "en"), []))
                if en_pwr:
                    self._execute_call(pinfo, dt_sec, forced_inputs, parent_ctx=call_ctx)
            elif pinfo.get("name") == "TON":
                eval_pin(uid, "Q")

        for cu_id, ctype, raw_sym in nw.coils:
            sym = self._resolve_sym(raw_sym, call_ctx)
            if forced_inputs and sym in forced_inputs:
                continue
            coil_in = any(eval_node(s) for s in nw.incoming.get((cu_id, "in"), []))
            if ctype == "Coil":
                self.state[sym] = coil_in
            elif ctype == "SCoil" and coil_in:
                self.state[sym] = True
            elif ctype == "RCoil" and coil_in:
                self.state[sym] = False
            elif ctype == "NCoil":
                self.state[sym] = not coil_in

    def _execute_call(
        self,
        call_meta: Dict[str, Any],
        dt_sec: float,
        forced_inputs: Optional[Dict[str, bool]],
        parent_ctx: Optional[Dict[str, Any]] = None,
    ) -> None:
        callee_name = call_meta.get("name", "")
        if callee_name not in self.blocks:
            return
        inst_name = self._resolve_sym(call_meta.get("instance", "") or callee_name, parent_ctx)
        locals_set = self.block_locals.get(callee_name, set())
        sections = call_meta.get("param_sections") or {}
        bindings = call_meta.get("param_bindings") or {}

        # 1. 将 Input / InOut 实参拷入被调块实例成员
        for p_name, p_bind in bindings.items():
            p_sec = sections.get(p_name, "Input")
            if p_sec not in ("Input", "InOut"):
                continue
            if p_bind.get("kind") == "var":
                src_sym = self._resolve_sym(p_bind["name"], parent_ctx)
                self.state[f"{inst_name}.{p_name}"] = self.state.get(src_sym, False)
            elif p_bind.get("kind") == "const":
                self.state[f"{inst_name}.{p_name}"] = p_bind.get("value", "")

        # 2. 执行被调块内部各网络
        sub_ctx = {"instance": inst_name, "locals": locals_set} if call_meta.get("block_type") == "FB" else None
        for nw in self.blocks.get(callee_name, []):
            self._eval_network(nw, dt_sec, forced_inputs, call_ctx=sub_ctx)

        # 3. 将 Output / InOut 成员写回实参变量
        for p_name, p_bind in bindings.items():
            p_sec = sections.get(p_name, "Input")
            if p_sec not in ("Output", "InOut") or p_bind.get("kind") != "var":
                continue
            dst_sym = self._resolve_sym(p_bind["name"], parent_ctx)
            if forced_inputs and dst_sym in forced_inputs:
                continue
            self.state[dst_sym] = self.state.get(f"{inst_name}.{p_name}", False)

    def _audit_cycle_safety(self) -> None:
        """每扫描周期结束后校验物理安全链与安全联锁矩阵不变量"""
        # 1. 安全输入（急停/安全门）断开时严禁任何非指示灯物理执行器 %Q 得电
        safety_broken = False
        broken_reasons: List[str] = []
        for tname, taddr in self.tag_addrs.items():
            if taddr.startswith("%I") and any(k in tname for k in ("急停", "安全门", "光栅", "热继")):
                if not self.state.get(tname, False):
                    safety_broken = True
                    broken_reasons.append(f"{tname}=0")

        if safety_broken:
            for tname, taddr in self.tag_addrs.items():
                if taddr.startswith("%Q") and not any(k in tname for k in ("指示", "灯", "报警", "HL", "PF")):
                    if self.state.get(tname, False):
                        self.violations.append({
                            "cycle": self.cycle_count,
                            "time_sec": round(self.elapsed_sec, 3),
                            "type": "SAFETY_GATE_OR_ESTOP_VIOLATION",
                            "actuator": tname,
                            "reason": f"安全信号断开 ({', '.join(broken_reasons)}) 时执行器 {tname} 仍得电！",
                        })

        # 2. 安全联锁矩阵不变量实时核查
        for rule in self.ir_data.get("interlocks", []):
            act = str(rule.get("actuator", "")).strip()
            if not act or not self.state.get(act, False):
                continue
            for perm in rule.get("required_permissives", []):
                if perm and not self.state.get(perm, False):
                    self.violations.append({
                        "cycle": self.cycle_count,
                        "time_sec": round(self.elapsed_sec, 3),
                        "type": "INTERLOCK_PERMISSIVE_VIOLATION",
                        "actuator": act,
                        "missing_permissive": perm,
                        "reason": f"t={self.elapsed_sec:.2f}s: 执行器 {act}=1 时允许条件 {perm}=0！",
                    })
            for mtx in rule.get("mutex_coils", []):
                if mtx and self.state.get(mtx, False):
                    self.violations.append({
                        "cycle": self.cycle_count,
                        "time_sec": round(self.elapsed_sec, 3),
                        "type": "INTERLOCK_MUTEX_VIOLATION",
                        "actuator": act,
                        "mutex_coil": mtx,
                        "reason": f"t={self.elapsed_sec:.2f}s: 执行器 {act} 与互斥线圈 {mtx} 同时得电！",
                    })

    def step_cycle(self, dt_sec: float = 0.25, forced_inputs: Optional[Dict[str, bool]] = None) -> Dict[str, bool]:
        self.cycle_count += 1
        self.elapsed_sec += dt_sec

        if forced_inputs:
            self.state.update(forced_inputs)

        for call_meta in self.ob1_calls:
            if call_meta.get("block_type") == "FB" and (call_meta.get("param_bindings") or call_meta.get("instance")):
                self._execute_call(call_meta, dt_sec, forced_inputs, parent_ctx=None)
            else:
                blk_name = call_meta.get("name", "")
                for nw in self.blocks.get(blk_name, []):
                    self._eval_network(nw, dt_sec, forced_inputs)
            # 若外部强制某信号卡死（如故障注入），防止 FC99_仿真自反馈 改写该故障点
            if forced_inputs:
                self.state.update(forced_inputs)

        self._audit_cycle_safety()
        return dict(self.state)


def _infer_sim_defaults(sim: LadScanCycleSimulator, ir_data: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, bool], List[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """从工艺状态机 IR 与工程变量表自动推导初始安全条件、原点传感器、步序位与故障测试目标"""
    init_ok: Dict[str, bool] = {}
    step_vars: List[str] = []
    start_btn: Optional[str] = None
    manual_jog_btn: Optional[str] = None
    mode_sel: Optional[str] = None
    alarm_lamp: Optional[str] = None

    # 1. 优先从工艺 IR 的 origin_conditions 与 safety_inputs 注入初始安全/原点前提
    for si in (ir_data or {}).get("safety_inputs", []):
        sname = si.get("name", "")
        if sname:
            init_ok[sname] = True
    for oc in (ir_data or {}).get("origin_conditions", []):
        oname = oc.get("name", "")
        if oname:
            init_ok[oname] = not bool(oc.get("negated", False))

    # 2. 结合变量表补全安全/模式/按钮/步序位
    for tname, taddr in sim.tag_addrs.items():
        if taddr.startswith("%I"):
            if any(k in tname for k in ("急停", "安全门", "光栅", "热继", "极限", "限位", "锁紧", "ESTOP", "DOOR")):
                init_ok.setdefault(tname, True)
            elif any(k in tname for k in ("缩回", "上到位", "原位", "零位", "HOME")):
                init_ok.setdefault(tname, True)
            elif "手自动" in tname or "SA" in tname.upper() or "MODE" in tname.upper():
                mode_sel = tname
                init_ok[tname] = True
            elif ("启动" in tname or "START" in tname.upper()) and ("SB" in tname.upper() or "BTN" in tname.upper()):
                start_btn = tname
            elif "点动" in tname or "手动" in tname or "JOG" in tname.upper():
                if manual_jog_btn is None:
                    manual_jog_btn = tname
        elif taddr.startswith("%Q"):
            if any(k in tname for k in ("指示", "灯", "报警", "HL", "PF", "LAMP", "ALARM")):
                alarm_lamp = tname
        elif taddr.startswith("%M"):
            if "步序" in tname or "STEP" in tname.upper():
                step_vars.append(tname)
            elif "仿真" in tname or "SIM" in tname.upper():
                init_ok[tname] = True

    step_vars.sort()
    return init_ok, step_vars, start_btn, manual_jog_btn, mode_sel, alarm_lamp


def _select_timeout_target_from_ir(sim: LadScanCycleSimulator, ir_data: Dict[str, Any]) -> Optional[str]:
    """优先从 ir_data['steps'] 或执行器配对动态推导故障注入目标传感器，彻底消除中文关键词硬编码"""
    steps = ir_data.get("steps") or []
    # 1. 优先挑选带有 timeout/alarm_var 定义的工步对应的 %I 转移到位传感器
    candidates: List[str] = []
    for st in steps:
        if not isinstance(st, dict):
            continue
        has_alarm = bool(st.get("alarm_var") or st.get("timeout"))
        for tv in (st.get("transition_vars") or []):
            clean_tv = str(tv).split("=")[0].strip("` ")
            addr = sim.tag_addrs.get(clean_tv, "")
            if addr.startswith("%I"):
                if has_alarm:
                    candidates.append(clean_tv)
                elif clean_tv not in candidates:
                    candidates.append(clean_tv)
    if candidates:
        return candidates[-1]

    # 2. 次优通过 sim_feedback_builder 的执行器-传感器配对推导工作到位传感器
    io_tags = ir_data.get("io_tags") or [
        {"name": n, "logical_address": a} for n, a in sim.tag_addrs.items()
    ]
    pairs = infer_actuator_sensor_pairs(io_tags, ir_data=ir_data)
    for p in reversed(pairs):
        ws = p.get("work_sensor")
        if ws and ws in sim.tag_addrs:
            return ws

    # 3. 兜底匹配非原点工作到位信号
    return next(
        (t for t, a in sim.tag_addrs.items() if a.startswith("%I") and ("下到位" in t or "伸出到位" in t or "工作位" in t)),
        None,
    )


def _select_interlock_fault_target(sim: LadScanCycleSimulator, ir_data: Dict[str, Any], fallback_jog_btn: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """从 ir_data['interlocks'] 动态选取非安全总线的机械防撞允许条件及对应手动点动输入"""
    for rule in (ir_data.get("interlocks") or []):
        if not isinstance(rule, dict):
            continue
        act = str(rule.get("actuator", "")).strip()
        for perm in (rule.get("required_permissives") or []):
            p = str(perm).strip()
            if p and p not in ("M_安全条件满足", "M_急停正常") and p in sim.tag_addrs:
                # 若能找到对应执行器同一网络中的手动点动输入则优先使用
                jog_btn = fallback_jog_btn
                for blk_nws in sim.blocks.values():
                    for nw in blk_nws:
                        if any(c_sym == act for _, _, c_sym in nw.coils):
                            for op in nw.operands.values():
                                addr = sim.tag_addrs.get(op, "")
                                if addr.startswith("%I") and op != p and not any(k in op for k in ("急停", "安全门", "热继")):
                                    jog_btn = op
                                    break
                return p, jog_btn

    fallback_home = next((t for t in sim.tag_addrs if "上到位" in t or "零位" in t), None)
    return fallback_home, fallback_jog_btn


def _try_run_plcsim_advanced_api(
    xml_dir: str,
    ir_data: Dict[str, Any],
    cpu_model: str,
    dll_path: Optional[str],
) -> Optional[Dict[str, Any]]:
    """
    尝试连接真实安装的 S7-PLCSIM Advanced Runtime API DLL (ISimulationRuntimeManager)。
    - 若 DLL 仅为测试占位文件（非 PE 'MZ' 头）或本机未安装 pythonnet/未启动运行实例，返回 None 触发无缝降级；
    - 若真实 DLL 与运行实例就绪，通过 Runtime API 执行变量探活并返回实时通道结果。
    """
    resolved_dll = dll_path
    if not resolved_dll:
        for cand in DEFAULT_PLCSIM_ADV_DLL_CANDIDATES:
            if os.path.exists(cand):
                resolved_dll = cand
                break
    if not resolved_dll or not os.path.exists(resolved_dll):
        return None

    try:
        if os.path.getsize(resolved_dll) < 10240:
            return None
        with open(resolved_dll, "rb") as f:
            if f.read(2) != b"MZ":
                return None
    except OSError:
        return None

    # 优先尝试 pythonnet (clr) 直接挂载 ISimulationRuntimeManager
    try:
        import clr  # type: ignore

        clr.AddReference(resolved_dll)
        from Siemens.Simatic.Simulation.Runtime import SimulationRuntimeManager  # type: ignore

        reg_infos = list(SimulationRuntimeManager.RegisteredInstanceInfo)
        if reg_infos:
            inst_name = reg_infos[0].Name
            instance = SimulationRuntimeManager.CreateInterface(inst_name)
            instance.UpdateTagList()
            return {
                "connected": True,
                "instance_name": str(inst_name),
                "dll_path": resolved_dll,
                "operating_state": str(getattr(instance, "OperatingState", "Unknown")),
            }
    except Exception:
        pass

    # 若无 pythonnet，尝试通过 PowerShell 加载同一 Runtime DLL 查询已注册仿真实例
    ps_cmd = (
        f"[System.Reflection.Assembly]::LoadFrom('{resolved_dll}') | Out-Null; "
        "$m = [Siemens.Simatic.Simulation.Runtime.SimulationRuntimeManager]::RegisteredInstanceInfo; "
        "if ($m.Count -gt 0) { Write-Output $m[0].Name }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        inst_name = (proc.stdout or "").strip()
        if proc.returncode == 0 and inst_name:
            return {
                "connected": True,
                "instance_name": inst_name,
                "dll_path": resolved_dll,
                "operating_state": "ProbedViaPowerShell",
            }
    except Exception:
        pass

    return None


def run_closed_loop_evaluation(
    xml_dir: str,
    ir_data: Dict[str, Any],
    cpu_model: str = "CPU 1214C DC/DC/DC",
    plcsim_adv_dll_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    执行完整三项闭环虚拟调试评测：
      1. Task Success：验证全周期自动步序转移与完成度
      2. Sensor Timeout Fault Injection：在工作步阻断到位传感器超过阈值，验证超时报警置位
      3. Safety Violation Check：在运行中注入急停/安全门断开及跨气缸机械干涉，统计安全违规数
    """
    backend = detect_simulation_backend(cpu_model, plcsim_adv_dll_path=plcsim_adv_dll_path)
    plcsim_adv_probe = None
    runtime_channel = "LadScanCycleSimulator"
    if backend == "PLCSIM_ADVANCED_API":
        plcsim_adv_probe = _try_run_plcsim_advanced_api(xml_dir, ir_data, cpu_model, plcsim_adv_dll_path)
        runtime_channel = (
            "PLCSIM_ADVANCED_NATIVE" if plcsim_adv_probe else "LadScanCycleSimulator (PLCSIM_ADV_FALLBACK)"
        )

    sim = LadScanCycleSimulator(xml_dir, ir_data=ir_data)
    init_ok, step_vars, start_btn, manual_jog_btn, mode_sel, alarm_lamp = _infer_sim_defaults(sim, ir_data=ir_data)

    trace_log: List[Dict[str, Any]] = []
    all_violations: List[Dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # Scenario 1: 正常全周期自动步序 Task Success 验证
    # -------------------------------------------------------------------------
    sim.reset(init_ok)
    visited_steps: Set[str] = set()
    for cyc in range(1, 49):
        forced = {}
        if start_btn:
            forced[start_btn] = (cyc <= 2)
        st = sim.step_cycle(dt_sec=0.25, forced_inputs=forced)
        active_steps = [sv for sv in step_vars if st.get(sv, False)]
        visited_steps.update(active_steps)
        active_qs = [q for q, addr in sim.tag_addrs.items() if addr.startswith("%Q") and st.get(q, False)]
        if active_steps or active_qs or cyc in (1, 48):
            trace_log.append({
                "scenario": "TaskSuccess",
                "cycle": cyc,
                "t": round(sim.elapsed_sec, 2),
                "active_steps": active_steps,
                "active_outputs": active_qs,
            })

    returned_idle = not any(sim.state.get(sv, False) for sv in step_vars)
    task_success_rate = (len(visited_steps) / len(step_vars)) if step_vars else 0.0
    all_violations.extend(sim.violations)

    # -------------------------------------------------------------------------
    # Scenario 2: 传感器卡阻超时故障注入验证（由 IR / 配对关系动态推导目标传感器）
    # -------------------------------------------------------------------------
    sim.reset(init_ok)
    blocked_sensor = _select_timeout_target_from_ir(sim, ir_data)
    timeout_alarm_caught = False
    for cyc in range(1, 40):
        forced = {}
        if start_btn:
            forced[start_btn] = (cyc <= 2)
        if blocked_sensor:
            forced[blocked_sensor] = False
        st = sim.step_cycle(dt_sec=0.25, forced_inputs=forced)
        alarm_on = any(st.get(k, False) for k in st if ("报警" in k or "ALARM" in k.upper()) and k != "M_报警汇总") or st.get("M_报警汇总", False)
        lamp_on = st.get(alarm_lamp, False) if alarm_lamp else True
        if alarm_on and lamp_on:
            timeout_alarm_caught = True
            trace_log.append({
                "scenario": "TimeoutFaultInjection",
                "cycle": cyc,
                "t": round(sim.elapsed_sec, 2),
                "blocked_sensor": blocked_sensor,
                "alarm_triggered": True,
            })
            break
    all_violations.extend(sim.violations)

    # -------------------------------------------------------------------------
    # Scenario 3: 急停/安全门打开 + 机械防撞跨机构干涉 Safety Violation 检测
    # -------------------------------------------------------------------------
    # 3A: 自动运行途中注入安全门打开与急停拍下
    sim.reset(init_ok)
    door_tag = next((t for t in sim.tag_addrs if any(k in t for k in ("安全门", "光栅", "防护罩", "DOOR"))), None)
    estop_tag = next((t for t in sim.tag_addrs if any(k in t for k in ("急停", "ESTOP"))), None)
    for cyc in range(1, 20):
        forced = {}
        if start_btn:
            forced[start_btn] = (cyc <= 2)
        if cyc >= 6 and door_tag:
            forced[door_tag] = False
        if cyc >= 12 and estop_tag:
            forced[estop_tag] = False
        sim.step_cycle(dt_sec=0.25, forced_inputs=forced)
    all_violations.extend(sim.violations)

    # 3B: 手动模式下按 IR 安全联锁矩阵动态阻断非安全允许条件，强行点动对应执行器
    manual_init = dict(init_ok)
    if mode_sel:
        manual_init[mode_sel] = False  # 切至手动模式
    sim.reset(manual_init)
    interlock_block_tag, target_jog_btn = _select_interlock_fault_target(sim, ir_data, manual_jog_btn)
    for cyc in range(1, 8):
        forced = {}
        if mode_sel:
            forced[mode_sel] = False
        if interlock_block_tag:
            forced[interlock_block_tag] = False
        if target_jog_btn:
            forced[target_jog_btn] = True
        sim.step_cycle(dt_sec=0.25, forced_inputs=forced)
    all_violations.extend(sim.violations)

    safety_violation_count = len(all_violations)
    passed = (
        task_success_rate == 1.0
        and returned_idle
        and timeout_alarm_caught
        and safety_violation_count == 0
    )

    return {
        "backend": backend,
        "runtime_channel": runtime_channel,
        "plcsim_adv_probe": plcsim_adv_probe,
        "cpu_model": cpu_model,
        "task_success_rate": round(task_success_rate, 4),
        "returned_idle": returned_idle,
        "visited_steps": sorted(visited_steps),
        "expected_steps": step_vars,
        "blocked_sensor": blocked_sensor,
        "interlock_block_tag": interlock_block_tag,
        "timeout_alarm_caught": timeout_alarm_caught,
        "safety_violation_count": safety_violation_count,
        "violations_detail": all_violations,
        "trace_log": trace_log,
        "passed": passed,
    }
