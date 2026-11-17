param(
    [string]$Action = "status",
    [int]$ProcessIndex = -1,
    [string]$ProjectPath = "",
    [string]$PlcName = "",
    [string]$TargetName = "",
    [string]$ExpectedAddresses = "",
    [string]$ExportDir = "",
    [string]$LibraryPath = "",
    [string]$LibraryName = "",
    [string]$BlockNames = "",
    [string]$AmlPath = "",
    [string]$CaxLogPath = "",
    [string]$ImportOption = "RetainTiaDevice",
    [string]$WatchXmlDir = "",
    [switch]$BindWebServer,
    [switch]$ShowInEditor,
    [string]$DownloadMode = "Software",
    [string]$ConfirmOnlineToken = "",
    [switch]$HumanApproved,
    [switch]$FirstManualDownloadDone,
    [string]$ToParamsPath = ""
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Resolve-OpennessDllPath {
    # 1. 按版本号降序动态探测 TIA Portal V20 ~ V16 注册表安装路径与 PublicAPI 目录 (兼容 64/32 位视图)
    $versions = @("20", "19", "18", "17", "16")
    foreach ($ver in $versions) {
        $regSubKey = "SOFTWARE\Siemens\Automation\_InstalledSW\TIAP$ver\Global"
        foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64, [Microsoft.Win32.RegistryView]::Registry32)) {
            try {
                $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, $view)
                $subKey = $baseKey.OpenSubKey($regSubKey)
                if ($subKey) {
                    $installPath = $subKey.GetValue("Path")
                    if ($installPath) {
                        $candidate = Join-Path $installPath "PublicAPI\V$ver\Siemens.Engineering.dll"
                        if (Test-Path $candidate) { return $candidate }
                    }
                }
            } catch {}
        }
    }
    # 2. 按版本号降序检查默认安装路径与 Junction 映射目录 (V20 ~ V16)
    foreach ($ver in $versions) {
        $defaultCandidate = "C:\Program Files\Siemens\Automation\Portal V$ver\PublicAPI\V$ver\Siemens.Engineering.dll"
        if (Test-Path $defaultCandidate) { return $defaultCandidate }
    }
    # 3. 保底返回 V16 标准路径（供后续 Test-Path 报告明确缺失提示）
    return "C:\Program Files\Siemens\Automation\Portal V16\PublicAPI\V16\Siemens.Engineering.dll"
}

$dllPath = Resolve-OpennessDllPath
$dllOk = Test-Path $dllPath
if ($dllOk) {
    [System.Reflection.Assembly]::LoadFrom($dllPath) | Out-Null
}

function Get-PlcSoftwareFromDeviceItem($deviceItem) {
    $methods = $deviceItem.GetType().GetMethods() | Where-Object { $_.Name -eq "GetService" -and $_.IsGenericMethod }
    if ($methods) {
        try {
            $swCont = $methods[0].MakeGenericMethod(@([Siemens.Engineering.HW.Features.SoftwareContainer])).Invoke($deviceItem, $null)
            if ($swCont -and $swCont.Software) {
                return $swCont.Software
            }
        } catch {}
    }
    foreach ($sub in $deviceItem.DeviceItems) {
        $res = Get-PlcSoftwareFromDeviceItem $sub
        if ($res) { return $res }
    }
    return $null
}

function Get-AllProjectDevices($proj) {
    $list = @()
    foreach ($d in $proj.Devices) { $list += $d }
    function Walk-DeviceGroup($grp) {
        $res = @()
        foreach ($d in $grp.Devices) { $res += $d }
        foreach ($sub in $grp.Groups) { $res += Walk-DeviceGroup $sub }
        return $res
    }
    foreach ($g in $proj.DeviceGroups) { $list += Walk-DeviceGroup $g }
    if ($proj.UngroupedDevicesGroup) {
        foreach ($d in $proj.UngroupedDevicesGroup.Devices) { $list += $d }
    }
    return $list
}

function Find-BlockRecursive($group, [string]$name) {
    $b = $group.Blocks.Find($name)
    if ($b) { return $b }
    foreach ($sub in $group.Groups) {
        $res = Find-BlockRecursive $sub $name
        if ($res) { return $res }
    }
    return $null
}

function Find-TagTableRecursive($group, [string]$name) {
    $tt = $group.TagTables.Find($name)
    if ($tt) { return $tt }
    foreach ($sub in $group.Groups) {
        $res = Find-TagTableRecursive $sub $name
        if ($res) { return $res }
    }
    return $null
}

function Get-PlcTargetFromDeviceItem($dev, $deviceItem) {
    $methods = $deviceItem.GetType().GetMethods() | Where-Object { $_.Name -eq "GetService" -and $_.IsGenericMethod }
    if ($methods) {
        try {
            $swCont = $methods[0].MakeGenericMethod(@([Siemens.Engineering.HW.Features.SoftwareContainer])).Invoke($deviceItem, $null)
            if ($swCont -and $swCont.Software -and ($null -ne $swCont.Software.PSObject.Properties["BlockGroup"])) {
                return [PSCustomObject]@{
                    Device         = $dev
                    CpuItem        = $deviceItem
                    DeviceName     = $dev.Name
                    PlcName        = $deviceItem.Name
                    TypeIdentifier = "$($deviceItem.TypeIdentifier)"
                    Software       = $swCont.Software
                }
            }
        } catch {}
    }
    foreach ($sub in $deviceItem.DeviceItems) {
        $res = Get-PlcTargetFromDeviceItem $dev $sub
        if ($res) { return $res }
    }
    return $null
}

function Select-TiaProcess($procs, [int]$procIdx, [string]$projPath) {
    if (-not $procs -or $procs.Count -eq 0) {
        Write-Host (ConvertTo-Json -Depth 4 @{ Success = $false; Error = "未找到运行中的博途进程" })
        exit 1
    }
    $candidates = @()
    for ($i = 0; $i -lt $procs.Count; $i++) {
        $p = $procs[$i]
        $candidates += [PSCustomObject]@{
            Index       = $i
            ProcessId   = $p.Id
            ProjectPath = if ($p.ProjectPath) { $p.ProjectPath.FullName } else { "" }
        }
    }

    if ($projPath) {
        $trimmed = $projPath.Trim().Trim('"')
        $normReq = $trimmed
        try { $normReq = [System.IO.Path]::GetFullPath($trimmed) } catch {}
        $matchedProcs = @()
        for ($i = 0; $i -lt $procs.Count; $i++) {
            $pp = if ($procs[$i].ProjectPath) { $procs[$i].ProjectPath.FullName } else { "" }
            if (-not $pp) { continue }
            $normPp = $pp
            try { $normPp = [System.IO.Path]::GetFullPath($pp) } catch {}
            if ($normPp -ieq $normReq -or $pp -ieq $trimmed -or ([System.IO.Path]::GetFileName($pp) -ieq $trimmed)) {
                $matchedProcs += $procs[$i]
            }
        }
        if ($matchedProcs.Count -eq 1) { return $matchedProcs[0] }
        if ($matchedProcs.Count -eq 0) {
            Write-Host ([PSCustomObject]@{
                Success    = $false
                Error      = "未找到打开目标项目 '$projPath' 的博途进程"
                Candidates = $candidates
            } | ConvertTo-Json -Depth 4)
            exit 2
        }
        Write-Host ([PSCustomObject]@{
            Success    = $false
            Error      = "存在多个匹配 '$projPath' 的博途进程，请改用 -ProcessIndex 显式指定"
            Candidates = $candidates
        } | ConvertTo-Json -Depth 4)
        exit 2
    }

    if ($procIdx -ge 0) {
        if ($procIdx -ge $procs.Count) {
            Write-Host ([PSCustomObject]@{
                Success    = $false
                Error      = "指定的 ProcessIndex=$procIdx 越界（共 $($procs.Count) 个博途进程）"
                Candidates = $candidates
            } | ConvertTo-Json -Depth 4)
            exit 2
        }
        return $procs[$procIdx]
    }

    if ($procs.Count -eq 1) { return $procs[0] }

    Write-Host ([PSCustomObject]@{
        Success    = $false
        Error      = "检测到 $($procs.Count) 个运行中的博途进程，请显式传入 -ProcessIndex 或 -ProjectPath"
        Candidates = $candidates
    } | ConvertTo-Json -Depth 4)
    exit 2
}

