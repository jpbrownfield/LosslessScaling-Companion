[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string] $LosslessScalingExe,

    [ValidateSet('Inventory', 'Launch', 'Full')]
    [string] $Mode = 'Inventory',

    [string] $SettingsXml = (Join-Path $env:LOCALAPPDATA 'Lossless Scaling\Settings.xml'),
    [string] $HotkeyA = 'ctrl+alt+f23',
    [string] $HotkeyB = 'ctrl+alt+f24',
    [int] $TargetAProcessId = 0,
    [int] $TargetBProcessId = 0,
    [string] $InstanceAOverlayDirectory,
    [string] $InstanceBOverlayDirectory,
    [int] $StartupSeconds = 4,
    [int] $ObservationSeconds = 5,
    [string] $PythonCommand = 'python',
    [switch] $KeepRunning,
    [switch] $KeepArtifacts
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$windowProbe = Join-Path $PSScriptRoot 'ls_window_target.py'
$sourceExe = (Resolve-Path -LiteralPath $LosslessScalingExe).Path
$sourceDirectory = Split-Path -Parent $sourceExe
$sourceExeName = Split-Path -Leaf $sourceExe
$runId = [guid]::NewGuid().ToString('N')
$probeRoot = Join-Path ([System.IO.Path]::GetTempPath()) "LosslessScalingMultiInstanceProbe-$runId"
$reportDirectory = Join-Path $repoRoot 'test-results'
$reportPath = Join-Path $reportDirectory "ls-multi-instance-$runId.json"
$report = [ordered]@{
    runId = $runId
    startedAt = (Get-Date).ToString('o')
    mode = $Mode
    sourceExe = $sourceExe
    probeRoot = $probeRoot
    tests = [ordered]@{}
    warnings = @()
}
$spawned = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()

