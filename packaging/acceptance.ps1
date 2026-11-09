param(
    [string]$BundleDirectory = $PSScriptRoot,
    [string]$ReportDirectory,
    [switch]$CleanMachine
)
$ErrorActionPreference = 'Stop'
if (-not $ReportDirectory) {
    $ReportDirectory = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('CodexImageCleanerAcceptance/' + [Guid]::NewGuid().ToString('N'))
}
$ReportDirectory = [IO.Path]::GetFullPath($ReportDirectory)
New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
$summaryPath = Join-Path $ReportDirectory 'acceptance-summary.json'
$runtimeReport = Join-Path $ReportDirectory 'runtime.json'
$savedEnvironment = @{}
foreach ($key in @('PATH','PYTHONPATH','PYTHONHOME','CODEX_IMAGE_CLEANER_DATA_DIR','TCL_LIBRARY','TK_LIBRARY')) {
    $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
$summary = [ordered]@{
    status = 'failed'
    clean_machine_user_confirmed = [bool]$CleanMachine
    os = [Environment]::OSVersion.VersionString
    timestamp_utc = [DateTime]::UtcNow.ToString('o')
    runtime = $null
}
try {
    $exe = Join-Path ([IO.Path]::GetFullPath($BundleDirectory)) 'CodexImageCleaner.exe'
    $infoPath = Join-Path ([IO.Path]::GetFullPath($BundleDirectory)) 'BUNDLE_INFO.json'
    $info = Get-Content -LiteralPath $infoPath -Raw -Encoding UTF8 | ConvertFrom-Json
    # Use the .NET primitive so the isolated PATH does not depend on cmdlet discovery.
    $hashAlgorithm = [Security.Cryptography.SHA256]::Create()
    $hashStream = [IO.File]::OpenRead($exe)
    try {
        $hash = [BitConverter]::ToString($hashAlgorithm.ComputeHash($hashStream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $hashStream.Dispose()
        $hashAlgorithm.Dispose()
    }
    if ($hash -ne $info.sha256) { throw 'Executable hash does not match the bundle manifest.' }
    $summary['application_version'] = $info.application_version
    $summary['executable_sha256'] = $hash
    $env:CODEX_IMAGE_CLEANER_DATA_DIR = Join-Path $ReportDirectory ('isolated-profile-' + [Guid]::NewGuid().ToString('N'))
    $env:PATH = "$env:SystemRoot/System32;$env:SystemRoot"
    $env:PYTHONPATH = ''
    $env:PYTHONHOME = Join-Path $ReportDirectory 'no-system-python'
    Remove-Item Env:TCL_LIBRARY -ErrorAction SilentlyContinue
    Remove-Item Env:TK_LIBRARY -ErrorAction SilentlyContinue
    Write-Host 'Running isolated runtime, synthetic cleanup/restore and instance checks...'
    $process = Start-Process -FilePath $exe -ArgumentList @('--self-test', ('"{0}"' -f $runtimeReport)) -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit(60000)) {
        $process.Kill()
        throw 'Self-test timed out.'
    }
    if ($process.ExitCode -ne 0) { throw 'Self-test process failed.' }
    $runtime = Get-Content -LiteralPath $runtimeReport -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($runtime.status -ne 'ok' -or -not $runtime.frozen) { throw 'Runtime self-test did not pass.' }
    $summary['runtime'] = [ordered]@{
        status = $runtime.status; version = $runtime.version; python = $runtime.python
        sqlite = $runtime.sqlite; storage_roundtrip = $runtime.storage_roundtrip
        layout_preservation = $runtime.layout_preservation; instance_ownership = $runtime.instance_ownership
        interface = $runtime.interface; web_assets = $runtime.web_assets
    }
    $summary['status'] = 'passed'
} catch {
    $summary['error_type'] = $_.Exception.GetType().Name
    Write-Host $_.Exception.Message
} finally {
    foreach ($key in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], 'Process')
    }
    $summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
}
Write-Host ('Result: ' + $summary.status)
Write-Host ('Summary: ' + $summaryPath)
if (-not $CleanMachine) { Write-Host 'Clean-machine acceptance is NOT confirmed by this run.' }
if ($summary.status -ne 'passed') { exit 1 }