function Select-PlcSoftware($proj, [string]$plcName) {
    $allDevs = Get-AllProjectDevices $proj
    $targets = @()
    foreach ($dev in $allDevs) {
        $t = Get-PlcTargetFromDeviceItem $dev $dev
        if ($t) { $targets += $t }
    }
    if ($targets.Count -eq 0) {
        Write-Host (ConvertTo-Json -Depth 4 @{ Success = $false; Error = "未找到 PLC 软件容器 (SoftwareContainer)" })
        exit 1
    }
    $candidates = @($targets | ForEach-Object {
        [PSCustomObject]@{
            DeviceName     = $_.DeviceName
            PlcName        = $_.PlcName
            TypeIdentifier = $_.TypeIdentifier
        }
    })
    if ($plcName) {
        $matched = @($targets | Where-Object { $_.PlcName -ieq $plcName -or $_.DeviceName -ieq $plcName })
        if ($matched.Count -eq 1) { return $matched[0] }
        if ($matched.Count -eq 0) {
            Write-Host ([PSCustomObject]@{
                Success    = $false
                Error      = "未找到名为 '$plcName' 的 PLC 设备或 CPU"
                Candidates = $candidates
            } | ConvertTo-Json -Depth 4)
            exit 2
        }
        Write-Host ([PSCustomObject]@{
            Success    = $false
            Error      = "名为 '$plcName' 的 PLC 匹配到多台设备，请使用唯一的站点/CPU 名称"
            Candidates = $candidates
        } | ConvertTo-Json -Depth 4)
        exit 2
    }
    if ($targets.Count -eq 1) { return $targets[0] }
    Write-Host ([PSCustomObject]@{
        Success    = $false
        Error      = "项目中包含 $($targets.Count) 台 PLC，请显式传入 -PlcName 指定目标 PLC"
        Candidates = $candidates
    } | ConvertTo-Json -Depth 4)
    exit 2
}

# 动作 1: 列出博途进程
if ($Action -eq "list-processes") {
    # 严禁对 GetProcesses() 返回的句柄调用 Dispose()，否则会直接杀死博途实例
    $procs = [Siemens.Engineering.TiaPortal]::GetProcesses()
    $result = @()
    for ($i = 0; $i -lt $procs.Count; $i++) {
        $p = $procs[$i]
        $result += [PSCustomObject]@{
            Index = $i
            ProcessId = $p.Id
            ProjectPath = if ($p.ProjectPath) { $p.ProjectPath.FullName } else { "" }
        }
    }
    $result | ConvertTo-Json
    exit 0
}

