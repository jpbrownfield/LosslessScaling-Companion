param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Subject = 'CN=LS Companion, O=LS Companion'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$envPath = Join-Path $root '.env.release-signing'
$keyRoot = Join-Path $root '.signing'
if ((Test-Path -LiteralPath $envPath) -or ((Test-Path -LiteralPath $keyRoot) -and @(Get-ChildItem -LiteralPath $keyRoot -Force).Count -gt 0)) {
    throw 'Signing material already exists. Back it up and explicitly rotate it instead of overwriting the release identity.'
}

function Protect-LocalSecret([string]$Target, [bool]$Directory) {
    $identity = '*' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $rights = if ($Directory) { ':(OI)(CI)F' } else { ':F' }
    # icacls changes only the DACL; Set-Acl can also request unavailable audit
    # privileges on otherwise writable Windows profiles.
    & icacls.exe $Target /inheritance:r /grant:r ($identity + $rights) ('*S-1-5-18' + $rights) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect local signing material: $Target" }
}

$null = New-Item -ItemType Directory -Path $keyRoot -Force
Protect-LocalSecret $keyRoot $true
$random = New-Object byte[] 48
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($random)
$password = [Convert]::ToBase64String($random)
$rsa = [Security.Cryptography.RSACng]::new(3072)
$certificate = $null
try {
    $request = [Security.Cryptography.X509Certificates.CertificateRequest]::new($Subject, $rsa,
        [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true))
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new(
        [Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature, $true))
    $usage = [Security.Cryptography.OidCollection]::new()
    $null = $usage.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.3'))
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($usage, $true))
    $certificate = $request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddMinutes(-5), [DateTimeOffset]::UtcNow.AddYears(3))
    $pfx = $certificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Pfx, $password)
    $public = $certificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Cert)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $fingerprint = ([BitConverter]::ToString($sha.ComputeHash($public))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    [IO.File]::WriteAllBytes((Join-Path $keyRoot 'LSCompanion.pfx'), $pfx)
    [IO.File]::WriteAllBytes((Join-Path $keyRoot 'LSCompanion.cer'), $public)
    # Restrict the env file before writing any secret into it. No private key is
    # imported into Windows trust stores, and no secret is printed to the console.
    [IO.File]::WriteAllText($envPath, '')
    Protect-LocalSecret $envPath $false
    $values = @(
        "WINDOWS_SIGNING_PFX_BASE64=$([Convert]::ToBase64String($pfx))",
        "WINDOWS_SIGNING_PFX_PASSWORD=$password",
        "WINDOWS_SIGNING_CERT_SHA256=$fingerprint"
    )
    [IO.File]::WriteAllLines($envPath, $values, [Text.UTF8Encoding]::new($false))
    Write-Host "Generated a three-year RSA-3072 code-signing certificate. Credentials: $envPath"
    Write-Host "Public certificate: $(Join-Path $keyRoot 'LSCompanion.cer')"
    Write-Host "Certificate SHA-256: $fingerprint"
} finally {
    if ($certificate) { $certificate.Dispose() }
    $rsa.Dispose()
    $rng.Dispose()
    [Array]::Clear($random, 0, $random.Length)
}
