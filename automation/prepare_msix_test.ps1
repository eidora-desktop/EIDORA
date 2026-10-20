# Creates a signed COPY and public test certificate. Never exports the private key.
param(
    [Parameter(Mandatory)][string]$Package,
    [Parameter(Mandatory)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$source = (Resolve-Path -LiteralPath $Package).Path
$sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
$archive = [IO.Compression.ZipFile]::OpenRead($source)
try {
    if ($archive.GetEntry('AppxSignature.p7x')) { throw 'Input must be the unsigned Store submission package.' }
    $reader = [IO.StreamReader]::new($archive.GetEntry('AppxManifest.xml').Open())
    try { [xml]$manifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
} finally { $archive.Dispose() }
$output = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $output | Out-Null
$copy = Join-Path $output 'EIDORA-test-only.msix'
$publicCertificate = Join-Path $output 'EIDORA-test-only.cer'
if (Test-Path -LiteralPath $copy) { throw "Use a new test output directory: $copy already exists." }
$signTool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" |
    Sort-Object { [version]$_.Directory.Parent.Name } -Descending |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $signTool) { throw 'Windows SDK signtool.exe is required.' }
$certificate = New-SelfSignedCertificate -Type Custom -Subject $manifest.Package.Identity.Publisher `
    -KeyUsage DigitalSignature -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 `
    -KeyExportPolicy NonExportable -CertStoreLocation 'Cert:\CurrentUser\My' `
    -NotAfter (Get-Date).AddDays(30) `
    -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3', '2.5.29.19={text}')
try {
    Copy-Item -LiteralPath $source -Destination $copy
    & $signTool sign /fd SHA256 /s My /sha1 $certificate.Thumbprint $copy
    if ($LASTEXITCODE -ne 0) { throw 'Test package signing failed.' }
    Export-Certificate -Cert $certificate -FilePath $publicCertificate | Out-Null
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $sourceHash) {
        throw 'Submission package changed during test preparation.'
    }
    Write-Output "Test package: $copy"
    Write-Output "Public certificate: $publicCertificate"
    Write-Output "Trust only on a disposable test machine, then run validate_msix.ps1 as a non-elevated user."
    Write-Output "Certificate thumbprint (remove from TrustedPeople after testing): $($certificate.Thumbprint)"
} catch {
    Remove-Item -LiteralPath $copy -Force -ErrorAction SilentlyContinue
    throw
} finally {
    Remove-Item -LiteralPath $certificate.PSPath -Force
}
