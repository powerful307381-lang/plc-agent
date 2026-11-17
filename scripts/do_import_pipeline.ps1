param(
    [string]$ImportDir = "",
    [int]$ProcessIndex = -1,
    [string]$ProjectPath = "",
    [string]$PlcName = "",
    [string]$BackupDir = "",
    [switch]$SkipExistingBlocks,
    [switch]$DiffOnly,
    [string]$SelectedBlocks = "",
    [string]$ConfirmToken = "",
    [switch]$CompileOutsideTransaction
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ([string]::IsNullOrWhiteSpace($ImportDir) -or -not (Test-Path $ImportDir)) {
    Write-Host ([PSCustomObject]@{ Success = $false; Stage = "Init"; Error = "ImportDir 参数缺失或目录不存在: '$ImportDir'" } | ConvertTo-Json -Depth 4)
    exit 1
}

function Resolve-OpennessDllPath {
    # 按版本号降序动态探测 TIA Portal V20 ~ V16 注册表与 PublicAPI 目录 (兼容 64/32 位视图)
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
    foreach ($ver in $versions) {
        $defaultCandidate = "C:\Program Files\Siemens\Automation\Portal V$ver\PublicAPI\V$ver\Siemens.Engineering.dll"
        if (Test-Path $defaultCandidate) { return $defaultCandidate }
    }
    return "C:\Program Files\Siemens\Automation\Portal V16\PublicAPI\V16\Siemens.Engineering.dll"
}

$dllPath = Resolve-OpennessDllPath
if (-not (Test-Path $dllPath)) {
    Write-Host ([PSCustomObject]@{ Success = $false; Stage = "Init"; Error = "未找到 Siemens.Engineering.dll: '$dllPath'" } | ConvertTo-Json -Depth 4)
    exit 1
}
[System.Reflection.Assembly]::LoadFrom($dllPath) | Out-Null

# 递归枚举所有设备与 PLC 目标
function Get-AllProjectDevices($project) {
    $list = @()
    foreach ($d in $project.Devices) { $list += $d }
    function Walk-DeviceGroup($grp) {
        $res = @()
        foreach ($d in $grp.Devices) { $res += $d }
        foreach ($sub in $grp.Groups) { $res += Walk-DeviceGroup $sub }
        return $res
    }
    foreach ($g in $project.DeviceGroups) { $list += Walk-DeviceGroup $g }
    if ($project.UngroupedDevicesGroup) {
        foreach ($d in $project.UngroupedDevicesGroup.Devices) { $list += $d }
    }
    return $list
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
        Write-Host ([PSCustomObject]@{ Success = $false; Error = $errMsg; Candidates = $candidates } | ConvertTo-Json -Depth 4)
        exit 2
    }
    if ($procIdx -ge 0) {
        if ($procIdx -ge $procs.Count) {
            Write-Host ([PSCustomObject]@{ Success = $false; Error = "指定的 ProcessIndex=$procIdx 越界（共 $($procs.Count) 个博途进程）"; Candidates = $candidates } | ConvertTo-Json -Depth 4)
            exit 2
        }
        return $procs[$procIdx]
    }
    if ($procs.Count -eq 1) { return $procs[0] }
    Write-Host ([PSCustomObject]@{ Success = $false; Error = "检测到 $($procs.Count) 个运行中的博途进程，请显式传入 -ProcessIndex 或 -ProjectPath"; Candidates = $candidates } | ConvertTo-Json -Depth 4)
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
        [PSCustomObject]@{ DeviceName = $_.DeviceName; PlcName = $_.PlcName; TypeIdentifier = $_.TypeIdentifier }
    })
    if ($plcName) {
        $matched = @($targets | Where-Object { $_.PlcName -ieq $plcName -or $_.DeviceName -ieq $plcName })
        if ($matched.Count -eq 1) { return $matched[0] }
        $errMsg = if ($matched.Count -eq 0) { "未找到名为 '$plcName' 的 PLC 设备或 CPU" } else { "名为 '$plcName' 的 PLC 匹配到多台设备，请使用唯一名称" }
        Write-Host ([PSCustomObject]@{ Success = $false; Error = $errMsg; Candidates = $candidates } | ConvertTo-Json -Depth 4)
        exit 2
    }
    if ($targets.Count -eq 1) { return $targets[0] }
    Write-Host ([PSCustomObject]@{ Success = $false; Error = "项目中包含 $($targets.Count) 台 PLC，请显式传入 -PlcName 指定目标 PLC"; Candidates = $candidates } | ConvertTo-Json -Depth 4)
    exit 2
}

# 断开指定设备的在线监视 (GoOffline)
function Disconnect-Online($deviceItem) {
    $methods = $deviceItem.GetType().GetMethods() | Where-Object { $_.Name -eq "GetService" -and $_.IsGenericMethod }
    if ($methods) {
        try {
            $online = $methods[0].MakeGenericMethod(@([Siemens.Engineering.Online.OnlineProvider])).Invoke($deviceItem, $null)
            if ($online) { $online.GoOffline() }
        } catch {}
    }
    foreach ($sub in $deviceItem.DeviceItems) {
        Disconnect-Online $sub
    }
}

