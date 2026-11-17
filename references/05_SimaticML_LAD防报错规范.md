# 05 SimaticML LAD 防报错规范（博途 V16）

> **第一原则：以本机导出的样板为准。** 下面的 XML 骨架只用来说明结构，命名空间版本号、元素顺序、属性名称**全部以 [templates/](../templates/) 目录中从本机博途 V16 导出的样板为准**（详见 [templates/样板结构说明.md](../templates/样板结构说明.md)）。样板与本文冲突时，以样板为准，并在汇报中说明。

## 1. 样板库（已内置于 `templates/` 目录）
本技能已包含从本机博途 V16（CPU 1214C V4.4）实测导出的全套标准样板文件（共 5 份基础样板 + 9 份高级扩充样板，全目录详见 [templates/样板结构说明.md](../templates/样板结构说明.md)）：
- **基础样板**：
  - [templates/FC_样板.xml](../templates/FC_样板.xml)：常开/常闭、自锁并联 `O`、`SCoil`/`RCoil`、TON 定时器、`PBox` 上升沿、MOVE/比较等 6 类基础程序段
  - [templates/FB_样板.xml](../templates/FB_样板.xml)：功能块 `Input`/`Output`/`InOut`/`Static`/`Temp` 接口与背景调用
  - [templates/TagTable_样板.xml](../templates/TagTable_样板.xml)：变量表样板
  - [templates/DB_样板.xml](../templates/DB_样板.xml)：`全局定时器` DB 样板
  - [templates/OB1_样板.xml](../templates/OB1_样板.xml)：OB1 调用 FC 样板
- **高级扩充样板**（按需选用，使用前查阅 [templates/样板结构说明.md](../templates/样板结构说明.md) 对应表）：
  - [templates/UDT_样板.xml](../templates/UDT_样板.xml)、[templates/InstanceDB_样板.xml](../templates/InstanceDB_样板.xml)、[templates/DB_数组与UDT样板.xml](../templates/DB_数组与UDT样板.xml)
  - [templates/FC_高级指令画廊样板.xml](../templates/FC_高级指令画廊样板.xml)（17 段高频工艺与运算指令行号索引见说明文档）、[templates/FB_多重背景样板.xml](../templates/FB_多重背景样板.xml)
  - [templates/OB100_启动样板.xml](../templates/OB100_启动样板.xml)、[templates/OB30_PID循环中断样板.xml](../templates/OB30_PID循环中断样板.xml)、[templates/FC_通信样板.xml](../templates/FC_通信样板.xml)、[templates/FC_运动控制样板.xml](../templates/FC_运动控制样板.xml)

**之后的生成器只能“克隆 + 填充”样板片段，不得新造结构。**

## 2. 文件级规则
| 规则 | 说明 |
|---|---|
| 编码 | **UTF-8（带 BOM `0xEF 0xBB 0xBF`）**，首行 `<?xml version="1.0" encoding="utf-8"?>`。Python 用 `encoding="utf-8-sig"` 写文件；**PowerShell 5 禁止使用 `Out-File` / `>` 写 XML**（默认是 UTF-16），改用 `[IO.File]::WriteAllText($p, $xml, (New-Object Text.UTF8Encoding $true))` |
| 版本 | `<Engineering version="V16" />`，不得写成 V15.1/V17 |
| 命名空间 | Interface 使用 `http://www.siemens.com/automation/Openness/SW/Interface/v4`；FlgNet 使用 `http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4` |
| 一个文件一个对象 | 一个 XML 只放一个块（FC/DB/OB）或一个变量表 |
| 文件名 | 与块名一致，可以用中文；整个路径中不要包含 `#` `%` `&` |
| 转义 | 文本中出现 `<` `>` `&` `"` 必须转义（`&lt;` 等），注释里的比较符号尤其要注意 |
| `ID` 属性 | 文档内的对象 `ID`（十六进制字符串）在整个文件内唯一 |
| `UId` 属性 | 在每个 `FlgNet`（每个程序段）内唯一，十进制整数，建议从 21 开始递增 |

