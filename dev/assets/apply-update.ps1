param([Parameter(Mandatory=$true)][string]$PlanPath)
$ErrorActionPreference = 'Stop'
$jobDirectory = Split-Path -LiteralPath (Resolve-Path -LiteralPath $PlanPath).Path
$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
$root = [IO.Path]::GetFullPath($plan.root)
$executable = [IO.Path]::GetFullPath($plan.executable)
$lock = $null
$parentProcess = $null
$committed = $false
$portableTouched = @()
$portableApplied = $false
$ownsLock = $false
$succeeded = $false
$restartAllowed = $false
$previousManifest = $null
function File-Digest([string]$path) {
    $stream = [IO.File]::OpenRead($path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}
function Assert-PlainPath([string]$path) {
    $cursor = $path
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $entry = Get-Item -LiteralPath $cursor -Force
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Linked deployment path' }
        }
        $next = Split-Path -Parent $cursor
        if ($next -eq $cursor) { break }
        $cursor = $next
    }
}
function Target-Path([string]$relative) {
    $target = [IO.Path]::GetFullPath((Join-Path $root $relative))
    if (-not $target.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Deployment path escaped' }
    Assert-PlainPath $target
    return $target
}
function Assert-InstalledFiles([string]$manifestDigest) {
    $manifest = Target-Path 'installed-files.txt'
    if ($manifestDigest -and (File-Digest $manifest) -ne $manifestDigest) {
        throw 'Previous installation manifest changed'
    }
    $seen = @{}
    foreach ($row in Get-Content -LiteralPath $manifest -Encoding UTF8) {
        if ($row -notmatch '^([a-f0-9]{64})  (.+)$') { throw 'Invalid installation manifest' }
        $expected = $Matches[1]
        $relative = $Matches[2]
        $path = Target-Path $relative
        if ($seen.ContainsKey($path) -or (File-Digest $path) -ne $expected) {
            throw ('Installation file mismatch: ' + $relative)
        }
        $seen[$path] = $true
    }
    if (-not $seen.ContainsKey($executable) -or
        -not $seen.ContainsKey((Target-Path 'DISTRIBUTION.json'))) { throw 'Incomplete installation manifest' }
}
try {
    # Reset the inherited frozen application's DLL search path in this helper
    # only. The active audio process retains its own loader environment.
    Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class UpdateLoader { [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool SetDllDirectory(string path); }'
    if (-not [UpdateLoader]::SetDllDirectory($null)) { throw 'Cannot reset updater DLL search path' }
    if ($plan.schema -ne 1 -or $plan.mode -notin @('installed','portable') -or
        $executable -ne (Join-Path $root 'ABMSynth.exe')) { throw 'Invalid update plan' }
    Assert-PlainPath $root
    Assert-PlainPath $plan.package
    $parentProcess = [Diagnostics.Process]::GetProcessById([int]$plan.parent)
    if ($parentProcess.MainModule.FileName -ne $plan.parentExecutable) { throw 'Parent identity changed' }
    $null = $parentProcess.Handle # Keep the exact process object alive before acknowledging.
    [IO.File]::WriteAllText((Join-Path $jobDirectory 'ready'), 'ready')
    while (-not (Test-Path -LiteralPath (Join-Path $jobDirectory 'commit'))) {
        if ((Test-Path -LiteralPath (Join-Path $jobDirectory 'cancel')) -or $parentProcess.HasExited) { exit 0 }
        Start-Sleep -Milliseconds 100
    }
    $committed = $true
    if (-not $parentProcess.WaitForExit(30000)) { throw 'Previous process did not exit' }
    $created = $false
    $lock = [Threading.Mutex]::new($true, 'Global\ABMSynth.Update.v1', [ref]$created)
    if (-not $created) { throw 'Another update is active' }
    $ownsLock = $true
    $running = $null
    try {
        $running = [Threading.Mutex]::OpenExisting('Global\ABMSynth.SingleApplication.v1')
        if (-not $running.WaitOne(0)) { throw 'ABMSynth was reopened' }
        $running.ReleaseMutex()
    } catch [Threading.WaitHandleCannotBeOpenedException] {
        # The old instance released its last handle normally.
    } finally { if ($running) { $running.Dispose() } }
    if ((File-Digest $plan.package) -ne $plan.sha256) { throw 'Package changed' }
    if ($plan.mode -eq 'installed') {
        $previousManifest = File-Digest (Target-Path 'installed-files.txt')
        $arguments = @('/SILENT','/SP-','/NORESTART','/NOCLOSEAPPLICATIONS','/NOFORCECLOSEAPPLICATIONS',
                       ('/DIR="' + $root + '"'),('/LOG="' + (Join-Path $jobDirectory 'setup.log') + '"'))
        $setup = Start-Process -FilePath $plan.package -ArgumentList $arguments -Wait -PassThru
        if ($setup.ExitCode -notin @(0,3010)) { throw ('Setup failed: ' + $setup.ExitCode) }
        Assert-InstalledFiles ''
        $distribution = Get-Content -LiteralPath (Target-Path 'DISTRIBUTION.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($distribution.mode -ne 'installed' -or $distribution.version -ne $plan.version) {
            throw 'Installed version does not match the update'
        }
    } else {
        # Validate every destination and every staged file before the first write.
        foreach ($row in $plan.files) {
            $null = Target-Path $row.path
            $source = Join-Path (Join-Path $jobDirectory 'payload') $row.path
            Assert-PlainPath $source
            if ((File-Digest $source) -ne $row.sha256) { throw 'Staged file changed' }
        }
        foreach ($row in $plan.files) {
            $target = Target-Path $row.path
            $backup = Join-Path (Join-Path $jobDirectory 'backup') $row.path
            $existed = Test-Path -LiteralPath $target
            if ($existed) {
                $null = New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force
                Copy-Item -LiteralPath $target -Destination $backup
            }
            $portableTouched += @{target=$target;backup=$backup;existed=$existed}
            $null = New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force
            Copy-Item -LiteralPath (Join-Path (Join-Path $jobDirectory 'payload') $row.path) -Destination $target -Force
        }
        $portableApplied = $true
    }
    $restartAllowed = $true
    [IO.File]::WriteAllText((Join-Path $jobDirectory 'result.json'), '{"passed":true}')
    $succeeded = $true
} catch {
    $failure = $_.Exception.Message
    $rollbackOK = $true
    if ($plan.mode -eq 'installed') {
        $rollbackOK = $false
        if ($previousManifest) {
            try {
                Assert-InstalledFiles $previousManifest
                $rollbackOK = $true
            } catch {
                $failure += '; ' + $_.Exception.Message
            }
        }
    } elseif (-not $portableApplied) {
        for ($i = $portableTouched.Count - 1; $i -ge 0; $i--) {
            $row = $portableTouched[$i]
            try {
                if ($row.existed) { Copy-Item -LiteralPath $row.backup -Destination $row.target -Force }
                elseif (Test-Path -LiteralPath $row.target) { Remove-Item -LiteralPath $row.target -Force }
            } catch { $rollbackOK = $false }
        }
    }
    @{passed=$false;error=$failure;rollback=$rollbackOK} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $jobDirectory 'result.json') -Encoding UTF8
    $restartAllowed = $rollbackOK
    if ($committed) {
        Add-Type -AssemblyName System.Windows.Forms
        $message = '업데이트를 완료하지 못했습니다.'
        if (-not $rollbackOK) { $message += "`nABMSynth 설치 파일을 다시 실행해 복구하세요." }
        [System.Windows.Forms.MessageBox]::Show("$message`n검사 기록: $jobDirectory", 'ABMSynth', 'OK', 'Warning') | Out-Null
    }
} finally {
    if ($lock) {
        if ($ownsLock) { $lock.ReleaseMutex() }
        $lock.Dispose()
    }
    if ($parentProcess) { $parentProcess.Dispose() }
    if (-not $committed -or $succeeded) {
        # Resolve and bound the generated job directory before recursive cleanup.
        $resolvedJob = [IO.Path]::GetFullPath($jobDirectory)
        $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if ($resolvedJob.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $resolvedJob).StartsWith('ABMSynth-update-')) {
            try {
                if ($succeeded) {
                    $null = New-Item -ItemType Directory -Path $plan.logDirectory -Force
                    Copy-Item -LiteralPath (Join-Path $jobDirectory 'result.json') -Destination (Join-Path $plan.logDirectory ('update-' + $plan.version + '.json')) -Force
                }
                Remove-Item -LiteralPath $resolvedJob -Recurse -Force
            } catch { Write-Warning $_.Exception.Message }
        }
    }
}
if ($committed -and $restartAllowed -and (Test-Path -LiteralPath $executable)) {
    Start-Process -FilePath $executable -WorkingDirectory $root
}