# 动作 2: 环境自检与硬件架构深度巡检 (模块 2: 导出各模块槽位与起始/终止地址)
if ($Action -eq "inspect-env" -or $Action -eq "compare-io") {
    $dllVer = if ($dllOk) { [System.Diagnostics.FileVersionInfo]::GetVersionInfo($dllPath).FileVersion } else { "" }
    $junctionOk = @(@("20", "19", "18", "17", "16") | Where-Object { Test-Path "C:\Program Files\Siemens\Automation\Portal V$_\PublicAPI\V$_\Siemens.Engineering.dll" }).Count -gt 0
    
    $groups = whoami /groups
    $inGroup = ($groups -match "Siemens TIA Openness").Count -gt 0
    
    $procs = if ($dllOk) { [Siemens.Engineering.TiaPortal]::GetProcesses() } else { @() }
    $hasProc = ($procs.Count -gt 0)
    
    $projInfo = $null
    $plcSoftwareOk = $false
    $cpuModel = ""
    $ioList = @()
    $hardwareModules = @()
    $languages = @()
    $sysMemInfo = [PSCustomObject]@{ SystemMemoryEnabled = $false; SystemMemoryByte = -1; ClockMemoryEnabled = $false; ClockMemoryByte = -1 }

    if ($hasProc) {
        $targetProc = Select-TiaProcess $procs $ProcessIndex $ProjectPath
        try {
            $tia = $targetProc.Attach()
            if ($tia.Projects.Count -eq 0) {
                $projInfo = [PSCustomObject]@{ Error = "博途进程已启动，但尚未打开任何项目（或缺失项目所需的可选包）" }
            } else {
                $proj = $tia.Projects[0]
                
                foreach ($l in $proj.LanguageSettings.Languages) {
                    $languages += $l.Culture.Name
                }
                
                function Collect-HwModulesRecursive($di, $seenKeys, [ref]$ioRef, [ref]$modRef) {
                    if (-not $di) { return }
                    $subRanges = @()
                    try {
                        foreach ($addr in $di.Addresses) {
                            $ioTypeStr = "$($addr.IoType)"
                            $startA = [int]$addr.StartAddress
                            # Openness Address.Length 的单位是位 (bit)，不是字节 (byte)
                            $lenBits = [int]$addr.Length
                            $effBits = $lenBits
                            if ($ioTypeStr -eq "Input" -and $di.Name -match "(?i)\bDI\s*(\d+)") {
                                $phys = [int]$matches[1]
                                if ($phys -gt 0 -and $phys -le $lenBits) { $effBits = $phys }
                            } elseif ($ioTypeStr -eq "Output" -and $di.Name -match "(?i)\bDQ\s*(\d+)") {
                                $phys = [int]$matches[1]
                                if ($phys -gt 0 -and $phys -le $lenBits) { $effBits = $phys }
                            }
                            $byteLen = if ($lenBits -gt 0) { [int][Math]::Ceiling($lenBits / 8.0) } else { 0 }
                            $endA = if ($byteLen -gt 0) { $startA + $byteLen - 1 } else { $startA - 1 }
                            $sig = "$($di.Name)|$($di.PositionNumber)|$ioTypeStr|$startA|$lenBits"
                            if (-not $seenKeys.Contains($sig)) {
                                [void]$seenKeys.Add($sig)
                                $ioRef.Value += "$($di.Name): $ioTypeStr byte $startA..$endA ($effBits/$lenBits bits)"
                                $subRanges += [PSCustomObject]@{
                                    IoType = $ioTypeStr
                                    StartAddress = $startA
                                    LengthBits = $lenBits
                                    EffectiveBits = $effBits
                                    Length = $byteLen
                                    EndAddress = $endA
                                }
                            }
                        }
                    } catch {}
                    if ($subRanges.Count -gt 0 -or $di.Classification -match "CPU|SM|SB|Module") {
                        $modKey = "MOD|$($di.Name)|$($di.PositionNumber)|$($di.TypeIdentifier)"
                        if (-not $seenKeys.Contains($modKey) -or $subRanges.Count -gt 0) {
                            [void]$seenKeys.Add($modKey)
                            $modRef.Value += [PSCustomObject]@{
                                Name = $di.Name
                                PositionNumber = $di.PositionNumber
                                TypeIdentifier = $di.TypeIdentifier
                                Classification = "$($di.Classification)"
                                IoRanges = $subRanges
                            }
                        }
                    }
                    foreach ($childDi in $di.DeviceItems) {
                        Collect-HwModulesRecursive $childDi $seenKeys $ioRef $modRef
                    }
                }

                $allDevs = Get-AllProjectDevices $proj
                $plcTarget = Select-PlcSoftware $proj $PlcName
                $targetDev = $plcTarget.Device
                $cpuItem = $plcTarget.CpuItem
                $plcSoftwareOk = ($null -ne $plcTarget.Software)
                $cpuModel = "$($cpuItem.Name) ($($cpuItem.TypeIdentifier))"
                try {
                    $attrNames = $cpuItem.GetAttributeInfos() | ForEach-Object { $_.Name }
                    if ($attrNames -contains "SystemMemoryByte") {
                        $sysMemInfo.SystemMemoryByte = $cpuItem.GetAttribute("SystemMemoryByte")
                    }
                    if ($attrNames -contains "SystemMemoryBit") {
                        $sysMemInfo.SystemMemoryEnabled = [bool]$cpuItem.GetAttribute("SystemMemoryBit")
                    }
                    if ($attrNames -contains "ClockMemoryByte") {
                        $sysMemInfo.ClockMemoryByte = $cpuItem.GetAttribute("ClockMemoryByte")
                    }
                    if ($attrNames -contains "ClockMemoryBit") {
                        $sysMemInfo.ClockMemoryEnabled = [bool]$cpuItem.GetAttribute("ClockMemoryBit")
                    }
                } catch {}

                $seenHwKeys = New-Object System.Collections.Generic.HashSet[string]
                # 采集目标 PLC 机架及项目内非其他 CPU 的分布式 IO 模块地址范围
                foreach ($item in $targetDev.DeviceItems) {
                    Collect-HwModulesRecursive $item $seenHwKeys ([ref]$ioList) ([ref]$hardwareModules)
                }
                foreach ($dev in $allDevs) {
                    if ($dev -eq $targetDev) { continue }
                    $otherPlc = Get-PlcTargetFromDeviceItem $dev $dev
                    if (-not $otherPlc) {
                        foreach ($item in $dev.DeviceItems) {
                            Collect-HwModulesRecursive $item $seenHwKeys ([ref]$ioList) ([ref]$hardwareModules)
                        }
                    }
                }
                
                $projInfo = [PSCustomObject]@{
                    Name = $proj.Name
                    Path = $proj.Path.FullName
                    DeviceCount = $allDevs.Count
                    DeviceName = $plcTarget.DeviceName
                    PlcName = $plcTarget.PlcName
                    CpuModel = $cpuModel
                    HasPlcSoftware = $plcSoftwareOk
                    Languages = $languages
                    IoRanges = $ioList
                    HardwareModules = $hardwareModules
                    SystemClockMemory = $sysMemInfo
                }
            }
        } catch [Siemens.Engineering.EngineeringSecurityException] {
            $projInfo = [PSCustomObject]@{
                ErrorType = "EngineeringSecurityException"
                Error = "Openness 防火墙拒绝访问：请在博途弹出的安全确认窗口中点击【是/全部允许】，并确认当前 Windows 用户已加入 Siemens TIA Openness 组且已重新登录。"
            }
        } catch [Siemens.Engineering.LicenseNotFoundException] {
            $projInfo = [PSCustomObject]@{
                ErrorType = "LicenseNotFoundException"
                Error = "缺少 STEP 7 许可证：请打开 Automation License Manager 检查 STEP 7 授权状态。"
            }
        } catch {
            $projInfo = [PSCustomObject]@{ Error = $_.Exception.Message }
        }
    }

    if ($Action -eq "inspect-env") {
        $ret = [PSCustomObject]@{
            DllPath = $dllPath
            DllOk = $dllOk
            JunctionOk = $junctionOk
            DllVersion = $dllVer
            UserInGroup = $inGroup
            HasProcess = $hasProc
            ProcessCount = $procs.Count
            Project = $projInfo
        }
        $ret | ConvertTo-Json -Depth 6
        exit 0
    }

    # 动作: 对账比对 EPLAN 图纸与博途实际硬件组态
    if ($Action -eq "compare-io") {
        $comparisonResults = @()
        $addrList = $ExpectedAddresses.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
        
        foreach ($addr in $addrList) {
            $matched = $false
            $targetModule = ""
            $targetSlot = ""
            $ioType = if ($addr -match "^%I") { "Input" } elseif ($addr -match "^%Q") { "Output" } else { "Other" }
            $byteIdx = -1
            $startBit = -1
            $endBit = -1
            if ($addr -match "^%[IQ]([BWD])(\d+)$") {
                $byteIdx = [int]$matches[2]
                $bitLen = switch ($matches[1]) { "B" { 8 } "W" { 16 } "D" { 32 } default { 8 } }
                $startBit = $byteIdx * 8
                $endBit = $startBit + $bitLen - 1
            } elseif ($addr -match "^%[IQ](\d+)\.([0-7])$") {
                $byteIdx = [int]$matches[1]
                $bitOff = [int]$matches[2]
                $startBit = $byteIdx * 8 + $bitOff
                $endBit = $startBit
            } elseif ($addr -match "^%[IQ](\d+)$") {
                $byteIdx = [int]$matches[1]
                $startBit = $byteIdx * 8
                $endBit = $startBit + 7
            }

            foreach ($m in $hardwareModules) {
                foreach ($r in $m.IoRanges) {
                    $rEffBits = if ($null -ne $r.EffectiveBits -and [int]$r.EffectiveBits -gt 0) {
                        [int]$r.EffectiveBits
                    } elseif ($null -ne $r.LengthBits -and [int]$r.LengthBits -gt 0) {
                        [int]$r.LengthBits
                    } else {
                        ([int]$r.EndAddress - [int]$r.StartAddress + 1) * 8
                    }
                    $rStartBit = [int]$r.StartAddress * 8
                    $rEndBit = $rStartBit + $rEffBits - 1
                    if ($r.IoType -eq $ioType -and $startBit -ge 0 -and $rStartBit -le $startBit -and $endBit -le $rEndBit) {
                        $matched = $true
                        $targetModule = $m.Name
                        $targetSlot = $m.PositionNumber
                        break
                    }
                }
                if ($matched) { break }
            }

            $comparisonResults += [PSCustomObject]@{
                Address = $addr
                IoType = $ioType
                ByteIndex = $byteIdx
                Status = if ($matched) { "MATCHED" } else { "OUT_OF_RANGE" }
                ModuleName = $targetModule
                SlotNumber = $targetSlot
            }
        }

        $ret = [PSCustomObject]@{
            Project = $projInfo.Name
            TotalChecked = $addrList.Count
            MatchedCount = ($comparisonResults | Where-Object { $_.Status -eq "MATCHED" }).Count
            MismatchCount = ($comparisonResults | Where-Object { $_.Status -ne "MATCHED" }).Count
            Details = $comparisonResults
        }
        $ret | ConvertTo-Json -Depth 6
        exit 0
    }
}

