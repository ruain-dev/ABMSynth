param([ValidateSet('Install','Uninstall','Update','Repair')][string]$Action='Install')
trap {
    # Machine-readable protocol identities. GUI copy belongs to its locale
    # catalog; do not forward framework/English exception prose as user copy.
    $code=switch($script:mapperPhase) {
        'host' {'HOST_UNSUPPORTED'} 'permission' {'ADMIN_REQUIRED'}
        'lock' {'INSTALLER_LOCK_FAILED'} 'routing' {'ROUTING_UNQUALIFIED'}
        'preparation' {'PREPARATION_FAILED'} 'transaction' {'TRANSACTION_FAILED'} 'cleanup' {'CLEANUP_FAILED'}
        default {'INSTALLER_FAILED'}
    }
    if($_.Exception.ToString().Contains('MAPPER_INSTALLER_BUSY')){$code='INSTALLER_BUSY'}
    if($_.Exception.ToString().Contains('MAPPER_LOCK_ABANDONED')){$code='LOCK_ABANDONED_RECOVERY_REQUIRED'}
    $cause=$_.Exception;$rollbackFailed=$false
    while($cause){
        if($cause -is [AggregateException]){$rollbackFailed=$true}
        if(-not $cause.InnerException){break}
        $cause=$cause.InnerException
    }
    $nativeError=if($cause -is [ComponentModel.Win32Exception]){$cause.NativeErrorCode}else{$cause.HResult -band 0xffff}
    if($script:mapperPhase -ne 'cleanup' -and -not $rollbackFailed -and $cause -is [IO.IOException] -and $nativeError -in @(32,33)){$code='DRIVER_IN_USE'}
    if($script:mapperFailureCode){$code=$script:mapperFailureCode}
    $result=ConvertTo-Json -InputObject @{
        schema=1;ok=$false;action=$Action;code=$code;phase=$script:mapperPhase;
        error_type=$_.Exception.GetType().FullName;
        rollback_failed=$rollbackFailed;
        native_error=$nativeError;backup_directory=$backupDirectory} -Compress
    [Console]::Out.WriteLine($result)
    # Retain the exact failure category even when the elevated process has no
    # console. UI copy remains in the locale catalogue,not this result record.
    if($backupDirectory -and (Test-Path -LiteralPath $backupDirectory)){
        try{$result | Set-Content -LiteralPath (Join-Path $backupDirectory 'result.json') -Encoding UTF8}catch{}
    }
    $exitCodes=@{HOST_UNSUPPORTED=10;ADMIN_REQUIRED=11;INSTALLER_BUSY=12;ROUTING_UNQUALIFIED=13;
        PREPARATION_FAILED=14;TRANSACTION_FAILED=15;REPAIR_FILE_CHANGED=16;REPAIR_SLOT_CONFLICT=17;
        REPAIR_OWNERSHIP_MISSING=18;REPAIR_SOURCE_MISMATCH=19;LOCK_ABANDONED_RECOVERY_REQUIRED=20;DRIVER_IN_USE=21;CLEANUP_FAILED=22}
    if($exitCodes.ContainsKey($code)){exit $exitCodes[$code]}
    exit 1
}
# Owned user-mode WinMM driver only. No foreign driver, service, security or
# midimapper alias is replaced. Update preserves mapped images in running apps.
$ErrorActionPreference='Stop'
$script:mapperPhase='host'
# A Python/PowerShell 7 parent may carry a different PSModulePath. Use this
# host's built-in modules, not a user-installed hash/JSON/file implementation.
Import-Module ([IO.Path]::Combine($PSHOME,'Modules','Microsoft.PowerShell.Utility')) -ErrorAction Stop
Import-Module ([IO.Path]::Combine($PSHOME,'Modules','Microsoft.PowerShell.Management')) -ErrorAction Stop