function Get-FileEvidence([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{
        path = $item.FullName
        length = $item.Length
        lastWriteTimeUtc = $item.LastWriteTimeUtc.ToString('o')
        sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    }
}

function Convert-HotkeyToXml([xml] $Document, [string] $Hotkey) {
    $parts = @($Hotkey.Split('+') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
    if ($parts.Count -lt 1) { throw "Invalid hotkey: $Hotkey" }
    $key = $parts[-1].ToUpperInvariant()
    $modifierMap = @{ ctrl = 'Control'; control = 'Control'; alt = 'Alt'; shift = 'Shift'; win = 'Windows' }
    $modifiers = @()
    if ($parts.Count -gt 1) {
        foreach ($part in $parts[0..($parts.Count - 2)]) {
            if (-not $modifierMap.ContainsKey($part)) { throw "Unsupported modifier '$part' in $Hotkey" }
            $modifiers += $modifierMap[$part]
        }
    }
    if (-not $Document.Settings.Hotkey) {
        $node = $Document.CreateElement('Hotkey')
        [void]$Document.Settings.PrependChild($node)
    }
    $Document.Settings.Hotkey = $key
    if (-not $Document.Settings.HotkeyModifierKeys) {
        $node = $Document.CreateElement('HotkeyModifierKeys')
        [void]$Document.Settings.AppendChild($node)
    }
    $Document.Settings.HotkeyModifierKeys = ($modifiers -join ' ')

    # Keep the probe controlled: preserve the process/environment we launched
    # and prevent native auto-scaling from racing the explicit hotkey tests.
    if ($Document.Settings.StartAsAdmin) {
        $Document.Settings.StartAsAdmin = 'false'
    }
    if ($Document.Settings.StartAtWindowsStartup) {
        $Document.Settings.StartAtWindowsStartup = 'false'
    }
    foreach ($profile in @($Document.Settings.GameProfiles.Profile)) {
        if ($profile.AutoScale) {
            $profile.AutoScale = 'false'
        }
    }
}

function New-InstanceLayout(
    [string] $Name,
    [string] $Hotkey,
    [string] $OverlayDirectory
) {
    $root = Join-Path $probeRoot $Name
    $appDirectory = Join-Path $root 'app'
    $localAppData = Join-Path $root 'LocalAppData'
    $roamingAppData = Join-Path $root 'RoamingAppData'
    $isolatedSettingsDirectory = Join-Path $localAppData 'Lossless Scaling'
    New-Item -ItemType Directory -Path $appDirectory, $isolatedSettingsDirectory, $roamingAppData -Force | Out-Null
    Get-ChildItem -LiteralPath $sourceDirectory -Force |
        Copy-Item -Destination $appDirectory -Recurse -Force
    if ($OverlayDirectory) {
        $resolvedOverlay = (Resolve-Path -LiteralPath $OverlayDirectory).Path
        Get-ChildItem -LiteralPath $resolvedOverlay -Force |
            Copy-Item -Destination $appDirectory -Recurse -Force
    }
    $isolatedSettings = Join-Path $isolatedSettingsDirectory 'Settings.xml'
    [xml]$document = Get-Content -LiteralPath $SettingsXml -Raw
    Convert-HotkeyToXml $document $Hotkey
    $document.Save($isolatedSettings)
    return [ordered]@{
        name = $Name
        root = $root
        appDirectory = $appDirectory
        exe = Join-Path $appDirectory $sourceExeName
        localAppData = $localAppData
        roamingAppData = $roamingAppData
        settings = $isolatedSettings
        hotkey = $Hotkey
    }
}

function Start-IsolatedInstance($Layout) {
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Layout.exe
    $startInfo.WorkingDirectory = $Layout.appDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.EnvironmentVariables['LOCALAPPDATA'] = $Layout.localAppData
    $startInfo.EnvironmentVariables['APPDATA'] = $Layout.roamingAppData
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $spawned.Add($process)
    return $process
}

function Invoke-WindowProbe([string[]] $Arguments) {
    $output = & $PythonCommand $windowProbe @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    $text = $output -join [Environment]::NewLine
    try { $data = $text | ConvertFrom-Json } catch { $data = [ordered]@{ rawOutput = $text } }
    return [ordered]@{ exitCode = $exitCode; data = $data }
}

function Stop-OwnedProcess([System.Diagnostics.Process] $Process) {
    try {
        if ($Process.HasExited) { return }
        if ($Process.MainWindowHandle -ne 0) {
            [void]$Process.CloseMainWindow()
            if ($Process.WaitForExit(2000)) { return }
        }
        $Process.Kill()
        [void]$Process.WaitForExit(3000)
    } catch {
        $report.warnings += "Could not stop probe process $($Process.Id): $($_.Exception.Message)"
    }
}

try {
    $report.tests.sourceDirectory = [ordered]@{
        passed = (Test-Path -LiteralPath $sourceExe -PathType Leaf)
        executable = Get-FileEvidence $sourceExe
        fileCount = @(
            Get-ChildItem -LiteralPath $sourceDirectory -Recurse -File -ErrorAction SilentlyContinue
        ).Count
    }
    $report.tests.sharedSettingsBefore = Get-FileEvidence $SettingsXml

    if ($Mode -eq 'Inventory') {
        $report.tests.runningInstances = @(
            Get-Process -Name 'LosslessScaling' -ErrorAction SilentlyContinue |
                Select-Object Id, Path, MainWindowTitle, StartTime
        )
        $report.tests.visibleWindows = (Invoke-WindowProbe @('--list'))
    } else {
        if (-not (Test-Path -LiteralPath $SettingsXml -PathType Leaf)) {
            throw "Settings XML not found: $SettingsXml"
        }
        New-Item -ItemType Directory -Path $probeRoot -Force | Out-Null
        $layoutA = New-InstanceLayout 'instance-a' $HotkeyA $InstanceAOverlayDirectory
        $layoutB = New-InstanceLayout 'instance-b' $HotkeyB $InstanceBOverlayDirectory
        $report.tests.layouts = @($layoutA, $layoutB)

        $processA = Start-IsolatedInstance $layoutA
        Start-Sleep -Seconds $StartupSeconds
        $processB = Start-IsolatedInstance $layoutB
        Start-Sleep -Seconds $StartupSeconds
        $processA.Refresh()
        $processB.Refresh()
        $report.tests.twoPersistentProcesses = [ordered]@{
            passed = (-not $processA.HasExited -and -not $processB.HasExited -and $processA.Id -ne $processB.Id)
            instanceA = [ordered]@{ processId = $processA.Id; hasExited = $processA.HasExited }
            instanceB = [ordered]@{ processId = $processB.Id; hasExited = $processB.HasExited }
        }
        $liveIds = @($processA, $processB) |
            Where-Object { -not $_.HasExited } |
            ForEach-Object { $_.Id }
        $report.tests.instanceWindowsBeforeTrigger = @(
            foreach ($processId in $liveIds) {
                Invoke-WindowProbe @('--list', '--process-id', [string]$processId)
            }
        )

        if ($Mode -eq 'Full') {
            if ($TargetAProcessId -le 0 -or $TargetBProcessId -le 0) {
                throw 'Full mode requires -TargetAProcessId and -TargetBProcessId.'
            }
            $report.tests.triggerA = Invoke-WindowProbe @(
                '--process-id', [string]$TargetAProcessId, '--hotkey', $HotkeyA
            )
            Start-Sleep -Seconds $ObservationSeconds
            $report.tests.instanceWindowsAfterA = @(
                foreach ($processId in $liveIds) {
                    Invoke-WindowProbe @('--list', '--process-id', [string]$processId)
                }
            )
            $report.tests.triggerB = Invoke-WindowProbe @(
                '--process-id', [string]$TargetBProcessId, '--hotkey', $HotkeyB
            )
            Start-Sleep -Seconds $ObservationSeconds
            $report.tests.instanceWindowsAfterB = @(
                foreach ($processId in $liveIds) {
                    Invoke-WindowProbe @('--list', '--process-id', [string]$processId)
                }
            )
        }

        $report.tests.isolatedSettingsAfter = @(
            Get-FileEvidence $layoutA.settings
            Get-FileEvidence $layoutB.settings
        )
        $report.tests.sharedSettingsAfter = Get-FileEvidence $SettingsXml
        if ($report.tests.sharedSettingsBefore.sha256 -ne $report.tests.sharedSettingsAfter.sha256) {
            $report.warnings += 'The real shared Settings.xml changed during the probe.'
        }
    }
} catch {
    $report.error = $_.Exception.Message
    $report.errorDetails = $_.ScriptStackTrace
} finally {
    if (-not $KeepRunning) {
        foreach ($process in $spawned) { Stop-OwnedProcess $process }
    }
    $report.finishedAt = (Get-Date).ToString('o')
    New-Item -ItemType Directory -Path $reportDirectory -Force | Out-Null
    $report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Output "Probe report: $reportPath"
    Get-Content -LiteralPath $reportPath
    if (Test-Path -LiteralPath $probeRoot) {
        if (-not $KeepArtifacts -and -not $KeepRunning) {
            $resolvedProbe = [System.IO.Path]::GetFullPath($probeRoot)
            $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
            if (-not $resolvedProbe.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to remove probe directory outside the temp root: $resolvedProbe"
            }
            Remove-Item -LiteralPath $resolvedProbe -Recurse -Force
            Write-Output 'Temporary cloned installations removed. Use -KeepArtifacts to retain them.'
        }
    }
}

if ($report.error) { exit 1 }
if ($report.tests.twoPersistentProcesses -and -not $report.tests.twoPersistentProcesses.passed) { exit 2 }
exit 0
