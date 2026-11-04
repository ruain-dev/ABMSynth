param(
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Debug',
    [ValidateSet('x64', 'x86')]
    [string]$Architecture = 'x64',
    [switch]$TraceSmoke,
    [switch]$Mapper,
    [switch]$BuildSmokeHost,
    [switch]$BuildCoexistProbe,
    [switch]$PortsProbe,
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere)) { throw 'Visual Studio Installer vswhere.exe was not found.' }
# Official vswhere Find-VC recipe; Community, Professional and Build Tools
# are all valid. Do not silently choose an installation without x86/x64 VC.
$installation = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or -not $installation) { throw 'Visual C++ x86/x64 build tools were not found.' }
$vsDevCmd = Join-Path ($installation | Select-Object -First 1) 'Common7\Tools\VsDevCmd.bat'
if (-not (Test-Path -LiteralPath $vsDevCmd)) {
    throw "Visual C++ environment script was not found: $vsDevCmd"
}

$variant = if ($TraceSmoke) { 'TraceSmoke' } elseif ($Mapper) { 'Mapper' } else { $Configuration }
$outDir = if ($OutputDirectory) {[IO.Path]::GetFullPath($OutputDirectory)} else {Join-Path $root "out\$Architecture\$variant"}
if ($outDir -match '["&|<>^%!\r\n]') {throw 'Unsupported shell character in output path.'}
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$flags = if ($Configuration -eq 'Debug') { '/Od /Zi /MDd' } else { '/O2 /DNDEBUG /MT' }
# Both release variants are loaded into third-party MIDI hosts. Do not make
# the regular endpoint depend on that host's MSVCP/VCRUNTIME search path while
# the Mapper variant is portable. Allocations stay inside this driver; MIDIHDR
# and callbacks retain WinMM caller ownership across the ABI (no CRT objects).
if ($TraceSmoke) { $flags += ' /DABMSYNTH_TRACE_SMOKE' }
if ($Mapper) { $flags += ' /DABMSYNTH_MAPPER' }
if ($PortsProbe) { $flags += ' /DABMSYNTH_PORTS_PROBE' }
if ($Mapper) {
    # A WinMM driver is loaded inside arbitrary third-party hosts, not beside
    # our Python/Qt runtime. Static CRT avoids depending on their DLL paths or
    # installing another VC runtime solely for this small user-mode driver.
    $flags = $flags.Replace('/MDd','/MTd').Replace('/MD','/MT')
}
$driverName = if ($Mapper) { 'ABMSynthMapper.drv' } else { 'ABMSynthMidi.drv' }
$object = Join-Path $outDir 'ABMSynthMidi.obj'
$compilePdb = Join-Path $outDir 'ABMSynthMidi.pdb'
$importLibrary = Join-Path $outDir 'ABMSynthMidi.lib'
$linkPdb = Join-Path $outDir 'ABMSynthMidi-link.pdb'
$command = "call `"$vsDevCmd`" -arch=$Architecture -host_arch=x64 && cl.exe /nologo /std:c++17 /EHsc /W4 /WX $flags /LD /Fo`"$object`" /Fd`"$compilePdb`" `"$root\ABMSynthMidi.cpp`" /link /DEF:`"$root\ABMSynthMidi.def`" /OUT:`"$outDir\$driverName`" /IMPLIB:`"$importLibrary`" /PDB:`"$linkPdb`" winmm.lib advapi32.lib"
& cmd.exe /d /c $command
if ($LASTEXITCODE -ne 0) { throw "Driver build failed with exit code $LASTEXITCODE." }
if ($BuildSmokeHost) {
    $smokeSource=Join-Path $root 'tests\winmm_smoke.cpp'
    $smokeObject=Join-Path $outDir 'winmm_smoke.obj'
    $smokeExe=Join-Path $outDir 'winmm_smoke.exe'
    $smokeCommand="call `"$vsDevCmd`" -arch=$Architecture -host_arch=x64 && cl.exe /nologo /std:c++17 /EHsc /W4 /WX /O2 /MT /Fo`"$smokeObject`" `"$smokeSource`" /link /OUT:`"$smokeExe`" winmm.lib"
    & cmd.exe /d /c $smokeCommand
    if ($LASTEXITCODE -ne 0) { throw "WinMM smoke host build failed with exit code $LASTEXITCODE." }
}
if ($BuildCoexistProbe) {
    $probeSource=Join-Path $root 'tests\coexist_probe.cpp'
    $probeObject=Join-Path $outDir 'coexist_probe.obj'
    $probeExe=Join-Path $outDir 'coexist_probe.exe'
    $probeCommand="call `"$vsDevCmd`" -arch=$Architecture -host_arch=x64 && cl.exe /nologo /std:c++17 /EHsc /W4 /WX /O2 /MT /Fo`"$probeObject`" `"$probeSource`" /link /OUT:`"$probeExe`" winmm.lib"
    & cmd.exe /d /c $probeCommand
    if ($LASTEXITCODE -ne 0) { throw 'WinMM coexistence probe build failed.' }
}
Get-Item -LiteralPath (Join-Path $outDir $driverName)
