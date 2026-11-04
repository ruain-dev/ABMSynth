param([string]$OutputDirectory,[switch]$AddressSanitizer)
$ErrorActionPreference='Stop'
$sourceDirectory=Split-Path -Parent $MyInvocation.MyCommand.Path
$devDirectory=Split-Path -Parent $sourceDirectory
$deployToRuntime=-not [bool]$OutputDirectory
if($AddressSanitizer -and $deployToRuntime){throw 'ASan requires an isolated output directory.'}
if($deployToRuntime){$OutputDirectory=Join-Path (Split-Path -Parent $devDirectory) 'build\native\dsp'}
$OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
if($OutputDirectory -match '["&|<>^%!\r\n]'){throw 'Unsupported shell character in output path.'}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$vsWhere=Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if(-not(Test-Path -LiteralPath $vsWhere)){throw 'Visual Studio Installer vswhere.exe is required.'}
# Official vswhere Find-VC pattern. Every candidate still needs the independent
# old/new PCM qualification; discovery alone does not certify a new toolchain.
$vsInstallation=& $vsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if($LASTEXITCODE -ne 0 -or -not $vsInstallation){throw 'Visual Studio x64/x86 C++ tools are required.'}
$vsTools=Join-Path $vsInstallation 'Common7\Tools\VsDevCmd.bat'
if(-not(Test-Path -LiteralPath $vsTools)){throw 'VsDevCmd.bat was not found.'}
# Explicit precise FP, never fast/contract: preserve legacy stereo rounding.
$taskBuildSources=@($MyInvocation.MyCommand.Path,(Join-Path $sourceDirectory 'audio_fx.cpp'),
    (Join-Path $sourceDirectory 'midi_recording.h'),
    (Join-Path $sourceDirectory 'midi_ports.h'),
    (Join-Path $sourceDirectory 'midi_humanization.h'),
    (Join-Path $sourceDirectory 'spatial_rate.h'),
    (Join-Path $sourceDirectory 'preload_queue.cpp'),(Join-Path $sourceDirectory 'preload_queue_api.h'),
    (Join-Path $sourceDirectory 'multicore_synth.cpp'),
    (Join-Path $devDirectory '../bass.h'),(Join-Path $devDirectory '../bassmidi.h'),
    (Join-Path $sourceDirectory 'asio_observer.cpp'),(Join-Path $sourceDirectory 'build_asio_observer.ps1'))
$taskBuildHashes=[ordered]@{}
foreach($taskBuildSource in $taskBuildSources){
    $taskBuildPath=[IO.Path]::GetFullPath($taskBuildSource)
    $taskBuildHashes[$taskBuildPath]=(Get-FileHash -LiteralPath $taskBuildPath -Algorithm SHA256).Hash
}
$compileOptions='/nologo /std:c++17 /EHsc /W4 /WX /O2 /fp:precise /MT'
$linkOptions='/nologo /DLL winmm.lib'
if($AddressSanitizer){
    # Microsoft ASan official recipe: diagnostic candidate only, never the
    # installed product; /Zi symbols, no incremental linking, same precise FP.
    if($OutputDirectory -eq [IO.Path]::GetFullPath($devDirectory)){throw 'ASan requires an isolated output directory.'}
    $compileOptions+=' /fsanitize=address /Zi'
    $linkOptions+=' /DEBUG /INCREMENTAL:NO'
}
$buildCommand="call `"$vsTools`" -arch=x64 -host_arch=x64 && cl $compileOptions /c `"$sourceDirectory\audio_fx.cpp`" /Fo`"$OutputDirectory\audio_fx.obj`" /Fd`"$OutputDirectory\compiler.pdb`" && cl $compileOptions /c `"$sourceDirectory\preload_queue.cpp`" /Fo`"$OutputDirectory\preload_queue.obj`" /Fd`"$OutputDirectory\compiler.pdb`" && cl $compileOptions /c `"$sourceDirectory\multicore_synth.cpp`" /Fo`"$OutputDirectory\multicore_synth.obj`" /Fd`"$OutputDirectory\compiler.pdb`" && link $linkOptions `"$OutputDirectory\audio_fx.obj`" `"$OutputDirectory\preload_queue.obj`" `"$OutputDirectory\multicore_synth.obj`" /OUT:`"$OutputDirectory\abmsynth_dsp.dll`" /IMPLIB:`"$OutputDirectory\abmsynth_dsp.lib`" ole32.lib avrt.lib"
& cmd.exe /d /c $buildCommand
if($LASTEXITCODE -ne 0){throw 'Native DSP build failed.'}
& (Join-Path $sourceDirectory 'build_asio_observer.ps1') -OutputDirectory $OutputDirectory
$taskBuildHashesAfter=[ordered]@{}
foreach($taskBuildPath in $taskBuildHashes.Keys){
    $taskBuildHashesAfter[$taskBuildPath]=(Get-FileHash -LiteralPath $taskBuildPath -Algorithm SHA256).Hash
    if($taskBuildHashesAfter[$taskBuildPath] -ne $taskBuildHashes[$taskBuildPath]){throw 'Native source changed during build; candidate is unqualified.'}
}
$taskBuildManifest=[ordered]@{schema=1;status='built';source_before=$taskBuildHashes;source_after=$taskBuildHashesAfter;
    compile_options=$compileOptions;link_options=$linkOptions;visual_studio=$vsInstallation;
    address_sanitizer=[bool]$AddressSanitizer;created_utc=[DateTime]::UtcNow.ToString('o');
    candidate_sha256=(Get-FileHash -LiteralPath (Join-Path $OutputDirectory 'abmsynth_dsp.dll') -Algorithm SHA256).Hash;
    asio_observer_sha256=(Get-FileHash -LiteralPath (Join-Path $OutputDirectory 'abmsynth_asio_observer.dll') -Algorithm SHA256).Hash}
$taskBuildManifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'build-manifest.json') -Encoding utf8
if($deployToRuntime){
    foreach($name in @('abmsynth_dsp.dll','abmsynth_asio_observer.dll')){
        Copy-Item -LiteralPath (Join-Path $OutputDirectory $name) -Destination (Join-Path $devDirectory $name)
    }
}
