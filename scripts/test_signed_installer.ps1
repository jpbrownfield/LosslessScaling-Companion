param(
    [string]$Installer = 'dist/installer/LosslessCompanion-Setup-x64.exe'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$installDirectory = Join-Path $env:RUNNER_TEMP ('LSC-Signing-Test-' + [Guid]::NewGuid().ToString('N'))
$uninstaller = Join-Path $installDirectory 'unins000.exe'
$application = Join-Path $installDirectory 'LosslessCompanion.exe'
$tampered = Join-Path $env:RUNNER_TEMP ('LSC-Tampered-' + [Guid]::NewGuid().ToString('N') + '.exe')
try {
    & "$PSScriptRoot/sign_release.ps1" -Path $installerPath -VerifyOnly
    $setup = Start-Process -FilePath $installerPath -WindowStyle Hidden -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS',
        '/MERGETASKS="!startmenuicon,!desktopicon"', ('/DIR="' + $installDirectory + '"'))
    if ($setup.ExitCode -ne 0) { throw "Signed installer failed with exit code $($setup.ExitCode)" }
    & "$PSScriptRoot/sign_release.ps1" -Path @($application, $uninstaller) -VerifyOnly

    # Reject a changed executable even when it still contains the expected signer.
    $bytes = [IO.File]::ReadAllBytes($application)
    $bytes[64] = $bytes[64] -bxor 1
    [IO.File]::WriteAllBytes($tampered, $bytes)
    $rejected = $false
    try { & "$PSScriptRoot/sign_release.ps1" -Path $tampered -VerifyOnly }
    catch {
        if ($_.Exception.Message -notmatch 'WinVerifyTrust 0x80096010') { throw }
        $rejected = $true
        Write-Host 'Tampered executable correctly rejected (TRUST_E_BAD_DIGEST).'
    }
    if (-not $rejected) { throw 'Signature verification accepted a tampered executable.' }
} finally {
    if (Test-Path -LiteralPath $tampered) { Remove-Item -LiteralPath $tampered -Force }
    if (Test-Path -LiteralPath $uninstaller) {
        $uninstall = Start-Process -FilePath $uninstaller -WindowStyle Hidden -Wait -PassThru -ArgumentList @(
            '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
        if ($uninstall.ExitCode -ne 0) { throw "Signed uninstaller failed with exit code $($uninstall.ExitCode)" }
    }
}
if (Test-Path -LiteralPath $application) { throw 'Signed uninstaller left the application installed.' }
Write-Host 'Signed setup, installed application, uninstaller, and tamper rejection verified.'
