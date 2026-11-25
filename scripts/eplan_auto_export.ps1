param(
    [string]$Action = "inspect-eplan",
    [string]$EplanProjectPath = "",
    [string]$OutputDir = "",
    [string]$PlcScheme = "PLC_IO_Excel_Export",
    [string]$BomScheme = "BOM_Part_Excel_Export",
    [string]$W3uPath = "",
    [string]$TiaVersion = "16",
    [string]$ConverterId = "",
    [int]$TimeoutSeconds = 300
)

if ([string]::IsNullOrWhiteSpace($ConverterId)) {
    $verClean = ($TiaVersion -replace "[^0-9]", "")
    if (-not $verClean) { $verClean = "16" }
    $ConverterId = "PlcDcExchangerSiemensTIA${verClean}AML"
}

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-JsonResult($obj, [int]$exitCode = 0) {
    Write-Host ($obj | ConvertTo-Json -Depth 6 -Compress)
    exit $exitCode
}

function Resolve-EplanW3uPath([string]$explicitPath) {
    $candidates = New-Object System.Collections.Generic.List[string]

    function Add-W3uCandidate([string]$rawValue) {
        if ([string]::IsNullOrWhiteSpace($rawValue)) { return }
        $vStr = $rawValue.Trim().Trim('"')
        # 剥离可能附带的命令行参数
        if ($vStr -match '^(?i)([A-Z]:\\[^"]+?\.exe)\b') {
            $vStr = $Matches[1]
        }
        $probes = @(
            $vStr,
            (Join-Path $vStr "W3u.exe"),
            (Join-Path $vStr "Bin\W3u.exe")
        )
        foreach ($probe in $probes) {
            if ($probe.EndsWith("W3u.exe", [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path $probe -PathType Leaf)) {
                if (-not $candidates.Contains($probe)) { [void]$candidates.Add($probe) }
            }
        }
    }

    Add-W3uCandidate $explicitPath

    # 1. 从注册表 HKLM\SOFTWARE\EPLAN\EPLAN W3 及其产品/版本子项递归探测 (兼容 64/32 位视图及 SystemStartUp/SystemDirectories)
    $regRoots = @(
        "SOFTWARE\EPLAN\EPLAN W3\Electric P8",
        "SOFTWARE\EPLAN\EPLAN W3\Platform",
        "SOFTWARE\EPLAN\EPLAN W3\Pro Panel",
        "SOFTWARE\EPLAN\EPLAN W3"
    )
    $valueNames = @("AppDir", "BinPath", "InstallPath", "Path", "ProgramPath", "ExePath", "Root", "Main")
    $subKeyHints = @("SystemStartUp", "SystemDirectories")

    foreach ($regRoot in $regRoots) {
        foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64, [Microsoft.Win32.RegistryView]::Registry32)) {
            try {
                $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, $view)
                $rootKey = $baseKey.OpenSubKey($regRoot)
                if (-not $rootKey) { continue }

                foreach ($vn in $valueNames) {
                    try { Add-W3uCandidate "$($rootKey.GetValue($vn))" } catch {}
                }

                # 遍历版本子键（按名称倒序优先最新版本）
                $subNames = @($rootKey.GetSubKeyNames() | Sort-Object -Descending)
                foreach ($subName in $subNames) {
                    try {
                        $subKey = $rootKey.OpenSubKey($subName)
                        if (-not $subKey) { continue }
                        foreach ($vn in $valueNames) {
                            try { Add-W3uCandidate "$($subKey.GetValue($vn))" } catch {}
                        }
                        foreach ($hintName in $subKeyHints) {
                            try {
                                $hintKey = $subKey.OpenSubKey($hintName)
                                if (-not $hintKey) { continue }
                                foreach ($vn in $valueNames) {
                                    try { Add-W3uCandidate "$($hintKey.GetValue($vn))" } catch {}
                                }
                            } catch {}
                        }
                    } catch {}
                }
            } catch {}
        }
    }

    # 1b. 读取 HKCU\SOFTWARE\EPLAN\Usage\LastStartedVariantDir（记录最近一次启动的 EPLAN 变体目录）
    try {
        $usageKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("SOFTWARE\EPLAN\Usage")
        if ($usageKey) {
            Add-W3uCandidate "$($usageKey.GetValue('LastStartedVariantDir'))"
        }
    } catch {}

    # 2. 动态扫描所有固定盘符下的标准 EPLAN 安装目录
    $drives = @(Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Root -match '^[A-Z]:\\$' } | Select-Object -ExpandProperty Root)
    if ($drives.Count -eq 0) { $drives = @("C:\") }
    $relPatterns = @(
        "Program Files\EPLAN\Electric P8\*\Bin\W3u.exe",
        "Program Files\EPLAN\Platform\*\Bin\W3u.exe",
        "Program Files (x86)\EPLAN\Electric P8\*\Bin\W3u.exe",
        "Program Files (x86)\EPLAN\Platform\*\Bin\W3u.exe",
        "EPLAN\Electric P8\*\Bin\W3u.exe",
        "EPLAN\Platform\*\Bin\W3u.exe"
    )
    foreach ($rootDir in $drives) {
        foreach ($rel in $relPatterns) {
            $glob = Join-Path $rootDir $rel
            try {
                $found = Get-Item -Path $glob -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
                foreach ($item in $found) {
                    Add-W3uCandidate $item.FullName
                }
            } catch {}
        }
    }

    $selected = if ($candidates.Count -gt 0) { $candidates[0] } else { "" }
    return [PSCustomObject]@{
        Found      = ($selected -ne "")
        Path       = $selected
        Candidates = @($candidates)
    }
}

function Invoke-W3uCommand([string]$exePath, [string[]]$argList, [int]$timeoutSec) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exePath
    $escapedArgs = @()
    foreach ($a in $argList) {
        if ($a -match '[\s"]') {
            $escapedArgs += ('"' + ($a -replace '"', '\"') + '"')
        } else {
            $escapedArgs += $a
        }
    }
    $psi.Arguments = ($escapedArgs -join " ")
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    $started = $proc.Start()
    if (-not $started) {
        return [PSCustomObject]@{
            Success  = $false
            ExitCode = -1
            StdOut   = ""
            StdErr   = "无法启动 W3u.exe 进程"
            Command  = "`"$exePath`" $($psi.Arguments)"
        }
    }
    # 异步读取 StdOut/StdErr 防止子进程输出超过管道缓冲区触发 WaitForExit 死锁
    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()
    $finished = $proc.WaitForExit($timeoutSec * 1000)
    if (-not $finished) {
        try { $proc.Kill() } catch {}
        return [PSCustomObject]@{
            Success  = $false
            ExitCode = -2
            StdOut   = ""
            StdErr   = "W3u.exe 执行超时（超过 ${timeoutSec}s）并已终止"
            Command  = "`"$exePath`" $($psi.Arguments)"
        }
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    return [PSCustomObject]@{
        Success  = ($proc.ExitCode -eq 0)
        ExitCode = $proc.ExitCode
        StdOut   = $stdout.Trim()
        StdErr   = $stderr.Trim()
        Command  = "`"$exePath`" $($psi.Arguments)"
    }
}

$allowedActions = @("inspect-eplan", "export-all", "export-aml", "export-tables", "export-pdf")
if ($allowedActions -notcontains $Action) {
    Write-JsonResult ([PSCustomObject]@{
        Success        = $false
        Action         = $Action
        Error          = "不支持的 Action: '$Action'"
        ActionableHint = "仅支持: $($allowedActions -join ', ')"
    }) 1
}

$resolved = Resolve-EplanW3uPath $W3uPath

if ($Action -eq "inspect-eplan") {
    $ver = ""
    if ($resolved.Found) {
        try {
            $ver = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($resolved.Path).FileVersion
        } catch {}
    }
    Write-JsonResult ([PSCustomObject]@{
        Success        = $resolved.Found
        Action         = "inspect-eplan"
        W3uFound       = $resolved.Found
        W3uPath        = $resolved.Path
        Version        = $ver
        Candidates     = $resolved.Candidates
        ActionableHint = if ($resolved.Found) { "已定位 EPLAN W3u.exe，可执行静默导出。" } else { "未在注册表 HKLM\SOFTWARE\EPLAN\EPLAN W3\Platform 或常见路径找到 W3u.exe；请安装 EPLAN Electric P8 或通过 -W3uPath 显式指定，也可直接在 EPLAN GUI 手动导出 .aml/.xlsx/.pdf 后投喂至工作流。" }
    }) 0
}

# 导出类动作参数校验
if ([string]::IsNullOrWhiteSpace($EplanProjectPath) -or -not (Test-Path $EplanProjectPath)) {
    Write-JsonResult ([PSCustomObject]@{
        Success        = $false
        Action         = $Action
        Error          = "EplanProjectPath 缺失或项目文件不存在: '$EplanProjectPath'"
        ActionableHint = "请传入有效的 EPLAN 工程路径（.elk 或 .elp）。"
    }) 1
}

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    Write-JsonResult ([PSCustomObject]@{
        Success        = $false
        Action         = $Action
        Error          = "OutputDir 参数不能为空"
        ActionableHint = "请指定用于存放导出资产的输出目录（如 00_输入图纸）。"
    }) 1
}

if (-not $resolved.Found) {
    Write-JsonResult ([PSCustomObject]@{
        Success        = $false
        Action         = $Action
        Error          = "未在本机定位到 EPLAN W3u.exe，无法执行静默导出。"
        Candidates     = $resolved.Candidates
        ActionableHint = "请确认已安装 EPLAN Electric P8 并通过 -W3uPath 指定 W3u.exe，或改用 EPLAN GUI 手动导出（文件->导出 PLC 数据 / 报表->标签 / 导出->PDF）。"
    }) 1
}

$absProj = (Resolve-Path $EplanProjectPath).Path
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}
$absOutDir = (Resolve-Path $OutputDir).Path
$projBase = [System.IO.Path]::GetFileNameWithoutExtension($absProj)
if ([string]::IsNullOrWhiteSpace($projBase)) { $projBase = "EPLAN_Project" }

$steps = @()
$exportedFiles = @{}
$allOk = $true

function Run-ExportStep([string]$stepName, [string[]]$w3uArgs, [string]$targetFile) {
    $res = Invoke-W3uCommand $resolved.Path $w3uArgs $TimeoutSeconds
    $fileExists = Test-Path $targetFile
    $stepSuccess = ($res.Success -and $fileExists)
    return [PSCustomObject]@{
        Step       = $stepName
        Success    = $stepSuccess
        ExitCode   = $res.ExitCode
        TargetFile = $targetFile
        FileExists = $fileExists
        Command    = $res.Command
        StdErr     = $res.StdErr
    }
}

if ($Action -eq "export-all" -or $Action -eq "export-aml") {
    $amlOut = Join-Path $absOutDir "${projBase}_HW.aml"
    $amlArgs = @(
        "/NoSplash", "/Auto", "/Quiet", "/Frame:0",
        "XPlcExportAction",
        "/PROJECTNAME:$absProj",
        "/EXPORTFILE:$amlOut",
        "/CONVERTERID:$ConverterId"
    )
    $s = Run-ExportStep "export-aml" $amlArgs $amlOut
    $steps += $s
    $exportedFiles["aml"] = $amlOut
    if (-not $s.Success) { $allOk = $false }
}

if ($Action -eq "export-all" -or $Action -eq "export-tables") {
    $plcOut = Join-Path $absOutDir "${projBase}_PLC_IO.xlsx"
    $plcArgs = @(
        "/NoSplash", "/Auto", "/Quiet", "/Frame:0",
        "label",
        "/PROJECTNAME:$absProj",
        "/CONFIGSCHEME:$PlcScheme",
        "/DESTINATIONFILE:$plcOut"
    )
    $sPlc = Run-ExportStep "export-plc-table" $plcArgs $plcOut
    $steps += $sPlc
    $exportedFiles["plc_io_xlsx"] = $plcOut
    if (-not $sPlc.Success) { $allOk = $false }

    $bomOut = Join-Path $absOutDir "${projBase}_BOM.xlsx"
    $bomArgs = @(
        "/NoSplash", "/Auto", "/Quiet", "/Frame:0",
        "label",
        "/PROJECTNAME:$absProj",
        "/CONFIGSCHEME:$BomScheme",
        "/DESTINATIONFILE:$bomOut"
    )
    $sBom = Run-ExportStep "export-bom-table" $bomArgs $bomOut
    $steps += $sBom
    $exportedFiles["bom_xlsx"] = $bomOut
    if (-not $sBom.Success) { $allOk = $false }
}

if ($Action -eq "export-all" -or $Action -eq "export-pdf") {
    $pdfOut = Join-Path $absOutDir "${projBase}_原理图.pdf"
    $pdfArgs = @(
        "/NoSplash", "/Auto", "/Quiet", "/Frame:0",
        "export",
        "/TYPE:PDFPROJECTSCHEME",
        "/PROJECTNAME:$absProj",
        "/EXPORTFILE:$pdfOut"
    )
    $sPdf = Run-ExportStep "export-pdf" $pdfArgs $pdfOut
    $steps += $sPdf
    $exportedFiles["schematic_pdf"] = $pdfOut
    if (-not $sPdf.Success) { $allOk = $false }
}

$hint = if ($allOk) {
    "EPLAN 静默导出全部完成，可直接传入 eplan_parser.py 与 cax_hardware_builder.py 进行解析预检。"
} else {
    "部分或全部 EPLAN 导出步骤未产出目标文件；请核对 EPLAN 授权、标签导出方案名（PlcScheme/BomScheme）或确认 EPLAN 版本支持 XPlcExportAction / plcservice，也可在 EPLAN GUI 手动导出后继续流水线。"
}

Write-JsonResult ([PSCustomObject]@{
    Success        = $allOk
    Action         = $Action
    W3uPath        = $resolved.Path
    EplanProject   = $absProj
    OutputDir      = $absOutDir
    ExportedFiles  = $exportedFiles
    Steps          = $steps
    ActionableHint = $hint
}) $(if ($allOk) { 0 } else { 1 })