# 动作 3: 全局库 MasterCopy 归档 (模块 4)
if ($Action -eq "archive-to-library") {
    $procs = [Siemens.Engineering.TiaPortal]::GetProcesses()
    $targetProc = Select-TiaProcess $procs $ProcessIndex $ProjectPath
    $tia = $targetProc.Attach()
    if ($tia.Projects.Count -eq 0) {
        Write-Host (ConvertTo-Json @{ Success = $false; Error = "当前博途进程未打开任何项目" })
        exit 1
    }
    $proj = $tia.Projects[0]
    $plcTarget = Select-PlcSoftware $proj $PlcName
    $sw = $plcTarget.Software

    # 检查或打开/创建 UserGlobalLibrary
    $lib = $null
    foreach ($l in $tia.GlobalLibraries) {
        if ($l.Path -and $l.Path.FullName -like "*$LibraryName*") {
            $lib = $l
            break
        }
    }
    if (-not $lib) {
        $destDir = New-Object System.IO.DirectoryInfo($LibraryPath)
        if (-not $destDir.Exists) { $destDir.Create() }
        try {
            $createMethods = $tia.GlobalLibraries.GetType().GetMethods() | Where-Object { $_.Name -eq "Create" -and $_.IsGenericMethod }
            $lib = $createMethods[0].MakeGenericMethod(@([Siemens.Engineering.Library.UserGlobalLibrary])).Invoke($tia.GlobalLibraries, @($destDir, $LibraryName))
        } catch {
            Write-Host (ConvertTo-Json @{ Success = $false; Error = "创建全局库失败: $($_.Exception.Message)" })
            exit 1
        }
    }

    $archivedItems = @()
    $blockNamesList = $BlockNames.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    foreach ($bName in $blockNamesList) {
        $targetBlock = Find-BlockRecursive $sw.BlockGroup $bName
        if ($targetBlock) {
            try {
                $mc = $lib.MasterCopyFolder.MasterCopies.Create($targetBlock)
                $archivedItems += [PSCustomObject]@{ Name = $bName; Status = "OK"; MasterCopy = $mc.Name }
            } catch {
                $archivedItems += [PSCustomObject]@{ Name = $bName; Status = "FAIL"; Error = $_.Exception.Message }
            }
        } else {
            $archivedItems += [PSCustomObject]@{ Name = $bName; Status = "FAIL"; Error = "未在 PLC 中找到同名块" }
        }
    }

    # 保存并安全关闭库，释放文件锁（严禁释放博途实例）
    try {
        $lib.Save()
        $lib.Close()
    } catch {}

    $ret = [PSCustomObject]@{
        Success = $true
        LibraryName = $LibraryName
        LibraryPath = $LibraryPath
        ArchivedItems = $archivedItems
    }
    $ret | ConvertTo-Json -Depth 5
    exit 0
}

# 动作 4: 批量导出博途当前已有块与变量表 (用于反向生成三表与 Diff 比对)
if ($Action -eq "export-all") {
    if (-not $ExportDir) {
        Write-Host (ConvertTo-Json @{ Success = $false; Error = "必须指定 ExportDir 导出目录" })
        exit 1
    }
    if (-not (Test-Path $ExportDir)) {
        New-Item -ItemType Directory -Path $ExportDir -Force | Out-Null
    }
    $procs = [Siemens.Engineering.TiaPortal]::GetProcesses()
    $targetProc = Select-TiaProcess $procs $ProcessIndex $ProjectPath
    $tia = $targetProc.Attach()
    if ($tia.Projects.Count -eq 0) {
        Write-Host (ConvertTo-Json @{ Success = $false; Error = "当前博途进程未打开任何项目" })
        exit 1
    }
    $proj = $tia.Projects[0]
    $plcTarget = Select-PlcSoftware $proj $PlcName
    $sw = $plcTarget.Software

    $exported = @()
    $expOpt = [Siemens.Engineering.ExportOptions]::WithDefaults

    # 导出用户自定义数据类型 (UDT / PlcStruct)
    function Export-TypesRecursive($tGrp) {
        $res = @()
        if (-not $tGrp) { return $res }
        foreach ($tp in $tGrp.Types) {
            $outP = Join-Path $ExportDir "UDT_$($tp.Name).xml"
            try {
                $tp.Export((New-Object System.IO.FileInfo($outP)), $expOpt)
                $res += [PSCustomObject]@{ Name = $tp.Name; Type = "PlcStruct"; Path = $outP }
            } catch {}
        }
        foreach ($sub in $tGrp.Groups) {
            $res += Export-TypesRecursive $sub
        }
        return $res
    }

    if ($sw.TypeGroup) {
        $exported += Export-TypesRecursive $sw.TypeGroup
    }

    # 导出变量表
    foreach ($tt in $sw.TagTableGroup.TagTables) {
        $outP = Join-Path $ExportDir "TagTable_$($tt.Name).xml"
        $tt.Export((New-Object System.IO.FileInfo($outP)), $expOpt)
        $exported += [PSCustomObject]@{ Name = $tt.Name; Type = "TagTable"; Path = $outP }
    }

    # 递归导出所有块
    function Export-BlocksRecursive($grp) {
        $res = @()
        foreach ($b in $grp.Blocks) {
            $outP = Join-Path $ExportDir "$($b.Name).xml"
            try {
                $b.Export((New-Object System.IO.FileInfo($outP)), $expOpt)
                $res += [PSCustomObject]@{ Name = $b.Name; Type = "$($b.GetType().Name)"; Path = $outP }
            } catch {}
        }
        foreach ($sub in $grp.Groups) {
            $res += Export-BlocksRecursive $sub
        }
        return $res
    }

    $blockResults = Export-BlocksRecursive $sw.BlockGroup
    $exported += $blockResults

    # 收集系统资源块 (SystemBlockGroups) 与工艺对象 (TechnologicalObjectGroup) 符号列表，供静态校验器识别
    $systemSymbols = New-Object System.Collections.Generic.List[string]
    function Collect-BlockNamesRecursive($grp, $list) {
        if (-not $grp) { return }
        foreach ($b in $grp.Blocks) {
            if ($b.Name -and -not $list.Contains($b.Name)) { [void]$list.Add($b.Name) }
        }
        foreach ($sub in $grp.Groups) {
            Collect-BlockNamesRecursive $sub $list
        }
    }
    try {
        foreach ($sbg in $sw.BlockGroup.SystemBlockGroups) {
            Collect-BlockNamesRecursive $sbg $systemSymbols
        }
    } catch {}
    function Collect-TechObjRecursive($toGrp, $list) {
        if (-not $toGrp) { return }
        try {
            foreach ($to in $toGrp.TechnologicalObjects) {
                if ($to.Name -and -not $list.Contains($to.Name)) { [void]$list.Add($to.Name) }
            }
            foreach ($sub in $toGrp.Groups) {
                Collect-TechObjRecursive $sub $list
            }
        } catch {}
    }
    try {
        if ($sw.TechnologicalObjectGroup) {
            Collect-TechObjRecursive $sw.TechnologicalObjectGroup $systemSymbols
        }
    } catch {}

    $sysSymPath = Join-Path $ExportDir "tia_system_symbols.json"
    [PSCustomObject]@{ SystemSymbols = @($systemSymbols) } | ConvertTo-Json -Depth 3 | Out-File -FilePath $sysSymPath -Encoding utf8

    $ret = [PSCustomObject]@{
        Success = $true
        ProjectName = $proj.Name
        CpuModel = "$($plcTarget.PlcName) ($($plcTarget.TypeIdentifier))"
        ExportDir = $ExportDir
        ExportedCount = $exported.Count
        SystemSymbolCount = $systemSymbols.Count
        Items = $exported
    }
    $ret | ConvertTo-Json -Depth 5
    exit 0
}

