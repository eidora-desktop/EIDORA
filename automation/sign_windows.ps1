# Used both by the build job and Inno Setup for its embedded uninstaller.
param([Parameter(Mandatory)][string]$Path)
$ErrorActionPreference = 'Stop'
if (-not $env:WINDOWS_CERTIFICATE_PFX_BASE64 -or -not $env:WINDOWS_CERTIFICATE_PASSWORD) {
    throw 'Windows signing credentials are required.'
}
$sdkRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
$signTool = Get-ChildItem "$sdkRoot\*\x64\signtool.exe" |
    Sort-Object { [version]$_.Directory.Parent.Name } -Descending |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $signTool) { throw 'Windows SDK signtool.exe was not found.' }
$certificate = Join-Path ([IO.Path]::GetTempPath()) ("eidora-signing-" + [guid]::NewGuid() + '.pfx')
try {
    [IO.File]::WriteAllBytes($certificate, [Convert]::FromBase64String($env:WINDOWS_CERTIFICATE_PFX_BASE64))
    & $signTool sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com /f $certificate /p $env:WINDOWS_CERTIFICATE_PASSWORD $Path
    if ($LASTEXITCODE -ne 0) { throw "Signing failed: $Path" }
    & $signTool verify /pa /all /tw /v $Path
    if ($LASTEXITCODE -ne 0) { throw "Signature or timestamp verification failed: $Path" }
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or -not $signature.TimeStamperCertificate) {
        throw "Untrusted or untimestamped signature: $Path"
    }
} finally {
    Remove-Item -LiteralPath $certificate -Force -ErrorAction SilentlyContinue
}