## 3. FC 块骨架（结构示意）
```xml
<?xml version="1.0" encoding="utf-8"?>
<Document>
  <Engineering version="V16" />
  <SW.Blocks.FC ID="0">
    <AttributeList>
      <Interface><Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v4">
        <Section Name="Input" /><Section Name="Output" /><Section Name="InOut" />
        <Section Name="Temp" /><Section Name="Constant" />
        <Section Name="Return"><Member Name="Ret_Val" Datatype="Void" Accessibility="Public" /></Section>
      </Sections></Interface>
      <MemoryLayout>Optimized</MemoryLayout>
      <Name>FC2_1号电机正反转</Name>
      <Number>2</Number>
      <ProgrammingLanguage>LAD</ProgrammingLanguage>
    </AttributeList>
    <ObjectList>
      <MultilingualText ID="1" CompositionName="Comment">…块注释 zh-CN…</MultilingualText>
      <SW.Blocks.CompileUnit ID="3" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource><FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v4">
            <Parts> … </Parts>
            <Wires> … </Wires>
          </FlgNet></NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
        <ObjectList>
          <MultilingualText ID="4" CompositionName="Comment">
            <ObjectList>
              <MultilingualTextItem ID="5" CompositionName="Items">
                <AttributeList><Culture>zh-CN</Culture><Text>调试指导……</Text></AttributeList>
              </MultilingualTextItem>
            </ObjectList>
          </MultilingualText>
          <MultilingualText ID="6" CompositionName="Title">…程序段标题 zh-CN…</MultilingualText>
        </ObjectList>
      </SW.Blocks.CompileUnit>
      <!-- 每个程序段一个 CompileUnit -->
    </ObjectList>
  </SW.Blocks.FC>
</Document>
```

## 4. FlgNet 元件与导线规则（LAD 最容易出错的地方）
| 元素 | 写法要点 |
|---|---|
| 全局变量 | `<Access Scope="GlobalVariable" UId="21"><Symbol><Component Name="SB1_1号电机启动" /></Symbol></Access>` |
| 局部变量 / 形参 | `<Access Scope="LocalVariable" UId="21"><Symbol><Component Name="形参或Static名" /></Symbol></Access>`（用于 `FB` 或带形参 `FC` 内访问 `Input`/`Output`/`InOut`/`Static`/`Temp`；多重背景定时器成员如 `stat_KT.Q` 写为 `<Component Name="stat_KT" /><Component Name="Q" />`） |
| DB 成员 | `<Symbol><Component Name="全局定时器" /><Component Name="T1_星三角切换" /></Symbol>` |
| 时间常量 | 必须使用 `<Access Scope="TypedConstant" UId=".."><Constant><ConstantValue>T#5S</ConstantValue></Constant></Access>`（**注意：千万不可使用 `LiteralConstant`，否则报 Type mismatch**） |
| 常开触点 | `<Part Name="Contact" UId="22" />` |
| 常闭触点 | `<Part Name="Contact" UId="23"><Negated Name="operand" /></Part>` |
| 上升沿触点 | `<Part Name="PBox" UId=".." />`（**注意：LAD 部件名为 `PBox`，非 `P_TRIG`，且无 `<Instance>` 子节点**；必须按 [templates/FC_样板.xml](../templates/FC_样板.xml) NW5 将边沿存储位 `<Access>` 通过 `<Wire>` 连接到 `PBox` 的 `bit` 引脚） |
| FC 块调用 (Call) | `<Call UId="21"><CallInfo Name="FC1_安全与模式" BlockType="FC" /></Call>`（**注意：必须直接使用 `<Call>` 标签，千万不可写为 `<Part Name="Call">`**） |
| FB 块调用 (Call) | `<Call UId="21"><CallInfo Name="FB10_模板" BlockType="FB"><Instance Scope="GlobalVariable" UId="22"><Component Name="iDB_1号电机" /></Instance><Parameter Name="..." Section="Input/Output" Type="..." /></CallInfo></Call>`（多重背景时 `<Instance Scope="LocalVariable">`；**遵循 R10：首次使用新 FB/UDT/InstanceDB/工艺指令前，应先在本机博途 V16 导出 1 个最小实测样本再克隆**） |
| 线圈 / 置位 / 复位 | `Coil` / `SCoil` / `RCoil` |
| 并联（OR） | `<Part Name="O" UId=".."><TemplateValue Name="Card" Type="Cardinality">2</TemplateValue></Part>`，引脚 `in1` `in2` → `out` |
| 定时器 | `<Part Name="TON" Version="1.0" UId="..">` 内含 `<Instance Scope="GlobalVariable" UId=".."><Component Name="全局定时器" /><Component Name="T1_.." /></Instance><TemplateValue Name="time_type" Type="Type">Time</TemplateValue>`；引脚 `IN` `PT` `Q` `ET`（未连接的输出引脚如 `ET` 必须连 `<OpenCon UId=".."/>`） |