function Get-OpennessService($targetObj, [type]$serviceType) {
    if (-not $targetObj) { return $null }
    $methods = $targetObj.GetType().GetMethods() | Where-Object { $_.Name -eq "GetService" -and $_.IsGenericMethod }
    if ($methods) {
        try {
            return $methods[0].MakeGenericMethod(@($serviceType)).Invoke($targetObj, $null)
        } catch {}
    }
    return $null
}

function Extract-CompileMessagesRecursive($msgNode, [string]$parentPath = "") {
    $list = @()
    if (-not $msgNode) { return $list }
    $curPath = if ($msgNode.Path) { "$($msgNode.Path)" } else { $parentPath }
    if (-not [string]::IsNullOrWhiteSpace($msgNode.Description)) {
        $list += [PSCustomObject]@{
            Severity    = "$($msgNode.Severity)"
            Description = "$($msgNode.Description)"
            Path        = $curPath
            DateTime    = "$($msgNode.DateTime)"
        }
    }
    if ($msgNode.Messages) {
        foreach ($sub in $msgNode.Messages) {
            $list += Extract-CompileMessagesRecursive $sub $curPath
        }
    }
    return $list
}

function ConvertTo-ActionableCompileErrors($messages) {
    $out = @()
    foreach ($m in $messages) {
        if ($m.Severity -ne "Error") { continue }
        $desc = "$($m.Description)"
        $hint = "请检查报错路径 '$($m.Path)' 对应的 LAD XML 符号、接口或指令语法，修复后重跑 validate_lad_xml.py 与 -DiffOnly。"
        if ($desc -match "not defined|未定义|unknown|不存在") {
            $hint = "检测到未定义符号/块，请检查 TagTable_IO/TagTable_M 变量声明、全局定时器 DB 成员名或被调用 FB/FC 是否已包含在导入目录中。"
        } elseif ($desc -match "data type|数据类型|mismatch|不匹配") {
            $hint = "检测到数据类型不匹配，请核对 %I/%Q/%IW 数据类型（Bool vs Int/Time）或形参 Section 类型是否一致。"
        } elseif ($desc -match "time stamp|时间戳|interface|接口") {
            $hint = "检测到块接口或时间戳冲突，请先导入 UDT/被调 FB，再重新生成调用方与 InstanceDB。"
        }
        $out += [PSCustomObject]@{
            Path          = $m.Path
            Description   = $desc
            FixSuggestion = $hint
        }
    }
    return $out
}

