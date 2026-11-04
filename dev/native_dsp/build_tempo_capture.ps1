param([string]$OutputDirectory)
$ErrorActionPreference='Stop'
$sourceDirectory=Split-Path -Parent $MyInvocation.MyCommand.Path
$devDirectory=Split-Path -Parent $sourceDirectory
$deployToRuntime=-not [bool]$OutputDirectory
if($deployToRuntime){$OutputDirectory=Join-Path (Split-Path -Parent $devDirectory) 'build\native\tempo-capture'}
$OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
if($OutputDirectory -match '["&|<>^%!\r\n]'){throw 'Unsupported shell character in output path.'}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$finder=Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$installation=& $finder -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$environment=Join-Path $installation 'Common7\Tools\VsDevCmd.bat'
$command="call `"$environment`" -arch=x64 -host_arch=x64 && cl /nologo /std:c++17 /EHsc /W4 /WX /O2 /fp:precise /MT /LD `"$sourceDirectory\tempo_capture.cpp`" /Fo`"$OutputDirectory\tempo_capture.obj`" /link /OUT:`"$OutputDirectory\abmsynth_tempo_capture.dll`" /IMPLIB:`"$OutputDirectory\tempo_capture.lib`""
& cmd.exe /d /c $command
if($LASTEXITCODE -ne 0){throw 'Tempo capture build failed.'}
if($deployToRuntime){Copy-Item -LiteralPath (Join-Path $OutputDirectory 'abmsynth_tempo_capture.dll') -Destination (Join-Path $devDirectory 'abmsynth_tempo_capture.dll')}