**导线（Wire）规则：**
1. 每根 `Wire` 有且只有**一个源**，可以有多个目标（一根线分叉到多个元件，就写在同一个 Wire 里）。
2. 左母线写作 `<Powerrail />`，作为源；多个程序分支从左母线开始时，放进**同一根** Powerrail Wire 的多个 `NameCon`。
3. 变量连到元件：`<Wire UId=".."><IdentCon UId="21"/><NameCon UId="22" Name="operand"/></Wire>`
4. 元件之间串联：`<NameCon UId="22" Name="out"/>` → `<NameCon UId="23" Name="in"/>`
5. 不用的输出引脚（如 TON 的 `ET`）按样板写法处理（用 `<OpenCon UId=".."/>`），**不得让引脚悬空不写**。
6. 每个 `Part` / `Call` 的必需引脚都必须有导线连接；每个 `Access` 都必须被一根 Wire 引用。
7. 一个程序段只放一条逻辑主线（一个输出或一组并列输出），复杂逻辑拆成多个程序段，便于新手阅读。

## 5. 变量表 XML
以 [templates/TagTable_样板.xml](../templates/TagTable_样板.xml) 为准，每个 `SW.Tags.PlcTag` 包含：`Name`、`DataTypeName`（Bool/Int/Real…）、`LogicalAddress`（如 `%I0.0`）、注释 `MultilingualText`（zh-CN）。I/O 与 M 分两张表：`IO变量表`、`M辅助继电器表`。
> **注意**：PLC 变量名和逻辑地址在整个 PLC 作用域（跨所有变量表）必须唯一，若博途“默认变量表”中残留同地址或同名旧变量，导入或编译时会报 `Duplicate identifier` 或 `ambiguous address`。