function Assert-OwnedMapperHost([bool]$OperatingSystem64=[Environment]::Is64BitOperatingSystem,[bool]$Process64=[Environment]::Is64BitProcess) {
    # System32 is redirected to SysWOW64 in a 32-bit process. Reject BEFORE
    # elevation, registry access or backup creation rather than disabling WOW64
    # redirection for the entire installer. This package contains x64/x86 only.
    if (-not $OperatingSystem64 -or -not $Process64) {throw 'A 64-bit PowerShell host on 64-bit Windows is required. No driver or registration was changed.'}
}
function Assert-OwnedMapperArchitecture([string]$Path,[string]$Architecture) {
    # Validate the actual candidate PE, not its directory name. Read-only,
    # bounded header access; malformed/truncated/wrong-machine builds refuse
    # preparation before the common file/registration transaction can run.
    $file=[IO.File]::OpenRead($Path)
    $reader=[IO.BinaryReader]::new($file)
    try {
        if ($file.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5a4d) {throw 'Candidate driver is not a PE image.'}
        $file.Position=0x3c;$offset=$reader.ReadUInt32()
        if ([uint64]$offset+24 -gt [uint64]$file.Length) {throw 'Candidate driver PE header is truncated.'}
        $file.Position=$offset
        if ($reader.ReadUInt32() -ne 0x4550) {throw 'Candidate driver PE signature is invalid.'}
        $expected=switch ($Architecture) {'x64' {0x8664} 'x86' {0x14c} default {throw 'Unknown candidate driver architecture.'}}
        if ($reader.ReadUInt16() -ne $expected) {throw 'Candidate driver PE architecture does not match the target.'}
    } finally {$reader.Dispose()}
}
function Assert-OwnedMapperRouting([string]$Action,[object]$TransferComplete,[object]$UseLegacy,[bool]$ServiceSafeDriver=$false) {
    # This is ABMSynth's unqualified install path,not evidence that Windows
    # rejects all WinMM drivers. Owned removal must remain possible after an
    # OS API-mode change. The normal ownership/foreign-slot checks still run.
    if ($Action -ne 'Uninstall' -and -not $ServiceSafeDriver -and $TransferComplete -eq 1 -and ([string]$UseLegacy) -ne '1') {
        throw 'ABMSynth mapper installation is not qualified in this MIDI API mode. System routing was not changed.'
    }
}
function Test-OwnedMapperRoutingContract([string]$Root) {
    # Admission is pinned to both reviewed binaries,not just a new UI promise.
    # A stale/changed/missing package retains the previous safe refusal.
    try {
        $contract=Get-Content -LiteralPath (Join-Path $Root 'routing_contract.json') -Raw | ConvertFrom-Json
        if($contract.schema -ne 1 -or $contract.policy -notin @('ordinary-ports-in-services-v1','winmm-default-with-sdk-caveat-v2')){return $false}
        foreach($arch in @('x64','x86')){
            $path=Join-Path $Root "out\$arch\Mapper\ABMSynthMapper.drv"
            if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne ([string]$contract.sha256.$arch).ToLowerInvariant()){return $false}
            Assert-OwnedMapperArchitecture $path $arch
        }
        return $true
    }catch{return $false}
}
function Enter-OwnedMapperInstaller([string]$Name='Global\ABMSynthMapperInstaller-v1') {
    # Serialize the entire durable snapshot/preparation/rollback boundary.
    # Kernel ownership is required; object existence does NOT mean ownership.
    $security=[Security.AccessControl.MutexSecurity]::new()
    $security.SetAccessRuleProtection($true,$false)
    foreach($sid in @('S-1-5-32-544','S-1-5-18')) {
        $security.AddAccessRule([Security.AccessControl.MutexAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid),
            [Security.AccessControl.MutexRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow))
    }
    $created=$false;$mutex=$null;$owned=$false
    try {
        $mutex=[Threading.Mutex]::new($false,$Name,[ref]$created,$security)
        # Constructor security is ignored for an existing named object.
        # Refuse an unexpected/precreated ACL, rather than joining a gate
        # that lets other users alter the installer ownership boundary.
        $rules=@($mutex.GetAccessControl().GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
        if($rules.Count -ne 2 -or
           @($rules | Where-Object {
                $_.IdentityReference.Value -notin @('S-1-5-32-544','S-1-5-18') -or
                $_.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                $_.MutexRights -ne [Security.AccessControl.MutexRights]::FullControl
           }).Count -ne 0 -or
           @($rules | ForEach-Object {$_.IdentityReference.Value} | Select-Object -Unique).Count -ne 2) {
            throw [InvalidOperationException]::new('MAPPER_LOCK_ACL_MISMATCH')
        }
        try {$owned=$mutex.WaitOne(0)} catch [Threading.AbandonedMutexException] {
            # WaitOne grants ownership on abandonment. Release but refuse:
            # interrupted durable state needs explicit recovery inspection.
            $owned=$true;throw [InvalidOperationException]::new('MAPPER_LOCK_ABANDONED')
        }
        if(-not $owned){throw [InvalidOperationException]::new('MAPPER_INSTALLER_BUSY')}
        return $mutex
    } catch {
        if($owned){$mutex.ReleaseMutex()}
        if($mutex){$mutex.Dispose()}
        throw
    }
}
function Exit-OwnedMapperInstaller($Mutex) {
    if($Mutex){try {$Mutex.ReleaseMutex()} finally {$Mutex.Dispose()}}
}
function Get-MapperValueSnapshot($Key,[string]$Name) {
    $exists=$Key.GetValueNames() -contains $Name
    @{exists=$exists;value=if ($exists) {$Key.GetValue($Name)} else {$null};
      kind=if ($exists) {$Key.GetValueKind($Name)} else {[Microsoft.Win32.RegistryValueKind]::String}}
}
function ConvertTo-OwnedMapperBackup($Entries) {
    # Windows PowerShell5 Select-Object does not expose hashtable keys as
    # selected properties. Convert each entry first, or every backup field
    # silently becomes null even though the live transaction has real values.
    $Entries | ForEach-Object {[pscustomobject]$_} |
        Select-Object arch,slot,subkey,view,target,source,backupFile,retired,before,after,slotBackup,originalSlot,slotBefore,backupBefore,hashBefore
}
function Restore-MapperValue($Key,[string]$Name,$Snapshot) {
    if ($Snapshot.exists) {$Key.SetValue($Name,$Snapshot.value,$Snapshot.kind)}
    else {$Key.DeleteValue($Name,$false)}
}
function Test-MapperValueSnapshot($Left,$Right) {
    $Left.exists -eq $Right.exists -and (-not $Left.exists -or
        ([int]$Left.kind -eq [int]$Right.kind -and
         (ConvertTo-Json -InputObject $Left.value -Compress) -ceq (ConvertTo-Json -InputObject $Right.value -Compress)))
}
function Set-OwnedMapperFile([string]$Source,[string]$Target,[string]$Before,[string]$After) {
    # Same-volume stage, never truncate a live DLL. File.Move refuses an
    # unexpected first-install destination; File.Replace retains destination
    # ACL metadata and reports merge/loaded-file errors rather than ignoring it.
    $present=Test-Path -LiteralPath $Target
    if ($Before) {
        if (-not $present -or (Get-FileHash -LiteralPath $Target).Hash -ne $Before) {throw 'Owned target changed before replacement.'}
    } elseif ($present) {throw 'Unexpected existing target; refusing replacement.'}
    $stage=$Target+'.abm-stage-'+[Guid]::NewGuid().ToString('N')
    try {
        [IO.File]::Copy($Source,$stage,$false)
        if ((Get-FileHash -LiteralPath $stage).Hash -ne $After) {throw 'Staged driver hash mismatch.'}
        if ($Before) {
            # A rejected replacement may be retried only while the exact old
            # target and staged bytes are still intact. No delete/unsafe Move
            # fallback. Unknown IOException0 is not called a proven lock cause.
            for($attempt=0;$attempt -lt 5;$attempt++) {
                try {[IO.File]::Replace($stage,$Target,[NullString]::Value);break}
                catch {
                    $cause=$_.Exception
                    while($cause.InnerException){$cause=$cause.InnerException}
                    $code=$cause.HResult -band 0xffff
                    if($attempt -eq 4 -or $cause -isnot [IO.IOException] -or $code -notin @(0,5,32,33,1175) -or
                        -not (Test-Path -LiteralPath $stage) -or -not (Test-Path -LiteralPath $Target) -or
                        (Get-FileHash -LiteralPath $stage).Hash -ne $After -or
                        (Get-FileHash -LiteralPath $Target).Hash -ne $Before){throw}
                    Start-Sleep -Milliseconds 50
                }
            }
        }
        else {[IO.File]::Move($stage,$Target)}
        if ((Get-FileHash -LiteralPath $Target).Hash -ne $After) {throw 'Published driver hash mismatch.'}
    } finally {
        if (Test-Path -LiteralPath $stage) {Remove-Item -LiteralPath $stage}
    }
}
function Invoke-OwnedMapperTransaction([object[]]$Entries,[string]$Mode,[string]$DriverName) {
    $attempted=@()
    try {
        foreach ($entry in $Entries) {
            $attempted+=$entry
            if (-not (Test-MapperValueSnapshot (Get-MapperValueSnapshot $entry.key $entry.slot) $entry.slotBefore)) {throw 'Driver slot changed before operation.'}
            if ($Mode -eq 'Uninstall') {
                Restore-MapperValue $entry.key $entry.slot $entry.originalSlot
                if ($entry.before) {
                    if ((Get-FileHash -LiteralPath $entry.target).Hash -ne $entry.before) {throw 'Owned file changed before uninstall.'}
                    if(-not $entry.retired){$entry.retired=$entry.target+'.abm-removed-'+[Guid]::NewGuid().ToString('N')}
                    # Rename a loaded image,never truncate it. Do not schedule
                    # cleanup until BOTH architecture transactions commit.
                    [IO.File]::Move($entry.target,$entry.retired)
                }
                $entry.state.DeleteValue('Backup',$false)
                $entry.state.DeleteValue('SHA256',$false)
            } else {
                # New ownership metadata is durable BEFORE file/slot writes.
                $entry.state.SetValue('Backup',$entry.slotBackup,[Microsoft.Win32.RegistryValueKind]::String)
                # Repair keeps an existing exact-owned DLL,including while loaded.
                # Only a missing file may be restored from an identical package.
                if($Mode -eq 'Repair' -and $entry.before){
                    if(-not (Test-Path -LiteralPath $entry.target) -or (Get-FileHash -LiteralPath $entry.target).Hash -ne $entry.before){
                        $script:mapperFailureCode='REPAIR_FILE_CHANGED';throw 'Repair file changed after preparation.'
                    }
                } else {Set-OwnedMapperFile $entry.source $entry.target $entry.before $entry.after}
                if ($Mode -in @('Install','Repair','Update')) {$entry.key.SetValue($entry.slot,$DriverName,[Microsoft.Win32.RegistryValueKind]::String)}
                $entry.state.SetValue('SHA256',$entry.after,[Microsoft.Win32.RegistryValueKind]::String)
            }
        }
    } catch {
        $failure=$_;$rollbackErrors=@()
        # Attempt EVERY restoration, even if another restoration fails. Keep
        # the original error and durable backups for an explicit later retry.
        foreach ($entry in $attempted) {
            try {
                $current=if (Test-Path -LiteralPath $entry.target) {(Get-FileHash -LiteralPath $entry.target).Hash} else {''}
                if ($current -ne $entry.before) {
                    if ($Mode -ne 'Uninstall' -and $current -ne $entry.after) {throw 'Rollback target changed externally.'}
                    if ($Mode -eq 'Uninstall' -and $current) {throw 'Rollback destination appeared externally.'}
                    if ($Mode -eq 'Uninstall' -and $entry.retired -and (Test-Path -LiteralPath $entry.retired)) {
                        if((Get-FileHash -LiteralPath $entry.retired).Hash -ne $entry.before){throw 'Retired file changed before rollback.'}
                        [IO.File]::Move($entry.retired,$entry.target)
                    }
                    elseif ($entry.before) {Set-OwnedMapperFile $entry.backupFile $entry.target $current $entry.before}
                    elseif ($current) {[IO.File]::Delete($entry.target)}
                }
            } catch {$rollbackErrors+=$_.Exception}
            try {
                $slotNow=Get-MapperValueSnapshot $entry.key $entry.slot
                $expectedSlot=if ($Mode -eq 'Uninstall') {$entry.originalSlot} else {@{exists=$true;value=$DriverName;kind=[Microsoft.Win32.RegistryValueKind]::String}}
                if (-not (Test-MapperValueSnapshot $slotNow $entry.slotBefore) -and -not (Test-MapperValueSnapshot $slotNow $expectedSlot)) {throw 'Rollback slot changed externally.'}
                Restore-MapperValue $entry.key $entry.slot $entry.slotBefore
            } catch {$rollbackErrors+=$_.Exception}
            # Failed file restoration must not make a residual DLL unowned.
            try {
                $fileRestored=if ($entry.before) {(Test-Path -LiteralPath $entry.target) -and (Get-FileHash -LiteralPath $entry.target).Hash -eq $entry.before} else {-not (Test-Path -LiteralPath $entry.target)}
                if ($fileRestored) {
                    Restore-MapperValue $entry.state 'Backup' $entry.backupBefore
                    Restore-MapperValue $entry.state 'SHA256' $entry.hashBefore
                }
            } catch {$rollbackErrors+=$_.Exception}
        }
        if ($rollbackErrors.Count) {
            $errors=[Collections.Generic.List[Exception]]::new();$errors.Add($failure.Exception)
            foreach ($error in $rollbackErrors) {$errors.Add($error)}
            throw [AggregateException]::new('MIDI mapper operation and rollback failed; retain the before.json/file archive for recovery.',$errors)
        }
        throw $failure
    }
}

function Register-OwnedMapperRemoval([string]$Path) {
    # Microsoft MoveFileExW:NULL destination +4 schedules removal at reboot.
    # The caller admits only a unique retired path,never the reinstall target.
    if(-not ('ABMSynthMapperCleanupNative' -as [type])){
        Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public static class ABMSynthMapperCleanupNative{[DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern bool MoveFileExW(string source,string destination,uint flags);public static int ScheduleRemoval(string path){return MoveFileExW(path,null,4)?0:Marshal.GetLastWin32Error();}}'
    }
    # Keep the NULL destination and last-error capture inside C#. PowerShell
    # converts a null string argument to an empty string at this boundary.
    $errorCode=[ABMSynthMapperCleanupNative]::ScheduleRemoval($Path)
    if($errorCode){throw [ComponentModel.Win32Exception]::new($errorCode)}
}
function Complete-OwnedMapperRemoval([object[]]$Entries,[scriptblock]$Schedule={param($Path) Register-OwnedMapperRemoval $Path}) {
    $records=@()
    foreach($entry in $Entries){
        if(-not $entry.retired -or -not (Test-Path -LiteralPath $entry.retired)){continue}
        $expectedPrefix=[IO.Path]::GetFullPath($entry.target)+'.abm-removed-'
        $resolved=[IO.Path]::GetFullPath($entry.retired)
        if(-not $resolved.StartsWith($expectedPrefix,[StringComparison]::OrdinalIgnoreCase) -or
            $resolved.Substring($expectedPrefix.Length) -notmatch '^[0-9a-f]{32}$' -or
            (Get-FileHash -LiteralPath $resolved).Hash -ne $entry.before){throw 'Retired cleanup identity mismatch.'}
        try{[IO.File]::Delete($resolved);$pending=$false}
        catch{
            # A mapped image may be renamed but cannot be deleted. Schedule
            # its private retired name;do not ask every client to terminate.
            & $Schedule $resolved
            $pending=$true
        }
        $records+=@{path=$resolved;sha256=$entry.before;reboot_required=$pending}
    }
    return ,$records
}

function Assert-OwnedMapperRepairSnapshot($Snapshot) {
    if($null -eq $Snapshot -or $Snapshot.exists -isnot [bool] -or $Snapshot.slot -notmatch '^midi[1-9]$' -or
       ($Snapshot.exists -and ($Snapshot.kind -ne 1 -or $Snapshot.value -notin @('ABMSynthMidi.drv','ABMSynthMapper.drv')))){
        $script:mapperFailureCode='REPAIR_OWNERSHIP_MISSING';throw 'Repair backup snapshot is invalid.'
    }
}

function Assert-OwnedMapperRepair($Key,[string]$Slot,[string]$Target,[string]$OwnedHash,[string]$DriverName) {
    if($Slot -notmatch '^midi[1-9]$' -or $OwnedHash -notmatch '^[0-9a-fA-F]{64}$'){
        $script:mapperFailureCode='REPAIR_OWNERSHIP_MISSING';throw 'Repair ownership is invalid.'
    }
    $value=Get-MapperValueSnapshot $Key $Slot
    $other=@($Key.GetValueNames()|Where-Object {$_ -ne $Slot -and $_ -match '^midi[1-9]$' -and [string]$Key.GetValue($_) -eq $DriverName})
    if(($value.exists -and [string]$value.value -ne $DriverName) -or $other.Count){
        $script:mapperFailureCode='REPAIR_SLOT_CONFLICT';throw 'Repair slot is owned by another registration.'
    }
    if((Test-Path -LiteralPath $Target) -and (Get-FileHash -LiteralPath $Target).Hash -ne $OwnedHash){
        $script:mapperFailureCode='REPAIR_FILE_CHANGED';throw 'Repair file no longer matches ownership.'
    }
}

function Get-OwnedMapperRepairSourceHash([string]$Source,[string]$OwnedHash) {
    if(-not (Test-Path -LiteralPath $Source)){
        $script:mapperFailureCode='REPAIR_SOURCE_MISMATCH';throw 'Matching repair package is missing.'
    }
    $hash=(Get-FileHash -LiteralPath $Source).Hash
    if($hash -ne $OwnedHash){
        $script:mapperFailureCode='REPAIR_SOURCE_MISMATCH';throw 'Packaged file does not match the missing owned file.'
    }
    return $hash
}

Assert-OwnedMapperHost
$root=Split-Path -Parent $MyInvocation.MyCommand.Path
$driverName='ABMSynthMapper.drv'
$script:mapperPhase='permission'
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {throw 'Administrator permission required.'}
$script:mapperPhase='lock'
$installerGate=Enter-OwnedMapperInstaller
try {
$script:mapperPhase='routing'
$nativeBase=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryView]::Registry64)
$native=$nativeBase.OpenSubKey('SOFTWARE\Microsoft\Windows NT\CurrentVersion\Drivers32')
try {
    Assert-OwnedMapperRouting $Action ($native.GetValue('MidisrvTransferComplete',0)) ($native.GetValue('UseLegacyMidi','0')) (Test-OwnedMapperRoutingContract $root)
} finally {if ($native) {$native.Close()};$nativeBase.Close()}
$script:mapperPhase='preparation'
$backupRoot=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) 'ABMSynth\DriverBackups'
$backupDirectory=Join-Path $backupRoot ([DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')+'-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
$entries=@();$handles=@()
try {
    # Prepare BOTH architectures and durable snapshots before any file or
    # driver registration changes. Only Backup/SHA256 metadata is owned here.
    foreach ($arch in @('x64','x86')) {
        $directory=if ($arch -eq 'x64') {'System32'} else {'SysWOW64'}
        $target=Join-Path $env:WINDIR "$directory\$driverName"
        $source=Join-Path $root "out\$arch\Mapper\$driverName"
        # Logical views, never physical WOW6432Node paths (Microsoft reserves
        # those implementation locations). Ownership metadata deliberately
        # stays in its original 64-bit view under separate x64/x86 children.
        $subkey='SOFTWARE\Microsoft\Windows NT\CurrentVersion\Drivers32'
        $view=if ($arch -eq 'x64') {[Microsoft.Win32.RegistryView]::Registry64} else {[Microsoft.Win32.RegistryView]::Registry32}
        $base=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,$view)
        $handles+=$base
        $key=$base.OpenSubKey($subkey,$true)
        if (-not $key) {throw "Cannot open $arch driver registration for writing."}
        $handles+=$key
        $stateBase=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryView]::Registry64)
        $handles+=$stateBase
        $state=$stateBase.CreateSubKey("SOFTWARE\ABMSynth\MIDIMapper\$arch")
        $handles+=$state
        $backupBefore=Get-MapperValueSnapshot $state 'Backup';$hashBefore=Get-MapperValueSnapshot $state 'SHA256'
        $saved=if ($backupBefore.exists) {[string]$backupBefore.value} else {''}
        if ($saved) {
            $originalSlot=$saved | ConvertFrom-Json;$slot=[string]$originalSlot.slot
            if($Action -in @('Repair','Update')){
                Assert-OwnedMapperRepairSnapshot $originalSlot
                Assert-OwnedMapperRepair $key $slot $target ([string]$hashBefore.value) $driverName
            } elseif ($slot -notmatch '^midi[1-9]$' -or [string]$key.GetValue($slot,'') -ne $driverName) {throw "Owned $arch slot changed; refusing operation."}
        } elseif ($Action -eq 'Install') {
            $slots=@(1..9 | ForEach-Object {"midi$_"})
            $slot=$slots | Where-Object {[string]$key.GetValue($_,'') -in @('ABMSynthMidi.drv',$driverName)} | Select-Object -First 1
            if (-not $slot) {$slot=$slots | Where-Object {$key.GetValueNames() -notcontains $_} | Select-Object -First 1}
            if (-not $slot) {throw "No owned/free $arch MIDI driver slot. Other drivers are unchanged."}
            $originalSlot=Get-MapperValueSnapshot $key $slot;$originalSlot.slot=$slot
            $saved=$originalSlot | ConvertTo-Json -Compress
        } elseif ($Action -eq 'Uninstall') {continue}
        else {
            if($Action -eq 'Repair'){$script:mapperFailureCode='REPAIR_OWNERSHIP_MISSING'}
            throw "Owned $arch slot backup is missing."
        }
        $before='';$backupFile=Join-Path $backupDirectory "$arch-$driverName"
        if (Test-Path -LiteralPath $target) {
            $before=(Get-FileHash -LiteralPath $target).Hash
            if (-not $backupBefore.exists -or -not $hashBefore.exists -or $before -ne $hashBefore.value) {throw "Unowned/externally changed $arch file; refusing operation."}
            # A loaded image denies WRITE but permits same-directory rename
            # and staged replacement. Validate ownership/hash/PE,then perform
            # the actual operation instead of an exclusive WRITE preflight.
            Assert-OwnedMapperArchitecture $target $arch
            Copy-Item -LiteralPath $target -Destination $backupFile
        } elseif ($Action -notin @('Uninstall','Repair','Update') -and $backupBefore.exists) {throw "Owned $arch file is missing; explicit recovery required."}
        $after=''
        if($Action -eq 'Repair' -and $before){$after=$before}
        elseif ($Action -ne 'Uninstall') {
            if($Action -eq 'Repair'){$after=Get-OwnedMapperRepairSourceHash $source ([string]$hashBefore.value)}
            if (-not (Test-Path -LiteralPath $source)) {throw "Build the $arch Mapper variant first."}
            Assert-OwnedMapperArchitecture $source $arch
            $after=(Get-FileHash -LiteralPath $source).Hash
        }
        $entries+=@{arch=$arch;slot=$slot;subkey=$subkey;view=$view.ToString();target=$target;source=$source;backupFile=$backupFile;
            retired=if($Action -eq 'Uninstall'){$target+'.abm-removed-'+[Guid]::NewGuid().ToString('N')}else{''};
            before=$before;after=$after;slotBackup=$saved;originalSlot=$originalSlot;
            slotBefore=(Get-MapperValueSnapshot $key $slot);backupBefore=$backupBefore;hashBefore=$hashBefore;key=$key;state=$state}
    }
    ConvertTo-OwnedMapperBackup $entries |
        ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $backupDirectory 'before.json') -Encoding UTF8
    $script:mapperPhase='transaction'
    Invoke-OwnedMapperTransaction $entries $Action $driverName
    $cleanup=@();$reboot=$false
    if($Action -eq 'Uninstall'){
        $script:mapperPhase='cleanup'
        $cleanup=Complete-OwnedMapperRemoval $entries
        $reboot=@($cleanup | Where-Object {$_.reboot_required}).Count -gt 0
    }
    $result=ConvertTo-Json -InputObject @{schema=1;ok=$true;action=$Action;code=if($reboot){'REMOVED_REBOOT_REQUIRED'}else{'COMPLETED'};
        reboot_required=$reboot;cleanup=$cleanup;backup_directory=$backupDirectory} -Depth 5 -Compress
    $result | Set-Content -LiteralPath (Join-Path $backupDirectory 'result.json') -Encoding UTF8
    [Console]::Out.WriteLine($result)
    if($reboot){exit 3010}
} finally {foreach ($handle in $handles) {if ($handle) {$handle.Close()}}}
} finally {Exit-OwnedMapperInstaller $installerGate}
