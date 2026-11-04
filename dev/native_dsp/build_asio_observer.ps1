param([string]$OutputDirectory,[switch]$Diagnostic,[switch]$Staging,[switch]$ForwardBuffers)
$ErrorActionPreference='Stop'
$taskSources=Split-Path -Parent $MyInvocation.MyCommand.Path
$taskDev=Split-Path -Parent $taskSources
$deployToRuntime=-not [bool]$OutputDirectory
if($deployToRuntime){$OutputDirectory=Join-Path (Split-Path -Parent $taskDev) 'build\native\asio-observer'}
$OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
if($OutputDirectory -match '["&|<>^%!\r\n]'){throw 'Unsupported shell character in output path'}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$taskVsWhere=Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$taskVs=& $taskVsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$taskTools=Join-Path $taskVs 'Common7\Tools\VsDevCmd.bat'
$taskDefinitions=if($Diagnostic){'/DABMSYNTH_ASIO_DIAGNOSTIC'}else{''}
if(-not $ForwardBuffers){$taskDefinitions+=' /DABMSYNTH_ASIO_STAGING'}
$taskCommand="call `"$taskTools`" -arch=x64 -host_arch=x64 && cl /nologo /std:c++17 /EHsc /W4 /WX /O2 /fp:precise /MT /LD $taskDefinitions `"$taskSources\asio_observer.cpp`" /Fo`"$OutputDirectory\asio_observer.obj`" /link /EXPORT:DllGetClassObject /EXPORT:DllCanUnloadNow /OUT:`"$OutputDirectory\abmsynth_asio_observer.dll`" /IMPLIB:`"$OutputDirectory\abmsynth_asio_observer.lib`" ole32.lib"
& cmd.exe /d /c $taskCommand
if($LASTEXITCODE -ne 0){throw 'ASIO observer build failed'}
if($deployToRuntime){Copy-Item -LiteralPath (Join-Path $OutputDirectory 'abmsynth_asio_observer.dll') -Destination (Join-Path $taskDev 'abmsynth_asio_observer.dll')}
