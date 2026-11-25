# -*- coding: utf-8 -*-
# 模块名称: tia_common.psm1
# 功能说明: TIA Portal Openness PowerShell 公共辅助模块（DLL 动态发现、进程/PLC 定位、递归树查找、
#           编译诊断展平、安全文件名清理与在线下载一次性 Gate 票据签发/验签）

function Resolve-OpennessDllInfo {
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
                        if (Test-Path $candidate) {
                            return [PSCustomObject]@{
                                DllPath = $candidate
                                Version = $ver
                                Found   = $true
                            }
                        }
                    }
                }
            } catch {}
        }
    }
    foreach ($ver in $versions) {
        $defaultCandidate = "C:\Program Files\Siemens\Automation\Portal V$ver\PublicAPI\V$ver\Siemens.Engineering.dll"
        if (Test-Path $defaultCandidate) {
            return [PSCustomObject]@{
                DllPath = $defaultCandidate
                Version = $ver
                Found   = $true
            }
        }
    }
    $fallback = "C:\Program Files\Siemens\Automation\Portal V16\PublicAPI\V16\Siemens.Engineering.dll"
    return [PSCustomObject]@{
        DllPath = $fallback
        Version = "16"
        Found   = (Test-Path $fallback)
    }
}

function Resolve-OpennessDllPath {
    return (Resolve-OpennessDllInfo).DllPath
}

