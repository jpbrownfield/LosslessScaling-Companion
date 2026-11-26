param(
    [string[]]$Path = @(),
    [string]$EnvFile = (Join-Path (Split-Path -Parent $PSScriptRoot) '.env.release-signing'),
    [switch]$CheckCredentials,
    [switch]$VerifyOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (Test-Path -LiteralPath $EnvFile) {
    foreach ($line in [IO.File]::ReadAllLines((Resolve-Path -LiteralPath $EnvFile).Path)) {
        if ($line -match '^(WINDOWS_SIGNING_(?:PFX_BASE64|PFX_PASSWORD|CERT_SHA256))=(.*)$') {
            if (-not [Environment]::GetEnvironmentVariable($Matches[1])) {
                [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
            }
        }
    }
}
foreach ($name in @('WINDOWS_SIGNING_PFX_BASE64', 'WINDOWS_SIGNING_PFX_PASSWORD', 'WINDOWS_SIGNING_CERT_SHA256')) {
    if (-not [Environment]::GetEnvironmentVariable($name)) { throw "Full releases require $name. Configure the signing env file or GitHub Actions secret." }
}
function Certificate-Digest($Certificate) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Certificate.RawData))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
if (-not ('LscReleaseSignature' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class LscReleaseSignature {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct FileInfo { public uint size; [MarshalAs(UnmanagedType.LPWStr)] public string path; public IntPtr file, subject; }
    [StructLayout(LayoutKind.Sequential)]
    struct TrustData {
        public uint size; public IntPtr policy, sip; public uint ui, revocation, choice;
        public IntPtr file; public uint action; public IntPtr state, url;
        public uint flags, context; public IntPtr settings;
    }
    [DllImport("wintrust.dll", ExactSpelling=true)]
    static extern uint WinVerifyTrust(IntPtr window, ref Guid action, ref TrustData data);
    public static uint Verify(string path) {
        var info = new FileInfo { size=(uint)Marshal.SizeOf(typeof(FileInfo)), path=path };
        var pointer=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FileInfo)));
        try {
            Marshal.StructureToPtr(info,pointer,false);
            var data=new TrustData {size=(uint)Marshal.SizeOf(typeof(TrustData)),ui=2,choice=1,file=pointer,flags=0x1010};
            var action=new Guid("00AAC56B-CD44-11d0-8CC2-00C04FC295EE");
            return WinVerifyTrust(new IntPtr(-1),ref action,ref data);
        } finally {Marshal.DestroyStructure(pointer,typeof(FileInfo));Marshal.FreeHGlobal(pointer);}
    }
}
'@
}
$certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new(
    [Convert]::FromBase64String($env:WINDOWS_SIGNING_PFX_BASE64), $env:WINDOWS_SIGNING_PFX_PASSWORD,
    [Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
try {
    $expected = $env:WINDOWS_SIGNING_CERT_SHA256.ToLowerInvariant()
    $eku = @($certificate.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.37' })
    if (-not $certificate.HasPrivateKey -or (Certificate-Digest $certificate) -ne $expected -or
        $certificate.NotAfter.ToUniversalTime() -le [DateTime]::UtcNow -or
        $certificate.NotBefore.ToUniversalTime() -gt [DateTime]::UtcNow -or
        $eku.Count -ne 1 -or '1.3.6.1.5.5.7.3.3' -notin @($eku[0].EnhancedKeyUsages | ForEach-Object { $_.Value })) {
        throw 'The signing certificate is expired, mismatched, or lacks a private key/code-signing usage.'
    }
    if ($CheckCredentials) { Write-Host 'Full-release signing credentials verified.'; return }
    if (-not $Path.Count) { throw 'Supply at least one file to sign or verify.' }
    foreach ($item in $Path) {
        $resolved = (Resolve-Path -LiteralPath $item).Path
        if (-not $VerifyOnly) {
            $null = Set-AuthenticodeSignature -LiteralPath $resolved -Certificate $certificate -HashAlgorithm SHA256 -IncludeChain All
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $resolved
        $selfSigned = $certificate.Subject -eq $certificate.Issuer
        $trustResult = [LscReleaseSignature]::Verify($resolved)
        # PowerShell can label CERT_E_UNTRUSTEDROOT as UnknownError. Accept that
        # exact WinVerifyTrust result only for our expected self-signed signer.
        # Hash mismatches and every other trust/signature error still fail.
        $untrustedRoot = [Convert]::ToUInt32('800B0109',16)
        if (-not $signature.SignerCertificate -or (Certificate-Digest $signature.SignerCertificate) -ne $expected -or
            ($trustResult -ne 0 -and -not ($selfSigned -and $trustResult -eq $untrustedRoot))) {
            throw ("Signature verification failed for {0} (WinVerifyTrust 0x{1:X8})" -f $resolved,$trustResult)
        }
        Write-Host "Verified Authenticode signature: $resolved (self-signed trust: $selfSigned)"
    }
} finally { $certificate.Dispose() }
