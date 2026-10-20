# Run in an interactive, NON-ELEVATED Windows 10/11 test session after trusting
# the public test certificate. Leaves the installed package for visible QA.
param(
    [Parameter(Mandatory)][string]$SubmissionPackage,
    [Parameter(Mandatory)][string]$TestPackage,
    [Parameter(Mandatory)][string]$ReportDirectory,
    [int]$TimeoutSeconds = 600
)
$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run installed-app validation in a non-elevated desktop session.'
}
if ($env:EIDORA_WORKSPACE_ROOT) { throw 'Clear EIDORA_WORKSPACE_ROOT to validate the default Store workspace.' }
$submission = (Resolve-Path -LiteralPath $SubmissionPackage).Path
$testCopy = (Resolve-Path -LiteralPath $TestPackage).Path
if ($submission -eq $testCopy) { throw 'The submission and test packages must be separate files.' }
$reportRoot = [IO.Path]::GetFullPath($ReportDirectory)
if (Test-Path -LiteralPath $reportRoot) { throw 'Use a new report directory to avoid stale validation results.' }
New-Item -ItemType Directory -Path $reportRoot | Out-Null
Start-Transcript -Path (Join-Path $reportRoot 'installation.log') | Out-Null
try {
    $signTool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" |
        Sort-Object { [version]$_.Directory.Parent.Name } -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $signTool) { throw 'Windows SDK signtool.exe is required.' }
    & $signTool verify /pa /v $testCopy
    if ($LASTEXITCODE -ne 0) { throw 'Test package integrity or trust verification failed.' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $original = [IO.Compression.ZipFile]::OpenRead($submission)
    $signed = [IO.Compression.ZipFile]::OpenRead($testCopy)
    try {
        if ($original.GetEntry('AppxSignature.p7x')) { throw 'Submission package must be unsigned.' }
        # The validated signature protects the block map, which covers the payload.
        foreach ($name in @('AppxBlockMap.xml', 'AppxManifest.xml')) {
            $hashes = foreach ($archive in @($original, $signed)) {
                $stream = $archive.GetEntry($name).Open()
                $hasher = [Security.Cryptography.SHA256]::Create()
                try { [Convert]::ToBase64String($hasher.ComputeHash($stream)) }
                finally { $hasher.Dispose(); $stream.Dispose() }
            }
            if ($hashes[0] -ne $hashes[1]) { throw "Test copy does not match submission: $name" }
        }
        $reader = [IO.StreamReader]::new($original.GetEntry('AppxManifest.xml').Open())
        try { [xml]$manifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
    } finally { $original.Dispose(); $signed.Dispose() }

    Add-AppxPackage -Path $testCopy
    $installed = @(Get-AppxPackage -Name $manifest.Package.Identity.Name |
        Where-Object { $_.Publisher -eq $manifest.Package.Identity.Publisher -and
            $_.Version.ToString() -eq $manifest.Package.Identity.Version })
    if ($installed.Count -ne 1) { throw 'The expected package identity/version was not installed.' }
    $package = $installed[0]
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace EidoraMsix {
    [ComImport, Guid("2e941141-7f97-4756-ba1d-9decde894a3d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivationManager {
        [PreserveSig] int ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string appId,
            [MarshalAs(UnmanagedType.LPWStr)] string arguments, uint options, out uint processId);
        [PreserveSig] int ActivateForFile(IntPtr appId, IntPtr items, IntPtr verb, out uint processId);
        [PreserveSig] int ActivateForProtocol(IntPtr appId, IntPtr items, out uint processId);
    }
    public static class Launcher {
        public static uint Launch(string appId, string arguments) {
            var type = Type.GetTypeFromCLSID(new Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C"));
            var manager = (IActivationManager)Activator.CreateInstance(type);
            try {
                uint processId;
                Marshal.ThrowExceptionForHR(manager.ActivateApplication(appId, arguments, 0, out processId));
                return processId;
            } finally { Marshal.ReleaseComObject(manager); }
        }
    }
}
'@
    $selfTestReport = Join-Path $reportRoot 'release-self-test.json'
    $appId = $package.PackageFamilyName + '!' + $manifest.Package.Applications.Application.Id
    $applicationProcessId = [EidoraMsix.Launcher]::Launch($appId, "--release-self-test `"$selfTestReport`" --require-package-identity")
    $process = Get-Process -Id $applicationProcessId -ErrorAction SilentlyContinue
    if ($process) {
        $null = $process.Handle  # Retain an exit-code-capable handle before termination.
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            & taskkill /PID $applicationProcessId /T /F
            throw 'Installed release self-test timed out; inspect checkpoint and trace logs.'
        }
        if ($process.ExitCode -ne 0) { throw "Installed release self-test exited with $($process.ExitCode)." }
    }
    $result = Get-Content -LiteralPath $selfTestReport -Raw | ConvertFrom-Json
    if ($result.ok -ne $true -or $result.windows_package_full_name -ne $package.PackageFullName) {
        throw 'Installed self-test failed or ran without the expected package identity.'
    }
    $expectedWorkspace = Join-Path $env:USERPROFILE 'EIDORA\workspace_projects'
    if ([IO.Path]::GetFullPath($result.default_workspace_root) -ne $expectedWorkspace) {
        throw 'The Store workspace is not at the durable user-profile location.'
    }
    $os = Get-CimInstance Win32_OperatingSystem
    @{
        package_full_name = $package.PackageFullName
        submission_sha256 = (Get-FileHash -LiteralPath $submission -Algorithm SHA256).Hash
        windows_caption = $os.Caption; windows_version = $os.Version
        installed_self_test = 'passed'; visible_gpu_tests = 'not_run'
        upgrade_uninstall_tests = 'not_run'; certification_kit = 'not_run'
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $reportRoot 'installed-validation.json') -Encoding UTF8
    Write-Output 'Installed smoke test passed. Visible QA, upgrade/uninstall tests, and WACK remain required.'
} finally { Stop-Transcript | Out-Null }