function Get-AllProjectDevices($proj) {
    $list = @()
    if (-not $proj) { return $list }
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

function Get-PlcTargetFromDeviceItem($dev, $deviceItem) {
    if (-not $deviceItem) { return $null }
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
        Write-Host ([PSCustomObject]@{ Success = $false; Error = "未找到运行中的博途进程" } | ConvertTo-Json -Depth 4)
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
        $errMsg = if ($matchedProcs.Count -eq 0) { "未找到打开目标项目 '$projPath' 的博途进程" } else { "存在多个匹配 '$projPath' 的博途进程，请改用 -ProcessIndex 显式指定" }
        Write-Host ([PSCustomObject]@{
            Success    = $false
            Error      = $errMsg
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
        Write-Host ([PSCustomObject]@{ Success = $false; Error = "未找到 PLC 软件容器 (SoftwareContainer)" } | ConvertTo-Json -Depth 4)
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
        $errMsg = if ($matched.Count -eq 0) { "未找到名为 '$plcName' 的 PLC 设备或 CPU" } else { "名为 '$plcName' 的 PLC 匹配到多台设备，请使用唯一的站点/CPU 名称" }
        Write-Host ([PSCustomObject]@{
            Success    = $false
            Error      = $errMsg
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

function Find-BlockRecursive($group, [string]$name) {
    if (-not $group) { return $null }
    $b = $group.Blocks.Find($name)
    if ($b) { return $b }
    foreach ($sub in $group.Groups) {
        $res = Find-BlockRecursive $sub $name
        if ($res) { return $res }
    }
    return $null
}

function Find-TagTableRecursive($group, [string]$name) {
    if (-not $group) { return $null }
    $tt = $group.TagTables.Find($name)
    if ($tt) { return $tt }
    foreach ($sub in $group.Groups) {
        $res = Find-TagTableRecursive $sub $name
        if ($res) { return $res }
    }
    return $null
}

function Find-UdtRecursive($group, [string]$name) {
    if (-not $group) { return $null }
    $tp = $group.Types.Find($name)
    if ($tp) { return $tp }
    foreach ($sub in $group.Groups) {
        $res = Find-UdtRecursive $sub $name
        if ($res) { return $res }
    }
    return $null
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
        $hint = "请检查报错路径 '$($m.Path)' 对应的 LAD XML 逻辑、引脚或符号引用，修复后重跑 validate_lad_xml.py 与 -DiffOnly。"
        if ($desc -match "not defined|未定义|unknown|不存在") {
            $hint = "检测到未定义符号或块，请检查 TagTable_IO/TagTable_M 变量声明、全局定时器 DB 成员名或被调用 FB/FC 是否已包含在导入目录中。"
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

function Get-Sha256HexFromBytes([byte[]]$bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hashBytes) -replace "-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Sanitize-SafeFileName([string]$name) {
    if ([string]::IsNullOrWhiteSpace($name)) { return "unnamed" }
    return ($name -replace '[\\/:*?"<>|]', '_')
}

function Get-OnlineGateTicketPath([string]$OverridePath = "") {
    if (-not [string]::IsNullOrWhiteSpace($OverridePath)) {
        return $OverridePath
    }
    return (Join-Path ([System.IO.Path]::GetTempPath()) "eplan_tia_online_gate_tickets.json")
}

function Read-OnlineGateTicketStore([string]$storePath) {
    if (-not (Test-Path $storePath)) { return @() }
    try {
        $raw = [System.IO.File]::ReadAllText($storePath, [System.Text.Encoding]::UTF8)
        if ([string]::IsNullOrWhiteSpace($raw)) { return @() }
        $parsed = ConvertFrom-Json -InputObject $raw
        if ($null -eq $parsed) { return @() }
        return @($parsed)
    } catch {
        return @()
    }
}

function Write-OnlineGateTicketStore([string]$storePath, $tickets) {
    $parentDir = Split-Path -Path $storePath -Parent
    if ($parentDir -and -not (Test-Path $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }
    $arr = @($tickets)
    $json = if ($arr.Count -eq 0) { "[]" } elseif ($arr.Count -eq 1) { "[" + ($arr[0] | ConvertTo-Json -Depth 4 -Compress) + "]" } else { ($arr | ConvertTo-Json -Depth 4) }
    [System.IO.File]::WriteAllText($storePath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function New-CompareOnlineGateTicket(
    [string]$ProjectName,
    [string]$PlcName,
    [string]$CpuModel,
    [string]$CompareState,
    [bool]$IsFailSafePlc,
    [int]$TtlMinutes = 15,
    [string]$TicketStorePath = ""
) {
    $storePath = Get-OnlineGateTicketPath $TicketStorePath
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $nonceBytes = New-Object byte[] 16
    try {
        $rng.GetBytes($nonceBytes)
    } finally {
        $rng.Dispose()
    }
    $nonceHex = ([System.BitConverter]::ToString($nonceBytes) -replace "-", "").ToLowerInvariant()
    $issuedUtc = [DateTime]::UtcNow
    $expiresUtc = $issuedUtc.AddMinutes([Math]::Max(1, $TtlMinutes))
    $raw = "ONLINE_GATE|$ProjectName|$PlcName|$CpuModel|$CompareState|$nonceHex|$($issuedUtc.ToString('o'))"
    $token = (Get-Sha256HexFromBytes ([System.Text.Encoding]::UTF8.GetBytes($raw))).Substring(0, 16)

    $existing = @(Read-OnlineGateTicketStore $storePath)
    $nowUtc = [DateTime]::UtcNow
    $kept = @()
    foreach ($item in $existing) {
        try {
            $exp = [DateTime]::Parse("$($item.ExpiresAtUtc)").ToUniversalTime()
            if ((-not [bool]$item.Consumed) -and ($exp -ge $nowUtc)) {
                $kept += $item
            }
        } catch {}
    }
    $newTicket = [PSCustomObject]@{
        Token         = $token
        ProjectName   = $ProjectName
        PlcName       = $PlcName
        CpuModel      = $CpuModel
        CompareState  = $CompareState
        IsFailSafePlc = $IsFailSafePlc
        IssuedAtUtc   = $issuedUtc.ToString("o")
        ExpiresAtUtc  = $expiresUtc.ToString("o")
        Consumed      = $false
    }
    $kept += $newTicket
    if ($kept.Count -gt 20) {
        $kept = @($kept | Select-Object -Last 20)
    }
    Write-OnlineGateTicketStore $storePath $kept
    return $token
}

function Test-CompareOnlineGateTicket(
    [string]$Token,
    [string]$ProjectName,
    [string]$PlcName,
    [switch]$Consume,
    [string]$TicketStorePath = ""
) {
    $cleanToken = if ($null -eq $Token) { "" } else { $Token.Trim().ToLowerInvariant() }
    if ($cleanToken -notmatch "^[0-9a-f]{16}$") {
        return [PSCustomObject]@{
            Valid   = $false
            Reason  = "INVALID_ONLINE_TOKEN_FORMAT"
            Message = "ConfirmOnlineToken 格式非法（必须为 compare-online 签发的 16 位十六进制令牌）。"
            Ticket  = $null
        }
    }

    $storePath = Get-OnlineGateTicketPath $TicketStorePath
    $tickets = @(Read-OnlineGateTicketStore $storePath)
    if ($tickets.Count -eq 0) {
        return [PSCustomObject]@{
            Valid   = $false
            Reason  = "ONLINE_TOKEN_STORE_EMPTY"
            Message = "未找到有效的在线比对签发记录，请先执行 compare-online 获取有效令牌。"
            Ticket  = $null
        }
    }

    $matched = $null
    $matchedIdx = -1
    for ($i = $tickets.Count - 1; $i -ge 0; $i--) {
        if ("$($tickets[$i].Token)".ToLowerInvariant() -eq $cleanToken) {
            $matched = $tickets[$i]
            $matchedIdx = $i
            break
        }
    }

    if ($null -eq $matched) {
        return [PSCustomObject]@{
            Valid   = $false
            Reason  = "ONLINE_TOKEN_NOT_FOUND"
            Message = "提供的 ConfirmOnlineToken 未经 compare-online 签发，禁止执行实机下载。"
            Ticket  = $null
        }
    }

    if ([bool]$matched.Consumed) {
        return [PSCustomObject]@{
            Valid   = $false
            Reason  = "ONLINE_TOKEN_ALREADY_CONSUMED"
            Message = "该 ConfirmOnlineToken 已被消费（一次性令牌禁止重放），请重新执行 compare-online 比对。"
            Ticket  = $matched
        }
    }

    $nowUtc = [DateTime]::UtcNow
    try {
        $expUtc = [DateTime]::Parse("$($matched.ExpiresAtUtc)").ToUniversalTime()
        if ($nowUtc -gt $expUtc) {
            return [PSCustomObject]@{
                Valid   = $false
                Reason  = "ONLINE_TOKEN_EXPIRED"
                Message = "ConfirmOnlineToken 已超过有效期，请重新执行 compare-online 确认当前在线状态。"
                Ticket  = $matched
            }
        }
    } catch {
        return [PSCustomObject]@{
            Valid   = $false
            Reason  = "ONLINE_TOKEN_CORRUPTED"
            Message = "在线确认票据时间格式异常，请重新执行 compare-online。"
            Ticket  = $matched
        }
    }

    if (("$($matched.ProjectName)" -ine "$ProjectName") -or ("$($matched.PlcName)" -ine "$PlcName")) {
        return [PSCustomObject]@{
            Valid   = $false
            Reason  = "ONLINE_TOKEN_SCOPE_MISMATCH"
            Message = "ConfirmOnlineToken 所属工程/PLC ('$($matched.ProjectName)/$($matched.PlcName)') 与当前下载目标 ('$ProjectName/$PlcName') 不匹配！"
            Ticket  = $matched
        }
    }

    if ($Consume -and $matchedIdx -ge 0) {
        $tickets[$matchedIdx].Consumed = $true
        Write-OnlineGateTicketStore $storePath $tickets
    }

    return [PSCustomObject]@{
        Valid   = $true
        Reason  = "OK"
        Message = "ConfirmOnlineToken 校验通过"
        Ticket  = $matched
    }
}

Export-ModuleMember -Function *