# 0. 全局单实例串行排队互斥锁（防止多工位并发写入触发 Openness COM 独占锁崩溃）
$serialMutex = New-Object System.Threading.Mutex($false, "Global\TiaPortalOpennessSerialLock")
$mutexAcquired = $false
try {
    try {
        $mutexAcquired = $serialMutex.WaitOne(120000)
    } catch [System.Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        Write-Host ([PSCustomObject]@{
            Success = $false
            Stage   = "SerialLock"
            Error   = "等待 TIA Portal Openness 串行排队锁超时（120s），请确认无其他导入任务卡死后重试。"
        } | ConvertTo-Json -Depth 4)
        exit 1
    }

# 严禁对 GetProcesses() 返回的句柄调用 Dispose()
$procs = [Siemens.Engineering.TiaPortal]::GetProcesses()
$targetProc = Select-TiaProcess $procs $ProcessIndex $ProjectPath
try {
    $tia = $targetProc.Attach()
} catch [Siemens.Engineering.EngineeringSecurityException] {
    Write-Host ([PSCustomObject]@{ Success = $false; Error = "Openness 防火墙拒绝访问，请在博途弹窗中点击【全部允许】" } | ConvertTo-Json -Depth 4)
    exit 1
}

if ($tia.Projects.Count -eq 0) {
    Write-Host ([PSCustomObject]@{ Success = $false; Error = "当前博途进程未打开任何项目" } | ConvertTo-Json -Depth 4)
    exit 1
}
$proj = $tia.Projects[0]

$plcTarget = Select-PlcSoftware $proj $PlcName
$targetDevice = $plcTarget.Device
$sw = $plcTarget.Software

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

function Get-XmlArtifactInfo($xmlPath) {
    $fallbackName = [System.IO.Path]::GetFileNameWithoutExtension($xmlPath)
    $res = [PSCustomObject]@{ Kind = "Unknown"; Name = $fallbackName }
    if (-not (Test-Path $xmlPath)) { return $res }
    try {
        [xml]$x = Get-Content $xmlPath -Encoding UTF8 -Raw
        $kinds = @(
            @{ Tag = "SW.Types.PlcStruct";    Kind = "UDT" },
            @{ Tag = "SW.Tags.PlcTagTable";   Kind = "TagTable" },
            @{ Tag = "SW.Blocks.GlobalDB";    Kind = "GlobalDB" },
            @{ Tag = "SW.Blocks.FB";          Kind = "FB" },
            @{ Tag = "SW.Blocks.InstanceDB";  Kind = "InstanceDB" },
            @{ Tag = "SW.Blocks.FC";          Kind = "FC" },
            @{ Tag = "SW.Blocks.OB";          Kind = "OB" },
            @{ Tag = "SW.WatchAndForceTables.PlcWatchTable"; Kind = "WatchTable" },
            @{ Tag = "SW.WatchAndForceTables.PlcForceTable"; Kind = "ForceTable" }
        )
        foreach ($k in $kinds) {
            $blkNode = $x.SelectSingleNode("//*[local-name()='$($k.Tag)']")
            if ($blkNode) {
                $nameNode = $blkNode.SelectSingleNode("./*[local-name()='AttributeList']/*[local-name()='Name']")
                $bName = if ($nameNode -and -not [string]::IsNullOrWhiteSpace($nameNode.InnerText)) { $nameNode.InnerText.Trim() } else { $fallbackName }
                return [PSCustomObject]@{ Kind = $k.Kind; Name = $bName }
            }
        }
    } catch {}
    return $res
}

function Get-XmlBlockName($xmlPath) {
    return (Get-XmlArtifactInfo $xmlPath).Name
}

function Compare-XmlFingerprint($oldXmlPath, $newXmlPath) {
    $fpScript = Join-Path $PSScriptRoot "block_fingerprint.py"
    if (-not (Test-Path $fpScript)) {
        return [PSCustomObject]@{ Ok = $false; Diffs = @(); Error = "缺少语义指纹脚本 block_fingerprint.py" }
    }
    $prevEncoding = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = "utf-8"
    try {
        $rawOut = & python $fpScript $oldXmlPath $newXmlPath 2>&1
        $exitCode = $LASTEXITCODE
        $rawStr = (($rawOut | ForEach-Object { "$_" }) -join "`n").Trim()
        if ($exitCode -ne 0) {
            return [PSCustomObject]@{ Ok = $false; Diffs = @(); Error = "指纹脚本返回错误码 $exitCode`: $rawStr" }
        }
        $parsed = ConvertFrom-Json -InputObject $rawStr
        $diffList = @()
        if ($null -ne $parsed) {
            foreach ($item in @($parsed)) {
                if (-not [string]::IsNullOrWhiteSpace("$item")) {
                    $diffList += "$item"
                }
            }
        }
        return [PSCustomObject]@{ Ok = $true; Diffs = $diffList; Error = "" }
    } catch {
        return [PSCustomObject]@{ Ok = $false; Diffs = @(); Error = $_.Exception.Message }
    } finally {
        $env:PYTHONIOENCODING = $prevEncoding
    }
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

function Get-DiffConfirmToken($xmlFiles, $diffList, [string]$plcScope, [bool]$skipExisting, [string]$selectedBlk) {
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine("SCOPE=$plcScope|SKIP=$skipExisting|SEL=$selectedBlk")
    foreach ($xf in ($xmlFiles | Sort-Object Name)) {
        $fBytes = [System.IO.File]::ReadAllBytes($xf.FullName)
        $fHash = Get-Sha256HexFromBytes $fBytes
        [void]$sb.AppendLine("FILE=$($xf.Name):$fHash")
    }
    foreach ($ds in ($diffList | Sort-Object Kind, BlockName)) {
        [void]$sb.AppendLine("DIFF=$($ds.Kind)|$($ds.BlockName)|$($ds.ExistsInPlc)|$($ds.HasSubstantiveDiff)|$($ds.ActionTaken)|$($ds.Differences)")
    }
    $hex = Get-Sha256HexFromBytes ([System.Text.Encoding]::UTF8.GetBytes($sb.ToString()))
    return $hex.Substring(0, 16)
}

function Get-IncomingXmlFiles([string]$dir) {
    $normDir = $dir.TrimEnd('\', '/')
    $byName = @{}
    $subTag = Join-Path $normDir "02_变量表"
    $subLad = Join-Path $normDir "03_梯形图XML"
    if ((Test-Path $subTag) -or (Test-Path $subLad)) {
        foreach ($sub in @($subTag, $subLad)) {
            if (Test-Path $sub) {
                foreach ($xf in (Get-ChildItem -Path $sub -Filter "*.xml" -ErrorAction SilentlyContinue)) {
                    $k = $xf.Name.ToLowerInvariant()
                    if (-not $byName.ContainsKey($k)) { $byName[$k] = $xf }
                }
            }
        }
    } else {
        foreach ($xf in (Get-ChildItem -Path $normDir -Filter "*.xml" -ErrorAction SilentlyContinue)) {
            $k = $xf.Name.ToLowerInvariant()
            if (-not $byName.ContainsKey($k)) { $byName[$k] = $xf }
        }
        $leaf = Split-Path $normDir -Leaf
        if ($leaf -ieq "03_梯形图XML") {
            $parentDir = Split-Path $normDir -Parent
            $siblingTagDir = Join-Path $parentDir "02_变量表"
            if (Test-Path $siblingTagDir) {
                foreach ($xf in (Get-ChildItem -Path $siblingTagDir -Filter "*.xml" -ErrorAction SilentlyContinue)) {
                    $k = $xf.Name.ToLowerInvariant()
                    if (-not $byName.ContainsKey($k)) { $byName[$k] = $xf }
                }
            }
        }
    }
    return @($byName.Values | Sort-Object Name)
}

# 3. 增量保护与语义指纹预检 (Pre-Import Diff Check，严格只读)
$diffSummaries = @()
$tempExportDir = Join-Path ([System.IO.Path]::GetTempPath()) "TiaImportDiffTemp_$([System.Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $tempExportDir -Force | Out-Null

$allIncomingXmls = @(Get-IncomingXmlFiles $ImportDir)
try {
    foreach ($ix in $allIncomingXmls) {
        $artInfo = Get-XmlArtifactInfo $ix.FullName
        if ($artInfo.Kind -eq "Unknown") { continue }
        $bName = $artInfo.Name

        $existing = $null
        if ($artInfo.Kind -eq "UDT") {
            $existing = Find-UdtRecursive $sw.TypeGroup $bName
        } elseif ($artInfo.Kind -eq "TagTable") {
            $existing = Find-TagTableRecursive $sw.TagTableGroup $bName
        } elseif ($artInfo.Kind -eq "WatchTable" -and $sw.WatchAndForceTableGroup) {
            $existing = $sw.WatchAndForceTableGroup.WatchTables.Find($bName)
        } elseif ($artInfo.Kind -eq "ForceTable" -and $sw.WatchAndForceTableGroup) {
            $existing = $sw.WatchAndForceTableGroup.ForceTables.Find($bName)
        } else {
            $existing = Find-BlockRecursive $sw.BlockGroup $bName
        }

        if ($existing) {
            $tempExportFile = Join-Path $tempExportDir "$($artInfo.Kind)_$bName.xml"
            try {
                $existing.Export((New-Object System.IO.FileInfo $tempExportFile), [Siemens.Engineering.ExportOptions]::WithDefaults)
                $cmp = Compare-XmlFingerprint $tempExportFile $ix.FullName
                if (-not $cmp.Ok) {
                    $diffSummaries += [PSCustomObject]@{
                        Kind               = $artInfo.Kind
                        BlockName          = $bName
                        FileName           = $ix.Name
                        ExistsInPlc        = $true
                        CompareFailed      = $true
                        HasSubstantiveDiff = $true
                        Differences        = "⚠ 语义指纹比对失败（Fail-Closed 按存在改动处理）: $($cmp.Error)"
                        ActionTaken        = if ($SkipExistingBlocks) { "SKIPPED_EXISTING" } else { "COMPARE_FAILED_WILL_OVERWRITE" }
                    }
                } else {
                    $hasDiff = ($cmp.Diffs.Count -gt 0)
                    $diffText = if ($hasDiff) { ($cmp.Diffs -join "; ") } else { "仅 UID/格式元数据差异（无实质语义改动）" }
                    $diffSummaries += [PSCustomObject]@{
                        Kind               = $artInfo.Kind
                        BlockName          = $bName
                        FileName           = $ix.Name
                        ExistsInPlc        = $true
                        CompareFailed      = $false
                        HasSubstantiveDiff = $hasDiff
                        Differences        = $diffText
                        ActionTaken        = if ($SkipExistingBlocks -and $hasDiff) { "SKIPPED_EXISTING" } elseif ($hasDiff) { "OVERWRITE" } else { "IDENTICAL_UID_ONLY" }
                    }
                }
            } catch {
                $diffSummaries += [PSCustomObject]@{
                    Kind               = $artInfo.Kind
                    BlockName          = $bName
                    FileName           = $ix.Name
                    ExistsInPlc        = $true
                    CompareFailed      = $true
                    HasSubstantiveDiff = $true
                    Differences        = "⚠ 博途现有对象导出失败（Fail-Closed 按存在改动处理）: $($_.Exception.Message)"
                    ActionTaken        = if ($SkipExistingBlocks) { "SKIPPED_EXISTING" } else { "COMPARE_FAILED_WILL_OVERWRITE" }
                }
            }
        } else {
            $diffSummaries += [PSCustomObject]@{
                Kind               = $artInfo.Kind
                BlockName          = $bName
                FileName           = $ix.Name
                ExistsInPlc        = $false
                CompareFailed      = $false
                HasSubstantiveDiff = $true
                Differences        = "博途中尚无同名对象，将首次创建"
                ActionTaken        = "NEW_CREATE"
            }
        }
    }
} finally {
    Remove-Item -Path $tempExportDir -Recurse -Force -ErrorAction SilentlyContinue
}

$plcScope = "$($plcTarget.DeviceName)|$($plcTarget.PlcName)"
$currentToken = Get-DiffConfirmToken $allIncomingXmls $diffSummaries $plcScope ([bool]$SkipExistingBlocks) $SelectedBlocks

if ($DiffOnly) {
    $hasAnyDiff = @($diffSummaries | Where-Object { $_.HasSubstantiveDiff }).Count -gt 0
    $diffOutput = [PSCustomObject]@{
        Success               = $true
        DiffOnly              = $true
        ProjectName           = $proj.Name
        PlcName               = $plcTarget.PlcName
        HasAnySubstantiveDiff = $hasAnyDiff
        ConfirmToken          = $currentToken
        PreImportDiffs        = $diffSummaries
    }
    $diffOutput | ConvertTo-Json -Depth 6
    exit 0
}

# 4. Gate 3 强制确认门禁（未持有匹配的 -ConfirmToken 禁止执行任何工程备份或写操作）
if ([string]::IsNullOrWhiteSpace($ConfirmToken) -or ($ConfirmToken.Trim().ToLowerInvariant() -ne $currentToken)) {
    Write-Host ([PSCustomObject]@{
        Success        = $false
        Stage          = "Gate3"
        ProjectName    = $proj.Name
        PlcName        = $plcTarget.PlcName
        ExpectedToken  = $currentToken
        ProvidedToken  = $ConfirmToken
        PreImportDiffs = $diffSummaries
        Error          = "未通过 Gate 3：必须先运行 -DiffOnly 预检并经用户明确确认变更清单后，将返回的 ConfirmToken 原样传入 -ConfirmToken 参数。严禁跳过。"
    } | ConvertTo-Json -Depth 6)
    exit 1
}

# 5. 导入前强制保存工程与压缩归档备份 (Fail-Closed：未传 BackupDir 或归档失败则中止导入)
$archivePath = ""
if ([string]::IsNullOrWhiteSpace($BackupDir)) {
    Write-Host ([PSCustomObject]@{
        Success = $false
        Stage   = "Backup"
        Error   = "必须提供 -BackupDir 参数执行导入前工程归档(.zap16)，未备份禁止修改博途工程"
    } | ConvertTo-Json -Depth 4)
    exit 1
}

try {
    if (-not (Test-Path $BackupDir)) {
        New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    }
    $ts = Get-Date -Format "yyyyMMdd_HHmmss"
    $zapName = "$($proj.Name)_backup_$ts.zap16"
    $expectedArchivePath = Join-Path $BackupDir $zapName
    $proj.Save()
    $dirInfo = New-Object System.IO.DirectoryInfo($BackupDir)
    $proj.Archive($dirInfo, $zapName, [Siemens.Engineering.ProjectArchivationMode]::Compressed)
    if (-not (Test-Path $expectedArchivePath) -or ((Get-Item $expectedArchivePath).Length -le 0)) {
        throw "归档文件未生成或大小为 0: $expectedArchivePath"
    }
    $archivePath = $expectedArchivePath
} catch {
    Write-Host ([PSCustomObject]@{
        Success = $false
        Stage   = "Backup"
        Error   = "导入前保存与归档备份失败（已中止导入以保护原工程）: $($_.Exception.Message)"
    } | ConvertTo-Json -Depth 4)
    exit 1
}

$blockRollbackDir = Join-Path $BackupDir "block_rollback_$ts"
New-Item -ItemType Directory -Path $blockRollbackDir -Force | Out-Null
$blockRollbackList = @()
$blockRollbackExecuted = $false
$restoredBlocks = @()
$actionableCompileErrors = @()

function ConvertTo-ActionableCompileErrors($messages) {
    $out = @()
    foreach ($m in $messages) {
        if ($m.Severity -ne "Error") { continue }
        $desc = "$($m.Description)"
        $hint = "请检查报错路径 '$($m.Path)' 对应的 LAD XML 逻辑、引脚或符号引用，修复后重跑 validate_lad_xml.py 与 -DiffOnly。"
        if ($desc -match "not defined|未定义|unknown|不存在") {
            $hint = "未定义符号或块：检查 TagTable 变量表、全局定时器 DB 成员或被调 FB/FC 是否在导入目录中。"
        } elseif ($desc -match "data type|数据类型|mismatch|不匹配") {
            $hint = "数据类型不匹配：检查 Bool/Int/Time 操作数或块接口形参定义。"
        } elseif ($desc -match "time stamp|时间戳|interface|接口") {
            $hint = "接口或时间戳冲突：先确认 UDT 与被调 FB，再重新生成调用块与 InstanceDB。"
        }
        $out += [PSCustomObject]@{
            Path          = $m.Path
            Description   = $desc
            FixSuggestion = $hint
        }
    }
    return $out
}

# 6. 仅断开目标 PLC 设备的在线监视 (GoOffline)
Disconnect-Online $targetDevice

$importLogs = @()
$compileLogs = @()
$crossRefLogs = @()
$compileState = "Unknown"
$compileMode = if ($CompileOutsideTransaction) { "OutsideTransaction" } else { "InsideTransaction" }
$errCount = 0
$warnCount = 0
$overrideOpt = [Siemens.Engineering.ImportOptions]::Override

function Extract-CompileMessages($msgNode, $parentPath = "") {
    $list = @()
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
            $list += Extract-CompileMessages $sub $curPath
        }
    }
    return $list
}

function Invoke-PlcCompile($targetSw) {
    $compMethods = $targetSw.GetType().GetMethods() | Where-Object { $_.Name -eq "GetService" -and $_.IsGenericMethod }
    $compService = $compMethods[0].MakeGenericMethod(@([Siemens.Engineering.Compiler.ICompilable])).Invoke($targetSw, $null)
    $compRes = $compService.Compile()
    $msgs = @()
    foreach ($m in $compRes.Messages) {
        $msgs += Extract-CompileMessages $m
    }
    return [PSCustomObject]@{
        State        = "$($compRes.State)"
        ErrorCount   = [int]$compRes.ErrorCount
        WarningCount = [int]$compRes.WarningCount
        Messages     = $msgs
    }
}

function Collect-CrossReferences($rootBlkGrp) {
    $refs = @()
    try {
        $filter = [Siemens.Engineering.CrossReference.CrossReferenceFilter]::AllObjects
        foreach ($b in $rootBlkGrp.Blocks) {
            $xrMethods = $b.GetType().GetMethods() | Where-Object { $_.Name -eq "GetService" -and $_.IsGenericMethod }
            if ($xrMethods) {
                try {
                    $xrService = $xrMethods[0].MakeGenericMethod(@([Siemens.Engineering.CrossReference.CrossReferenceService])).Invoke($b, $null)
                    if ($xrService) {
                        $xr = $xrService.GetCrossReferences($filter)
                        $srcCount = if ($xr.Sources) { $xr.Sources.Count } else { 0 }
                        $refs += [PSCustomObject]@{
                            Block          = $b.Name
                            ReferenceCount = $srcCount
                        }
                    }
                } catch {}
            }
        }
    } catch {}
    return $refs
}

# 7. 使用 ExclusiveAccess + Transaction 保证导入原子性，并兼容事务内外编译模式
$exclusiveAccess = $null
$transaction = $null
$committed = $false
$compileFailedAfterCommit = $false
$rollbackRequired = $false
$recoveryHint = ""

$selectedSet = @()
if (-not [string]::IsNullOrWhiteSpace($SelectedBlocks)) {
    $selectedSet = $SelectedBlocks.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
}

$skipKeys = @()
if ($SkipExistingBlocks) {
    foreach ($ds in $diffSummaries) {
        if ($ds.ActionTaken -eq "SKIPPED_EXISTING") {
            $skipKeys += "$($ds.Kind)|$($ds.BlockName)"
        }
    }
}

try {
    $exclusiveAccess = $tia.ExclusiveAccess("EPLAN_LAD自动化导入与编译")
    $transaction = $exclusiveAccess.Transaction($proj, "导入EPLAN梯形图与变量表")

    $tagTableGroup = $sw.TagTableGroup
    $rootBlockGroup = $sw.BlockGroup
    $typeGroup = $sw.TypeGroup

    # 按 XML 节点类型精确分类，并严格按依赖顺序导入：
    # UDT (TypeGroup.Types) -> TagTable -> GlobalDB -> FB -> InstanceDB -> FC -> OB
    $classifiedXmls = @()
    foreach ($xf in $allIncomingXmls) {
        $info = Get-XmlArtifactInfo $xf.FullName
        $classifiedXmls += [PSCustomObject]@{
            File = $xf
            Kind = $info.Kind
            Name = $info.Name
        }
    }

    function Import-StageGroup($items, $stageLabel, $targetCollection, [bool]$allowSkip = $true, [bool]$twoPassRetry = $false) {
        $stageLogs = New-Object System.Collections.ArrayList
        if (-not $items -or $items.Count -eq 0) { return $stageLogs }
        $pending = @($items)
        $maxPass = if ($twoPassRetry -and $items.Count -gt 1) { 2 } else { 1 }
        for ($pass = 1; $pass -le $maxPass; $pass++) {
            $nextPending = @()
            foreach ($it in $pending) {
                $xf = $it.File
                if ($selectedSet.Count -gt 0 -and -not ($selectedSet -contains $it.Name)) {
                    [void]$stageLogs.Add([PSCustomObject]@{ File = $xf.Name; Status = "SKIPPED"; Type = $it.Kind; Reason = "不在 SelectedBlocks 指定列表中" })
                    continue
                }
                if ($allowSkip -and ($skipKeys -contains "$($it.Kind)|$($it.Name)")) {
                    [void]$stageLogs.Add([PSCustomObject]@{ File = $xf.Name; Status = "SKIPPED"; Type = $it.Kind; Reason = "保留博途现场修改" })
                    continue
                }
                try {
                    # 事务性块覆盖保护：先导出博途中已存在原块的 XML 快照以支持失败回滚
                    $existingObj = $null
                    if ($it.Kind -eq "UDT") {
                        $existingObj = Find-UdtRecursive $typeGroup $it.Name
                    } elseif ($it.Kind -eq "TagTable") {
                        $existingObj = Find-TagTableRecursive $tagTableGroup $it.Name
                    } elseif ($it.Kind -eq "WatchTable" -and $sw.WatchAndForceTableGroup) {
                        $existingObj = $sw.WatchAndForceTableGroup.WatchTables.Find($it.Name)
                    } elseif ($it.Kind -eq "ForceTable" -and $sw.WatchAndForceTableGroup) {
                        $existingObj = $sw.WatchAndForceTableGroup.ForceTables.Find($it.Name)
                    } else {
                        $existingObj = Find-BlockRecursive $rootBlockGroup $it.Name
                    }
                    if ($existingObj) {
                        $rbFile = Join-Path $blockRollbackDir "$($it.Kind)_$($it.Name).xml"
                        try {
                            $existingObj.Export((New-Object System.IO.FileInfo $rbFile), [Siemens.Engineering.ExportOptions]::WithDefaults)
                            if (Test-Path $rbFile) {
                                $script:blockRollbackList += [PSCustomObject]@{
                                    Kind       = $it.Kind
                                    Name       = $it.Name
                                    BackupFile = $rbFile
                                    Collection = $targetCollection
                                }
                            }
                        } catch {}
                    }

                    $exclusiveAccess.Text = "正在导入${stageLabel}: $($xf.Name)"
                    $fi = New-Object System.IO.FileInfo($xf.FullName)
                    $null = $targetCollection.Import($fi, $overrideOpt)
                    [void]$stageLogs.Add([PSCustomObject]@{ File = $xf.Name; Status = "OK"; Type = $it.Kind })
                } catch {
                    if ($pass -lt $maxPass) {
                        $nextPending += $it
                    } else {
                        [void]$stageLogs.Add([PSCustomObject]@{ File = $xf.Name; Status = "FAIL"; Type = $it.Kind; Error = $_.Exception.Message })
                    }
                }
            }
            $pending = $nextPending
            if ($pending.Count -eq 0) { break }
        }
        return $stageLogs
    }

    function Import-PostCompileTables {
        $tblLogs = New-Object System.Collections.ArrayList
        if (-not $sw.WatchAndForceTableGroup) { return $tblLogs }
        foreach ($l in @(Import-StageGroup ($classifiedXmls | Where-Object { $_.Kind -eq "WatchTable" }) "监控表(WatchTable)" $sw.WatchAndForceTableGroup.WatchTables $true $false)) {
            [void]$tblLogs.Add($l)
        }
        $forceXmls = @($classifiedXmls | Where-Object { $_.Kind -eq "ForceTable" })
        foreach ($ftItem in $forceXmls) {
            $existingFt = $null
            try { $existingFt = $sw.WatchAndForceTableGroup.ForceTables.Find($ftItem.Name) } catch {}
            if ($existingFt) {
                foreach ($l in @(Import-StageGroup @($ftItem) "强制表(ForceTable)" $sw.WatchAndForceTableGroup.ForceTables $true $false)) {
                    [void]$tblLogs.Add($l)
                }
            } else {
                # TIA Portal V16 Openness 的 ForceTables 为只读单例集合，不支持新建命名强制表
                [void]$tblLogs.Add([PSCustomObject]@{
                    File   = $ftItem.File.Name
                    Status = "SKIPPED"
                    Type   = "ForceTable"
                    Reason = "TIA V16 Openness ForceTables 为只读单例集合，不支持新建命名强制表（冷态强制表 XML 已留档供手动导入参考）"
                })
            }
        }
        return $tblLogs
    }

    $importLogs += @(Import-StageGroup ($classifiedXmls | Where-Object { $_.Kind -eq "UDT" })        "用户自定义类型(UDT)" $typeGroup.Types      $true $true)
    $importLogs += @(Import-StageGroup ($classifiedXmls | Where-Object { $_.Kind -eq "TagTable" })   "变量表(TagTable)"    $tagTableGroup.TagTables $true $false)
    $importLogs += @(Import-StageGroup ($classifiedXmls | Where-Object { $_.Kind -eq "GlobalDB" })   "全局数据块(GlobalDB)" $rootBlockGroup.Blocks $true $false)
    $importLogs += @(Import-StageGroup ($classifiedXmls | Where-Object { $_.Kind -eq "FB" })         "功能块(FB)"          $rootBlockGroup.Blocks $true $true)
    $importLogs += @(Import-StageGroup ($classifiedXmls | Where-Object { $_.Kind -eq "InstanceDB" }) "背景数据块(InstanceDB)" $rootBlockGroup.Blocks $true $false)
    $importLogs += @(Import-StageGroup ($classifiedXmls | Where-Object { $_.Kind -eq "FC" })         "函数(FC)"            $rootBlockGroup.Blocks $true $true)
    $importLogs += @(Import-StageGroup ($classifiedXmls | Where-Object { $_.Kind -eq "OB" })         "组织块(OB)"          $rootBlockGroup.Blocks $true $false)

    $hasImportFail = @($importLogs | Where-Object { $_.Status -eq "FAIL" }).Count -gt 0
    if ($hasImportFail) {
        $compileState = "AbortedOnImportFail"
        # 未提交事务且在退出前按倒序执行块级显式回滚保护
        if ($blockRollbackList.Count -gt 0) {
            $blockRollbackExecuted = $true
            for ($ri = $blockRollbackList.Count - 1; $ri -ge 0; $ri--) {
                $rb = $blockRollbackList[$ri]
                try {
                    $null = $rb.Collection.Import((New-Object System.IO.FileInfo $rb.BackupFile), $overrideOpt)
                    $restoredBlocks += "$($rb.Kind):$($rb.Name)"
                } catch {}
            }
        }
    } else {
        $runOutsideTx = [bool]$CompileOutsideTransaction
        $compRes = $null
        if (-not $runOutsideTx) {
            try {
                $exclusiveAccess.Text = "正在执行 PLC 软件编译（事务内）..."
                $compRes = Invoke-PlcCompile $sw
                $compileMode = "InsideTransaction"
            } catch {
                $compileMode = "FallbackOutsideTransaction"
                $runOutsideTx = $true
                $compileLogs += [PSCustomObject]@{
                    Severity    = "Warning"
                    Description = "事务内编译被 Openness 拒绝或抛出异常，已自动提交导入并切换为事务外编译: $($_.Exception.Message)"
                    Path        = ""
                    DateTime    = (Get-Date -Format "o")
                }
            }
        }

        if (-not $runOutsideTx) {
            $compileState = $compRes.State
            $errCount = $compRes.ErrorCount
            $warnCount = $compRes.WarningCount
            $compileLogs += $compRes.Messages
            if ($errCount -eq 0 -and $compileState -ne "Error") {
                $crossRefLogs = Collect-CrossReferences $rootBlockGroup
                $transaction.CommitOnDispose()
                $committed = $true
                $transaction.Dispose()
                $transaction = $null

                # 编译已提交，DB/UDT 块已处于一致态后再导入 WatchTable（防止 DB 成员引用因未提交而报错）
                $wtTx = $exclusiveAccess.Transaction($proj, "导入监控表")
                try {
                    $importLogs += @(Import-PostCompileTables)
                    if (@($importLogs | Where-Object { $_.Status -eq "FAIL" }).Count -gt 0) {
                        $compileState = "AbortedOnWatchTableFail"
                    } else {
                        $wtTx.CommitOnDispose()
                    }
                } finally {
                    if ($wtTx) { $wtTx.Dispose() }
                }
            } else {
                $actionableCompileErrors = @(ConvertTo-ActionableCompileErrors $compileLogs)
                if ($blockRollbackList.Count -gt 0) {
                    $blockRollbackExecuted = $true
                    for ($ri = $blockRollbackList.Count - 1; $ri -ge 0; $ri--) {
                        $rb = $blockRollbackList[$ri]
                        try {
                            $null = $rb.Collection.Import((New-Object System.IO.FileInfo $rb.BackupFile), $overrideOpt)
                            $restoredBlocks += "$($rb.Kind):$($rb.Name)"
                        } catch {}
                    }
                }
            }
        } else {
            # 先提交并释放 Transaction，保持 ExclusiveAccess 再执行编译
            $transaction.CommitOnDispose()
            $committed = $true
            $transaction.Dispose()
            $transaction = $null

            $exclusiveAccess.Text = "正在执行 PLC 软件编译（事务外）..."
            $compRes = Invoke-PlcCompile $sw
            $compileState = $compRes.State
            $errCount = $compRes.ErrorCount
            $warnCount = $compRes.WarningCount
            $compileLogs += $compRes.Messages
            if ($errCount -eq 0 -and $compileState -ne "Error") {
                $importLogs += @(Import-PostCompileTables)
                $crossRefLogs = Collect-CrossReferences $rootBlockGroup
            } else {
                $compileFailedAfterCommit = $true
                $rollbackRequired = $true
                $actionableCompileErrors = @(ConvertTo-ActionableCompileErrors $compileLogs)
                if ($blockRollbackList.Count -gt 0) {
                    $rbTx = $null
                    try {
                        $rbTx = $exclusiveAccess.Transaction($proj, "编译失败回滚原块")
                        $blockRollbackExecuted = $true
                        for ($ri = $blockRollbackList.Count - 1; $ri -ge 0; $ri--) {
                            $rb = $blockRollbackList[$ri]
                            try {
                                $null = $rb.Collection.Import((New-Object System.IO.FileInfo $rb.BackupFile), $overrideOpt)
                                $restoredBlocks += "$($rb.Kind):$($rb.Name)"
                            } catch {}
                        }
                        $rbTx.CommitOnDispose()
                    } catch {
                    } finally {
                        if ($rbTx) { $rbTx.Dispose() }
                    }
                }
                $recoveryHint = "事务外编译存在 $errCount 个错误（已自动回滚 $($restoredBlocks.Count) 个原块）；系统未执行 Project.Save()。也可使用归档备份恢复工程: $archivePath"
            }
        }
    }
} catch {
    $compileState = "Exception"
    $compileLogs += [PSCustomObject]@{ Severity = "Error"; Description = $_.Exception.Message; Path = ""; DateTime = (Get-Date -Format "o") }
    if ($committed) {
        $compileFailedAfterCommit = $true
        $rollbackRequired = $true
        $recoveryHint = "事务提交后编译/收尾阶段出现异常；系统未执行 Project.Save()。请从备份恢复: $archivePath"
    }
} finally {
    if ($transaction) { $transaction.Dispose() }
    if ($exclusiveAccess) { $exclusiveAccess.Dispose() }
}

# 8. 仅当全部导入成功、事务已提交且编译零错误时，才持久化保存工程并校验保存结果
$projSaved = $false
$saveError = ""
$hasImportFailFinal = @($importLogs | Where-Object { $_.Status -eq "FAIL" }).Count -gt 0
if ($committed -and (-not $compileFailedAfterCommit) -and (-not $hasImportFailFinal) -and ($errCount -eq 0) -and ($compileState -ne "Error")) {
    try {
        $proj.Save()
        $projSaved = $true
    } catch {
        $saveError = $_.Exception.Message
        $projSaved = $false
    }
}

$finalSuccess = ($committed -and $projSaved -and (-not $saveError) -and (-not $compileFailedAfterCommit) -and (-not $hasImportFailFinal) -and ($errCount -eq 0) -and ($compileState -ne "Error"))

$finalOutput = [PSCustomObject]@{
    Success                 = $finalSuccess
    ProjectName             = $proj.Name
    PlcName                 = $plcTarget.PlcName
    TransactionCommitted    = $committed
    Saved                   = $projSaved
    SaveError               = $saveError
    RollbackRequired        = $rollbackRequired
    BlockRollbackExecuted   = $blockRollbackExecuted
    RestoredBlocks          = $restoredBlocks
    RecoveryHint            = $recoveryHint
    ArchiveBackupPath       = $archivePath
    CompileMode             = $compileMode
    CompileState            = $compileState
    ErrorCount              = $errCount
    WarningCount            = $warnCount
    PreImportDiffs          = $diffSummaries
    CrossReferences         = $crossRefLogs
    ImportLogs              = $importLogs
    CompileMessages         = $compileLogs
    ActionableCompileErrors = $actionableCompileErrors
}

$finalOutput | ConvertTo-Json -Depth 6
} finally {
    if ($mutexAcquired) { try { $serialMutex.ReleaseMutex() } catch {} }
    if ($serialMutex) { $serialMutex.Dispose() }
}
