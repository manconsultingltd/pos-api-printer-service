<#
.SYNOPSIS
    Register or remove the API Printer Service as a Windows Scheduled Task.
    Called by the NSIS installer — not meant to be run directly.

.PARAMETER InstallDir
    Directory where the service was installed.

.PARAMETER Action
    "install" to register and start, "uninstall" to stop and remove.
#>
param(
    [Parameter(Mandatory)][string] $InstallDir,
    [Parameter(Mandatory)][ValidateSet("install","uninstall")][string] $Action
)

# Hard-fail on any unhandled error so the installer (which checks our exit
# code) can surface a clear message instead of leaving the user with a
# silently-unregistered task. The original script swallowed errors and
# returned 0 even when Register-ScheduledTask threw, which produced
# Control Panel screens stuck on "Service: Unknown".
$ErrorActionPreference = "Stop"

$TaskName  = "API Printer Service"
$Python    = Join-Path $InstallDir "python\python.exe"
$MainPy    = Join-Path $InstallDir "main.py"
# Task runs as SYSTEM which has no real APPDATA — use ProgramData, the
# standard machine-wide writable location for system-service state.
$LogFile      = "$env:ProgramData\api-printer-service\api-printer.log"
# Separate file for very early stderr — Python errors that fire before
# the file handler in main.py is configured (missing module, import-time
# crash, missing DLL) never reach api-printer.log because the logger is
# not yet alive. Without this capture we have no signal at all when the
# scheduled task "ran" but the process died in the first hundred ms.
$StartupErrLog = "$env:ProgramData\api-printer-service\startup-err.log"
$Wrapper       = Join-Path $InstallDir "start-service.bat"

if ($Action -eq "install") {

    # Ensure log directory exists
    New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

    # Wrapper .bat sets env vars before launching the service. Stderr is
    # appended to a startup error log so import-time / pre-logger crashes
    # leave a trail — the main api-printer.log is only written *after*
    # main.py finishes configuring logging.
    Set-Content -Path $Wrapper -Encoding ASCII -Value @"
@echo off
set LOG_FILE=$LogFile
set PYTHONUNBUFFERED=1
"$Python" "$MainPy" 2>>"$StartupErrLog"
"@

    # Remove any previous registration so a re-install / repair starts from a
    # clean slate. This is wrapped defensively: with $ErrorActionPreference =
    # Stop a throwing Unregister-ScheduledTask (task currently running, locked
    # task store, access denied, or a leftover task sitting in a non-root
    # folder) would kill the whole script with exit 1 — surfacing as
    # "Service registration failed" even though the only problem was a stale
    # same-name task. If the cmdlet can't remove it, fall back to the native
    # schtasks.exe, which is locale-independent and can delete tasks the
    # PowerShell module sometimes refuses (e.g. created by a different tool).
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        try {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop | Out-Null
        } catch {
            Add-Content -Path $StartupErrLog -ErrorAction SilentlyContinue `
                -Value "Unregister-ScheduledTask failed, falling back to schtasks: $($_.Exception.Message)"
            # /F = force delete even if running; redirect output so the
            # installer log is not polluted with schtasks chatter.
            & schtasks.exe /Delete /TN $TaskName /F *> $null
        }

        # Confirm the old task is actually gone. If something still holds the
        # name, Register-ScheduledTask -Force below may collide; fail early
        # with an actionable message instead of a cryptic registration error.
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            $msg = "A scheduled task named '$TaskName' already exists and could not be removed (it may be running or locked). Reboot and re-run the installer as Administrator."
            Write-Error $msg
            Add-Content -Path $StartupErrLog -Value $msg -ErrorAction SilentlyContinue
            exit 1
        }
    }

    $Action_ = New-ScheduledTaskAction `
        -Execute          "cmd.exe" `
        -Argument         "/c `"$Wrapper`"" `
        -WorkingDirectory $InstallDir

    $Trigger  = New-ScheduledTaskTrigger -AtStartup

    # RestartInterval MUST be at least 1 minute: the Task Scheduler schema
    # constrains <RestartOnFailure><Interval> to the range PT1M..P31D. A
    # previous revision set this to 15 seconds (PT15S) to speed up the
    # cold-boot retry on freshly-installed Win11 boxes — but that value is
    # below the schema floor, so Register-ScheduledTask throws
    # "The task XML contains a value which is incorrectly formatted or out
    # of range" and, with $ErrorActionPreference = Stop, the script exits 1.
    # That is the "Service registration failed (exit code 1)" the installer
    # reported. 1 minute is the smallest value Windows actually accepts.
    #
    # To still recover quickly from a flaky first-boot start without waiting
    # on the 1-minute restart floor, we ALSO add a one-shot delayed startup
    # trigger (boot + 30s). If the AtStartup launch died before the disk
    # cache warmed / Defender finished scanning the bundled Python tree, the
    # delayed trigger brings the service up well before the restart timer.
    $TriggerDelayed = New-ScheduledTaskTrigger -AtStartup
    $TriggerDelayed.Delay = "PT30S"

    # Win10/11 Fast Startup: "Shut down" hibernates the kernel session
    # instead of doing a real boot, so on the next power-on the AtStartup
    # triggers do NOT fire and the service stays down until a full
    # Restart. A logon trigger (any user) covers that path; with
    # -MultipleInstances IgnoreNew it is a no-op when already running.
    $TriggerLogon = New-ScheduledTaskTrigger -AtLogOn

    $Settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
        -RestartCount 5 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable

    $Principal = New-ScheduledTaskPrincipal `
        -UserId    "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel  Highest

    # Wrap the actual registration so any failure (out-of-range setting,
    # locked task store, locale-specific principal mapping, group policy)
    # surfaces its REAL message in the install log instead of a bare
    # "exit code 1". The message is also appended to the startup error log
    # so it can be recovered after the installer window is gone.
    try {
        Register-ScheduledTask `
            -TaskName  $TaskName `
            -Action    $Action_ `
            -Trigger   @($Trigger, $TriggerDelayed, $TriggerLogon) `
            -Settings  $Settings `
            -Principal $Principal `
            -Force | Out-Null
    } catch {
        $msg = "Register-ScheduledTask failed: $($_.Exception.Message)"
        Write-Error $msg
        Add-Content -Path $StartupErrLog -Value $msg -ErrorAction SilentlyContinue
        exit 1
    }

    # Verify registration actually landed. Register-ScheduledTask with
    # -Force can fail silently in some edge cases (mismatched principal,
    # locked task store), and downstream every "Start failed: exit 1" in
    # the Control Panel was tracking back to a missing task. Re-query so
    # the installer fails loudly if the task isn't there.
    $registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $registered) {
        Write-Error "Scheduled task '$TaskName' was not registered."
        exit 1
    }

    Start-ScheduledTask -TaskName $TaskName

} elseif ($Action -eq "uninstall") {

    # Same defensive removal as the install path: never let a stuck task
    # abort the uninstaller. Fall back to schtasks.exe if the cmdlet throws.
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        try {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop | Out-Null
        } catch {
            & schtasks.exe /Delete /TN $TaskName /F *> $null
        }
    }
}