# 动作 5: CAx (AutomationML .aml) 硬件导入与结构化 TransferResult 抽取
if ($Action -eq "import-cax") {
    if ([string]::IsNullOrWhiteSpace($AmlPath) -or -not (Test-Path $AmlPath)) {
        Write-Host ([PSCustomObject]@{ Success = $false; Error = "AmlPath 参数缺失或文件不存在: '$AmlPath'" } | ConvertTo-Json -Depth 4)
        exit 1
    }
    $procs = [Siemens.Engineering.TiaPortal]::GetProcesses()
    $targetProc = Select-TiaProcess $procs $ProcessIndex $ProjectPath
    $tia = $targetProc.Attach()
    if ($tia.Projects.Count -eq 0) {
        Write-Host ([PSCustomObject]@{ Success = $false; Error = "当前博途进程未打开任何项目" } | ConvertTo-Json -Depth 4)
        exit 1
    }
    $proj = $tia.Projects[0]
    $caxProvider = Get-OpennessService $proj ([Siemens.Engineering.Cax.CaxProvider])
    if (-not $caxProvider) {
        Write-Host ([PSCustomObject]@{ Success = $false; Error = "当前 TIA Portal 项目不支持 CaxProvider 服务" } | ConvertTo-Json -Depth 4)
        exit 1
    }

    $logFile = if ($CaxLogPath) { $CaxLogPath } else { Join-Path ([System.IO.Path]::GetTempPath()) "cax_import_$(Get-Date -Format 'yyyyMMdd_HHmmss').log" }
    $logDir = Split-Path $logFile -Parent
    if ($logDir -and -not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

    $optEnum = [Siemens.Engineering.Cax.CaxImportOptions]::RetainTiaDevice
    try {
        $optEnum = [System.Enum]::Parse([Siemens.Engineering.Cax.CaxImportOptions], $ImportOption, $true)
    } catch {}

    $amlFi = New-Object System.IO.FileInfo($AmlPath)
    $logFi = New-Object System.IO.FileInfo($logFile)
    $warnings = @()
    $errors = @()
    $importedState = "Unknown"
    $importOk = $false

    try {
        $rawRes = $caxProvider.Import($amlFi, $logFi, $optEnum)
        if ($rawRes -is [bool]) {
            $importOk = [bool]$rawRes
            $importedState = if ($importOk) { "Succeeded" } else { "Failed" }
        } elseif ($null -ne $rawRes -and $null -ne $rawRes.PSObject.Properties["State"]) {
            $importedState = "$($rawRes.State)"
            $importOk = ($importedState -ne "Error" -and $importedState -ne "Failed")
            if ($rawRes.Messages) {
                foreach ($msg in $rawRes.Messages) {
                    $txt = "$($msg.Description)"
                    if ("$($msg.Severity)" -eq "Error") { $errors += $txt }
                    elseif ("$($msg.Severity)" -eq "Warning") { $warnings += $txt }
                }
            }
        } else {
            $importOk = $true
            $importedState = "Completed"
        }
    } catch {
        $errors += $_.Exception.Message
        $importedState = "Exception"
    }

    if (Test-Path $logFile) {
        foreach ($line in (Get-Content $logFile -Encoding UTF8 -ErrorAction SilentlyContinue)) {
            if ($line -match "(?i)\berror\b|失败|错误") { $errors += $line.Trim() }
            elseif ($line -match "(?i)\bwarn|告警|警告") { $warnings += $line.Trim() }
        }
    }

    $hint = if ($importOk) {
        "CAx 硬件组态导入完成，请继续执行 -Action inspect-env 核验槽位与 I/O 地址区间。"
    } else {
        "CAx 导入失败：请检查 AML 中是否残留驱动器/非按键HMI/受限Scalance交换机，或查看日志 '$logFile'。"
    }

    [PSCustomObject]@{
        Success        = ($importOk -and $errors.Count -eq 0)
        State          = $importedState
        ProjectName    = $proj.Name
        AmlPath        = $AmlPath
        LogPath        = $logFile
        ImportOption   = "$optEnum"
        Warnings       = $warnings
        Errors         = $errors
        ActionableHint = $hint
    } | ConvertTo-Json -Depth 5
    exit 0
}

# 动作 6: 批量导入 PlcWatchTable / PlcForceTable 并可选绑定至 PLC Web Server
if ($Action -eq "import-watch-tables") {
    if ([string]::IsNullOrWhiteSpace($WatchXmlDir) -or -not (Test-Path $WatchXmlDir)) {
        Write-Host ([PSCustomObject]@{ Success = $false; Error = "WatchXmlDir 目录不存在: '$WatchXmlDir'" } | ConvertTo-Json -Depth 4)
        exit 1
    }
    $procs = [Siemens.Engineering.TiaPortal]::GetProcesses()
    $targetProc = Select-TiaProcess $procs $ProcessIndex $ProjectPath
    $tia = $targetProc.Attach()
    if ($tia.Projects.Count -eq 0) {
        Write-Host ([PSCustomObject]@{ Success = $false; Error = "当前博途进程未打开任何项目" } | ConvertTo-Json -Depth 4)
        exit 1
    }
    $proj = $tia.Projects[0]
    $plcTarget = Select-PlcSoftware $proj $PlcName
    $sw = $plcTarget.Software
    $wfGroup = $sw.WatchAndForceTableGroup
    $accessMgr = $null
    if ($BindWebServer) {
        $accessMgr = Get-OpennessService $plcTarget.CpuItem ([Siemens.Engineering.HW.Features.WatchAndForceTableAccessManager])
    }

    $watchLogs = @()
    $forceLogs = @()
    $boundTables = @()
    $importOpt = [Siemens.Engineering.ImportOptions]::Override

    foreach ($xf in (Get-ChildItem -Path $WatchXmlDir -Filter "*.xml" | Sort-Object Name)) {
        try {
            [xml]$xDoc = Get-Content $xf.FullName -Encoding UTF8 -Raw
            $wNode = $xDoc.SelectSingleNode("//*[local-name()='SW.WatchAndForceTables.PlcWatchTable']")
            $fNode = $xDoc.SelectSingleNode("//*[local-name()='SW.WatchAndForceTables.PlcForceTable']")
            $fi = New-Object System.IO.FileInfo($xf.FullName)
            if ($wNode) {
                $tNameNode = $wNode.SelectSingleNode("./*[local-name()='AttributeList']/*[local-name()='Name']")
                $tName = if ($tNameNode) { $tNameNode.InnerText.Trim() } else { [System.IO.Path]::GetFileNameWithoutExtension($xf.Name) }
                $null = $wfGroup.WatchTables.Import($fi, $importOpt)
                $wtObj = $wfGroup.WatchTables.Find($tName)
                if ($wtObj -and $ShowInEditor) { try { $wtObj.ShowInEditor() } catch {} }
                if ($wtObj -and $BindWebServer -and $accessMgr) {
                    try {
                        $existingRule = $accessMgr.WatchtableAccessRules.Find($wtObj)
                        if (-not $existingRule) {
                            $null = $accessMgr.WatchtableAccessRules.Create($wtObj, [Siemens.Engineering.HW.WatchAndForceTableAccess]::Read)
                        }
                        $boundTables += $tName
                    } catch {}
                }
                $watchLogs += [PSCustomObject]@{ Name = $tName; File = $xf.Name; Status = "OK" }
            } elseif ($fNode) {
                $tNameNode = $fNode.SelectSingleNode("./*[local-name()='AttributeList']/*[local-name()='Name']")
                $tName = if ($tNameNode) { $tNameNode.InnerText.Trim() } else { [System.IO.Path]::GetFileNameWithoutExtension($xf.Name) }
                $null = $wfGroup.ForceTables.Import($fi, $importOpt)
                $ftObj = $wfGroup.ForceTables.Find($tName)
                if ($ftObj -and $ShowInEditor) { try { $ftObj.ShowInEditor() } catch {} }
                $forceLogs += [PSCustomObject]@{ Name = $tName; File = $xf.Name; Status = "OK" }
            }
        } catch {
            $watchLogs += [PSCustomObject]@{ File = $xf.Name; Status = "FAIL"; Error = $_.Exception.Message }
        }
    }

    [PSCustomObject]@{
        Success        = (@($watchLogs + $forceLogs | Where-Object { $_.Status -eq "FAIL" }).Count -eq 0)
        ProjectName    = $proj.Name
        PlcName        = $plcTarget.PlcName
        WatchTables    = $watchLogs
        ForceTables    = $forceLogs
        WebServerBound = $boundTables
    } | ConvertTo-Json -Depth 5
    exit 0
}

# 动作 7: 独立执行 PLC 软件编译并返回可行动诊断结果
if ($Action -eq "compile-plc") {
    $procs = [Siemens.Engineering.TiaPortal]::GetProcesses()
    $targetProc = Select-TiaProcess $procs $ProcessIndex $ProjectPath
    $tia = $targetProc.Attach()
    if ($tia.Projects.Count -eq 0) {
        Write-Host ([PSCustomObject]@{ Success = $false; Error = "当前博途进程未打开任何项目" } | ConvertTo-Json -Depth 4)
        exit 1
    }
    $proj = $tia.Projects[0]
    $plcTarget = Select-PlcSoftware $proj $PlcName
    $sw = $plcTarget.Software
    $compService = Get-OpennessService $sw ([Siemens.Engineering.Compiler.ICompilable])
    $compRes = $compService.Compile()
    $msgs = @()
    foreach ($m in $compRes.Messages) {
        $msgs += Extract-CompileMessagesRecursive $m
    }
    $actionable = @(ConvertTo-ActionableCompileErrors $msgs)
    [PSCustomObject]@{
        Success                 = ([int]$compRes.ErrorCount -eq 0 -and "$($compRes.State)" -ne "Error")
        ProjectName             = $proj.Name
        PlcName                 = $plcTarget.PlcName
        CompileState            = "$($compRes.State)"
        ErrorCount              = [int]$compRes.ErrorCount
        WarningCount            = [int]$compRes.WarningCount
        Messages                = $msgs
        ActionableCompileErrors = $actionable
    } | ConvertTo-Json -Depth 6
    exit 0
}

function Test-IsFailSafePlc($plcTarget) {
    if (-not $plcTarget) { return $false }
    $idText = "$($plcTarget.TypeIdentifier) $($plcTarget.PlcName) $($plcTarget.DeviceName)"
    if ($idText -match "(?i)(1[25]1\dFC?|FailSafe|Fail-Safe|F-CPU|PROFIsafe|6ES7[25]1\d-\dF)") {
        return $true
    }
    try {
        foreach ($b in $plcTarget.Software.BlockGroup.Blocks) {
            try {
                if ([bool]$b.GetAttribute("IsFailsafe")) { return $true }
            } catch {}
        }
    } catch {}
    return $false
}

function Get-CompareOnlineToken([string]$projName, [string]$plcName, [string]$cpuModel, [string]$compareState) {
    $raw = "ONLINE_GATE|$projName|$plcName|$cpuModel|$compareState"
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($raw))
        return ([System.BitConverter]::ToString($bytes) -replace "-", "").ToLowerInvariant().Substring(0, 16)
    } finally {
        $sha.Dispose()
    }
}