## 6. 导入前本地深度审计与校验（运行 `scripts/validate_lad_xml.py`，必须 0 错误通过）
- [ ] XML 格式合法，编码 UTF-8（含 BOM），`Engineering version="V16"`
- [ ] 所有程序块的 `ProgrammingLanguage` 都是 `LAD`（`OB`/`FC`/`FB` 均为 `LAD`，数据块为 `DB`，数据类型为 `UDT`），**全目录中找不到 `SCL` 代码或块声明**
- [ ] `ID` 在文件内唯一；`UId` 在每个 FlgNet 内唯一
- [ ] 每个 Wire 恰好一个源；每个 Part 的必需引脚都已连接；没有孤立的 Access；无 `<Part Name="Call">` 或 `Time` 类型的 `LiteralConstant`
- [ ] 所有 `Symbol/Component` 引用的名字都存在于变量表或 DB 中
- [ ] 所有 `MultilingualTextItem/Culture` = `zh-CN`；每个非空 CompileUnit 都有 Title 和 Comment
- [ ] 命名规则与安全规则（[02_命名与注释规范.md](02_命名与注释规范.md)、[03_安全信号校验.md](03_安全信号校验.md)）校验通过
- [ ] **电源串联路径拓扑安全审计（S3/S4/S5）**：通过有向电源流图核验每个输出线圈串联前导触点（非单纯自锁或并联分支假串联），确保直接或间接传递串联 `M_安全条件满足`、正反转软件互锁常闭以及对应热继触点（报错阻断导入）
- [ ] **死线圈检测（Dead Coil Check）**：除样板画廊外，程序段中同一驱动结果不存在无法驱动任何后级线圈的悬空中间触点链或死区输出（输出审计告警）
- [ ] **双线圈拦截（Multiple Coil Check）**：同一个全局变量严禁在普通线圈 `<Part Name="Coil">` 中被重复驱动（报 ERROR，阻断导入）
- [ ] **调用完整性（Call Integrity）**：所有生成的业务功能块 `FC` / `FB` 必须全部在 `OB1_Main.xml` 或上层 `FC`/`FB` 的 `<Call>` 中被调用（报 ERROR，阻断导入）
- [ ] **悬空实物 I/O 检查（Unused I/O Check）**：核对图纸声明的 `%I` / `%Q` 信号（非备用）是否遗漏未进入梯形图逻辑（输出审计告警）
- [ ] **M 变量读写闭环（Read/Write Balance）**：核对 `%M` 变量是否处于只写未读或只读未写的孤立状态（输出审计告警）

## 7. 常见导入报错速查（报错时翻译成中文给用户）
| 现象 / 英文关键词 | 原因 | 处理 |
|---|---|---|
| `culture … not supported` / 中文注释丢失 | 项目语言未启用 zh-CN | 让用户在“项目语言”中勾选中文 |
| 中文变成 `???` 或乱码 | 文件不是 UTF-8 / 用 PowerShell 写入 | 按第 2 节重写文件 |
| `Schema validation failed` / `namespace` | 命名空间版本与 V16 不符 | 照抄样板中的 xmlns |
| `UId … not unique` | UId 重复 | 重新编号 |
| `Wire … invalid` / 引脚错误 | 引脚名写错（`in`/`out`/`operand`/`IN`/`PT`） | 对照样板 |
| 导入成功但编译提示“未定义的变量” | 先导入了块，后导入变量表；或者名字不一致 | 按 [07_导入编译闭环.md](07_导入编译闭环.md) 规定的顺序导入，检查名字 |
| `Version` 相关错误（TON 等） | 指令版本号与样板不同 | 照抄样板中的 Version |
| `Access denied` / `EngineeringSecurityException` | 不在 Openness 用户组 / 防火墙弹窗点了拒绝 | 见 [07_导入编译闭环.md](07_导入编译闭环.md) 第 1 节 |
| `The connection with the name 'ET' is not connected` | TON 定时器的 ET 引脚未连接任何导线 | 增加一条 Wire，连接 ET 引脚与 `<OpenCon UId=".."/>` |
| `Part 中的 CallInfo 无效` | 块调用误写成了 `<Part Name="Call">` | 块调用顶层元素必须是 `<Call UId="..">`，而非 Part |
| `The value cannot be set for the parameter of the type 'Time'` | 时间常量使用了 LiteralConstant | 改用 `<Access Scope="TypedConstant"><Constant><ConstantValue>T#..S</ConstantValue></Constant></Access>` |
| `Access to ... with ambiguous address` | 多个变量表存在相同绝对地址的定义 | 清理默认变量表或重复变量表中的同地址残留定义 |
| `This function is not supported in online mode` | 博途当前处于在线监视模式 | 导入前先通过 Openness 调用 `OnlineProvider.GoOffline()` 断开在线 |