# 动作 8: 在线/离线程序比对与下载确认令牌签发
if ($Action -eq "compare-online") {
    $procs = [Siemens.Engineering.TiaPortal]::GetProcesses()
    $targetProc = Select-TiaProcess $procs $ProcessIndex $ProjectPath
    $tia = $targetProc.Attach()
    if ($tia.Projects.Count -eq 0) {
        Write-Host ([PSCustomObject]@{ Success = $false; Error = "当前博途进程未打开任何项目" } | ConvertTo-Json -Depth 4)
        exit 1
    }
    $proj = $tia.Projects[0]
    $plcTarget = Select-PlcSoftware $proj $PlcName
    $sw = $plcTarget.Software
    $cpuModel = "$($plcTarget.PlcName) ($($plcTarget.TypeIdentifier))"
    $isFailSafe = Test-IsFailSafePlc $plcTarget

    $cmpState = "Unknown"
    $diffItems = @()
    $compareError = ""
    try {
        $cmpRes = $sw.CompareToOnline()
        if ($cmpRes -and $cmpRes.RootElement) {
            $cmpState = "$($cmpRes.RootElement.ComparisonResult)"
            foreach ($ch in $cmpRes.RootElement.Elements) {
                $diffItems += [PSCustomObject]@{
                    Name   = "$($ch.LeftName)"
                    Result = "$($ch.ComparisonResult)"
                }
            }
        } else {
            $cmpState = "Compared"
        }
    } catch {
        $cmpState = "OfflineOrUnreachable"
        $compareError = $_.Exception.Message
    }

    $token = Get-CompareOnlineToken $proj.Name $plcTarget.PlcName $cpuModel $cmpState
    $hint = if ($isFailSafe) {
        "检测到故障安全 F-PLC：首次包含安全程序的完整下载必须在博途 GUI 手动执行；仅常规程序修改可通过 ConfirmOnlineToken 与 HumanApproved 授权下装。"
    } else {
        "请向现场工程师展示比对摘要与安全检查表，获取显式人工授权后携带 ConfirmOnlineToken 与 -HumanApproved 执行受控下载。"
    }

    [PSCustomObject]@{
        Success                     = $true
        ProjectName                 = $proj.Name
        PlcName                     = $plcTarget.PlcName
        CpuModel                    = $cpuModel
        IsFailSafePlc               = $isFailSafe
        FirstManualDownloadRequired = $isFailSafe
        CompareState                = $cmpState
        DiffSummary                 = $diffItems
        CompareError                = $compareError
        ConfirmOnlineToken          = $token
        ActionableHint              = $hint
    } | ConvertTo-Json -Depth 5
    exit 0
}

# 动作 9: 受控向 PLC 下载程序 (三重安全门禁硬拦截)
if ($Action -eq "download-plc") {
    if (-not $HumanApproved) {
        Write-Host ([PSCustomObject]@{
            Success        = $false
            GateBlocked    = $true
            GateReason     = "HUMAN_APPROVAL_REQUIRED"
            ActionableHint = "高危操作拦截：严禁静默向物理 PLC 执行下载，必须由工程师确认安全区域清场并显式传入 -HumanApproved。"
        } | ConvertTo-Json -Depth 4)
        exit 1
    }
    if ([string]::IsNullOrWhiteSpace($ConfirmOnlineToken) -or $ConfirmOnlineToken.Trim().Length -lt 8) {
        Write-Host ([PSCustomObject]@{
            Success        = $false
            GateBlocked    = $true
            GateReason     = "MISSING_COMPARE_ONLINE_TOKEN"
            ActionableHint = "高危操作拦截：必须先执行 -Action compare-online 获取有效 ConfirmOnlineToken 并在人工比对后传入。"
        } | ConvertTo-Json -Depth 4)
        exit 1
    }

    $procs = [Siemens.Engineering.TiaPortal]::GetProcesses()
    $targetProc = Select-TiaProcess $procs $ProcessIndex $ProjectPath
    $tia = $targetProc.Attach()
    if ($tia.Projects.Count -eq 0) {
        Write-Host ([PSCustomObject]@{ Success = $false; Error = "当前博途进程未打开任何项目" } | ConvertTo-Json -Depth 4)
        exit 1
    }
    $proj = $tia.Projects[0]
    $plcTarget = Select-PlcSoftware $proj $PlcName
    $isFailSafe = Test-IsFailSafePlc $plcTarget
    if ($isFailSafe -and (-not $FirstManualDownloadDone)) {
        Write-Host ([PSCustomObject]@{
            Success        = $false
            GateBlocked    = $true
            GateReason     = "FAILSAFE_FIRST_MANUAL_DOWNLOAD_REQUIRED"
            IsFailSafePlc  = $true
            ActionableHint = "F-PLC 硬性边界拦截：包含故障安全程序的控制器必须先在博途 GUI 手动完成首次完整下载，之后才开放 Openness 常规程序自动下载。"
        } | ConvertTo-Json -Depth 4)
        exit 1
    }

    $dlProvider = Get-OpennessService $plcTarget.CpuItem ([Siemens.Engineering.Download.DownloadProvider])
    if (-not $dlProvider) {
        $dlProvider = Get-OpennessService $plcTarget.Device ([Siemens.Engineering.Download.DownloadProvider])
    }
    if (-not $dlProvider) {
        Write-Host ([PSCustomObject]@{ Success = $false; Error = "目标 CPU 未提供 DownloadProvider 服务" } | ConvertTo-Json -Depth 4)
        exit 1
    }

    $dlOption = [Siemens.Engineering.Download.DownloadOptions]::Software
    try {
        if ($DownloadMode -eq "Hardware") {
            $dlOption = [Siemens.Engineering.Download.DownloadOptions]::Hardware
        } elseif ($DownloadMode -match "Software") {
            if ([System.Enum]::IsDefined([Siemens.Engineering.Download.DownloadOptions], $DownloadMode)) {
                $dlOption = [System.Enum]::Parse([Siemens.Engineering.Download.DownloadOptions], $DownloadMode, $true)
            } else {
                $dlOption = [Siemens.Engineering.Download.DownloadOptions]::Software
            }
        }
    } catch {}

    $dlState = "Unknown"
    $dlErrCount = 0
    $dlWarnCount = 0
    $dlMsgs = @()
    $dlOk = $false
    try {
        $cfg = $dlProvider.Configuration
        $preDel = [Siemens.Engineering.Download.DownloadConfigurationDelegate]{ param($dlCfg) }
        $postDel = [Siemens.Engineering.Download.DownloadConfigurationDelegate]{ param($dlCfg) }
        $dlRes = $dlProvider.Download($cfg, $preDel, $postDel, $dlOption)
        $dlState = "$($dlRes.State)"
        $dlErrCount = [int]$dlRes.ErrorCount
        $dlWarnCount = [int]$dlRes.WarningCount
        foreach ($m in $dlRes.Messages) {
            $dlMsgs += [PSCustomObject]@{
                Severity    = "$($m.Severity)"
                Description = "$($m.Description)"
            }
        }
        $dlOk = ($dlErrCount -eq 0 -and $dlState -ne "Error")
    } catch {
        $dlState = "Exception"
        $dlErrCount = 1
        $dlMsgs += [PSCustomObject]@{ Severity = "Error"; Description = $_.Exception.Message }
    }

    [PSCustomObject]@{
        Success            = $dlOk
        ProjectName        = $proj.Name
        PlcName            = $plcTarget.PlcName
        DownloadMode       = "$dlOption"
        ConfirmTokenMasked = "***REDACTED***"
        State              = $dlState
        ErrorCount         = $dlErrCount
        WarningCount       = $dlWarnCount
        Messages           = $dlMsgs
    } | ConvertTo-Json -Depth 5
    exit 0
}

# 动作 10: 根据 TO_Params.xlsx 自动创建/配置博途工艺对象 (TechnologicalObjectGroup)
if ($Action -eq "configure-to") {
    if ([string]::IsNullOrWhiteSpace($ToParamsPath) -or -not (Test-Path $ToParamsPath)) {
        Write-Host ([PSCustomObject]@{
            Success        = $false
            Errors         = @("ToParamsPath 参数缺失或文件不存在: '$ToParamsPath'")
            ActionableHint = "请先运行 cax_hardware_builder.build_to_params_template() 生成并填写 TO_Params.xlsx。"
        } | ConvertTo-Json -Depth 4)
        exit 1
    }

    $caxPy = Join-Path $PSScriptRoot "cax_hardware_builder.py"
    $prevEnc = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = "utf-8"
    try {
        $pyCode = "import json, sys; sys.path.insert(0, r'$PSScriptRoot'); from cax_hardware_builder import parse_to_params_xlsx; print(json.dumps(parse_to_params_xlsx(sys.argv[1]), ensure_ascii=False))"
        $rawAxesJson = & python -c $pyCode $ToParamsPath 2>&1
        $pyExit = $LASTEXITCODE
    } finally {
        $env:PYTHONIOENCODING = $prevEnc
    }
    if ($pyExit -ne 0) {
        Write-Host ([PSCustomObject]@{
            Success        = $false
            Errors         = @("解析 TO_Params.xlsx 失败: $rawAxesJson")
            ActionableHint = "请核对 TO_Params.xlsx 的机械螺距、最大速度、加减速是否为正数且列名完整。"
        } | ConvertTo-Json -Depth 4)
        exit 1
    }

    $axesList = ConvertFrom-Json -InputObject (($rawAxesJson | ForEach-Object { "$_" }) -join "`n").Trim()
    $procs = [Siemens.Engineering.TiaPortal]::GetProcesses()
    $targetProc = Select-TiaProcess $procs $ProcessIndex $ProjectPath
    $tia = $targetProc.Attach()
    if ($tia.Projects.Count -eq 0) {
        Write-Host ([PSCustomObject]@{ Success = $false; Errors = @("当前博途进程未打开任何项目") } | ConvertTo-Json -Depth 4)
        exit 1
    }
    $proj = $tia.Projects[0]
    $plcTarget = Select-PlcSoftware $proj $PlcName
    $sw = $plcTarget.Software
    $toGrp = $sw.TechnologicalObjectGroup
    if (-not $toGrp) {
        Write-Host ([PSCustomObject]@{
            Success        = $false
            Errors         = @("当前 PLC 软件容器未提供 TechnologicalObjectGroup 服务（部分低配固件或未启用运动控制时不可用）")
            ActionableHint = "请在博途项目树核对 CPU 型号及固件版本是否支持工艺对象。"
        } | ConvertTo-Json -Depth 4)
        exit 1
    }

    $configuredAxes = @()
    $toWarnings = @()
    $toErrors = @()

    foreach ($ax in @($axesList)) {
        $axisName = "$($ax.axis_name)"
        $toType = if ($ax.to_type) { "$($ax.to_type)" } else { "TO_PositioningAxis" }
        try {
            $toObj = $toGrp.TechnologicalObjects.Find($axisName)
            $actionMode = "UpdatedExisting"
            if (-not $toObj) {
                $actionMode = "CreatedNew"
                $created = $false
                foreach ($ver in @("4.0", "5.0", "6.0", "3.0")) {
                    try {
                        $toObj = $toGrp.TechnologicalObjects.Create($axisName, $toType, (New-Object System.Version($ver)))
                        if ($toObj) { $created = $true; break }
                    } catch {}
                }
                if (-not $created) {
                    $toObj = $toGrp.TechnologicalObjects.Create($axisName, $toType)
                }
            }

            if ($toObj -and $toObj.Parameters) {
                $paramPairs = @{
                    "Mechanics.LeadScrewPitch" = [double]$ax.lead_pitch_mm
                    "DynamicDefaults.Velocity" = [double]$ax.max_velocity_mms
                    "DynamicDefaults.Acceleration" = [double]$ax.acceleration_mms2
                    "DynamicDefaults.Deceleration" = [double]$ax.acceleration_mms2
                }
                foreach ($pKey in $paramPairs.Keys) {
                    try {
                        $pNode = $toObj.Parameters.Find($pKey)
                        if ($pNode) { $pNode.Value = $paramPairs[$pKey] }
                    } catch {
                        $toWarnings += "轴 '$axisName' 参数 '$pKey' 写入跳过: $($_.Exception.Message)"
                    }
                }
            }

            $configuredAxes += [PSCustomObject]@{
                AxisName   = $axisName
                ToType     = $toType
                ActionMode = $actionMode
                Status     = "OK"
            }
        } catch {
            $toErrors += "配置工艺对象 '$axisName' ($toType) 失败: $($_.Exception.Message)"
        }
    }

    [PSCustomObject]@{
        Success        = ($toErrors.Count -eq 0)
        ProjectName    = $proj.Name
        PlcName        = $plcTarget.PlcName
        ConfiguredAxes = $configuredAxes
        Warnings       = $toWarnings
        Errors         = $toErrors
    } | ConvertTo-Json -Depth 5
    exit 0
}

Write-Host (ConvertTo-Json @{ Status = "Ready"; OpennessDll = $dllPath })
exit 0
