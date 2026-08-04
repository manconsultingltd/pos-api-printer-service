; =============================================================================
; API Printer Service — NSIS Modern UI Script
; Compiled with: makensis -DOUTDIR=/path/to/dist setup.nsi
; CI builds this natively on windows-latest (see .github/workflows/release.yml).
;
; Bundle layout (assembled by CI before running makensis, paths relative to
; the directory where makensis is invoked):
;   bundle/python/   ← Full Python 3.11 for Windows amd64 (NuGet distribution —
;                       includes Tkinter, pip and the full stdlib)
;   bundle/src/      ← service .py files + printer_control_gui_windows.py
;                       + requirements.txt
;   bundle/src/web/  ← HTML web panel served by the service
;   bundle/deps/     ← pre-downloaded Windows .whl files
;   prepare_python.ps1
;   register_service.ps1
; =============================================================================

Unicode True
!include "MUI2.nsh"
!include "WinVer.nsh"
!include "x64.nsh"

; Solid LZMA compression — default NSIS (zlib, per-file) yields a ~180 MB
; installer here; solid LZMA brings it down substantially by exploiting
; redundancy across the bundled Python stdlib and wheel cache. Trade-off
; is slower makensis pass on CI (single-threaded LZMA over the whole
; archive), not slower install on the user machine.
SetCompressor /SOLID lzma

; ── App metadata ──────────────────────────────────────────────────────────────
!define APP_NAME      "API Printer Service"
!define APP_VERSION   "0.0.0-dev"  ; placeholder — CI stamps 1.1.<run_number> at release build
!define APP_PUBLISHER "POSAwesome"
!define APP_URL       "http://localhost:5058/"
!define APP_REGKEY    "Software\POSAwesome\APIprinterService"
!define APP_UNINST    "Software\Microsoft\Windows\CurrentVersion\Uninstall\APIprinterService"
!define TASK_NAME     "API Printer Service"

Name "${APP_NAME}"
!ifdef OUTDIR
  OutFile "${OUTDIR}/api-printer-setup-windows.exe"
!else
  OutFile "api-printer-setup-windows.exe"
!endif

RequestExecutionLevel admin
InstallDir         "$PROGRAMFILES64\${APP_NAME}"
InstallDirRegKey   HKLM "${APP_REGKEY}" "InstallDir"

; ── MUI pages ─────────────────────────────────────────────────────────────────
!define MUI_ABORTWARNING

!define MUI_WELCOMEPAGE_TITLE "Welcome to ${APP_NAME} ${APP_VERSION} Setup"
!define MUI_WELCOMEPAGE_TEXT  \
  "This wizard will install ${APP_NAME} on your computer.$\r$\n$\r$\n\
  The service provides a local REST API on port 5058 that allows \
  POSAwesome to print receipts directly to Windows-managed thermal \
  printers — no browser print dialog needed.$\r$\n$\r$\n\
  A native Control Panel app is installed for managing printers, \
  running test prints and starting / stopping the service. The service \
  also exposes a browser-based panel at http://localhost:5058/ \
  (bound to localhost — not reachable from other machines).$\r$\n$\r$\n\
  Requirements:  Windows 10 (1809+) or Windows 11, 64-bit.$\r$\n$\r$\n\
  Click Next to continue."

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "Installation Complete"
!define MUI_FINISHPAGE_TEXT  \
  "${APP_NAME} has been installed and started.$\r$\n$\r$\n\
  Control Panel :  Start Menu > ${APP_NAME} > Control Panel$\r$\n\
  Web panel     :  http://localhost:5058/$\r$\n\
  Logs          :  %ProgramData%\api-printer-service\api-printer.log$\r$\n$\r$\n\
  Start Menu shortcuts have been created for the Control Panel and \
  for starting or stopping the service.$\r$\n$\r$\n\
  The service starts automatically at boot."
; Launch the native Tkinter GUI on Finish. pythonw.exe avoids spawning a
; console window; the .py argument is quoted so the path's spaces survive.
; Inner quotes MUST use $\" — MUI2 substitutes this value inside a
; double-quoted Exec string, so literal " would break Exec into multiple args.
!define MUI_FINISHPAGE_RUN            "$INSTDIR\python\pythonw.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS "$\"$INSTDIR\printer_control_gui_windows.py$\""
!define MUI_FINISHPAGE_RUN_TEXT       "Open Control Panel now"

!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ── Pre-install compatibility checks ──────────────────────────────────────────
Function .onInit

    ; 64-bit Windows required — the bundled Python build is x86_64
    ${IfNot} ${RunningX64}
        MessageBox MB_ICONSTOP|MB_OK \
            "${APP_NAME} requires 64-bit Windows.$\r$\n$\r$\n\
            Detected a 32-bit OS. Installation cannot continue."
        Abort
    ${EndIf}

    ; Windows 10 or later. Earlier versions lack curl and modern schtasks
    ; flags used by the helper scripts.
    ${IfNot} ${AtLeastWin10}
        MessageBox MB_ICONSTOP|MB_OK \
            "${APP_NAME} requires Windows 10 or later.$\r$\n$\r$\n\
            Please upgrade your operating system and try again."
        Abort
    ${EndIf}

FunctionEnd

; ── Install section ───────────────────────────────────────────────────────────
Section "${APP_NAME}" SecMain

    ; ── Stop any previously-installed instance BEFORE copying new files ──────
    ; Needed because python.dll and friends are locked while the service runs,
    ; and copy-on-top fails with "file in use" otherwise.
    DetailPrint "Stopping any existing ${APP_NAME} task..."
    nsExec::ExecToLog 'schtasks /End /TN "${TASK_NAME}"'
    Pop $0
    ; Give the OS a moment to release the file handles
    nsExec::ExecToLog 'cmd /c "timeout /t 3 /nobreak >nul"'
    Pop $0

    ; Bundled Python 3.11 (full distribution — includes Tkinter and pip)
    ; lives under $INSTDIR\python\ to keep its Lib\, DLLs\, tcl\ trees
    ; from colliding with the service source files at the install root.
    SetOutPath "$INSTDIR\python"
    File /r "bundle\python\*.*"

    SetOutPath "$INSTDIR"

    ; Service source files + native Tkinter Control Panel
    File "bundle\src\*.py"
    File "bundle\src\requirements.txt"

    ; Web panel static assets (served by the service at /)
    SetOutPath "$INSTDIR\web"
    File "bundle\src\web\*"
    SetOutPath "$INSTDIR"

    ; Pre-downloaded Windows wheels (offline — no internet needed on target)
    SetOutPath "$INSTDIR\deps"
    File "bundle\deps\*.whl"
    SetOutPath "$INSTDIR"

    ; Helper PowerShell scripts
    File "prepare_python.ps1"
    File "register_service.ps1"

    ; Icon used by all ${APP_NAME} shortcuts (Start Menu, Desktop, Startup)
    ; and by the Tk GUI's system-tray icon.
    File "bundle\printer.ico"

    ; ── Setup Python: enable site-packages + install deps from local wheels ──
    DetailPrint "Setting up Python environment..."
    nsExec::ExecToLog \
        'powershell.exe -ExecutionPolicy Bypass -NonInteractive \
         -File "$INSTDIR\prepare_python.ps1" -InstallDir "$INSTDIR"'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_ICONEXCLAMATION \
            "Python setup returned exit code $0.$\nCheck the install log."
    ${EndIf}

    ; ── Register Windows Scheduled Task (auto-start as SYSTEM) ───────────────
    DetailPrint "Registering service..."
    nsExec::ExecToLog \
        'powershell.exe -ExecutionPolicy Bypass -NonInteractive \
         -File "$INSTDIR\register_service.ps1" \
         -InstallDir "$INSTDIR" -Action install'
    Pop $0
    ; The previous version of this script discarded $0 with no check, so a
    ; failing register_service.ps1 (e.g. Register-ScheduledTask throwing on
    ; a locked task store) finished the install with no warning at all and
    ; left the user staring at "Service: Unknown" + "Start failed: exit 1"
    ; in the Control Panel with no way to know the task was never created.
    ; Surface that loudly now — the install completes either way (so the
    ; uninstaller exists for cleanup) but the user gets an actionable
    ; message instead of a silently-broken system.
    ${If} $0 != 0
        MessageBox MB_ICONSTOP|MB_OK \
            "Service registration failed (exit code $0).$\r$\n$\r$\n\
            The scheduled task was not created, so the API Printer \
            Service will NOT start automatically at boot.$\r$\n$\r$\n\
            Re-run this installer as Administrator. If the problem \
            persists, check the install log shown above for details."
    ${EndIf}

    ; ── Helper batch files for the Start/Stop shortcuts ──────────────────────
    ;
    ; The Control Panel GUI triggers these same schtasks calls internally,
    ; but keeping standalone shortcuts gives users a one-click way to
    ; start or stop the service without opening the GUI.

    ; The schtasks arguments are passed as ONE pre-quoted -ArgumentList
    ; string: Windows PowerShell 5.1 joins multi-element lists with spaces
    ; WITHOUT quoting, so '/TN','${TASK_NAME}' used to reach schtasks as
    ; three separate arguments and the shortcut failed with
    ; "Invalid argument/option" after the UAC prompt was accepted.
    ; \$\" emits a literal \" into the .bat, which powershell.exe's
    ; command-line parser turns back into an embedded quote.
    FileOpen $0 "$INSTDIR\start-task.bat" w
    FileWrite $0 "@echo off$\r$\n"
    FileWrite $0 "powershell -NoProfile -Command $\"Start-Process schtasks -ArgumentList '/Run /TN \$\"${TASK_NAME}\$\"' -Verb RunAs$\"$\r$\n"
    FileClose $0

    FileOpen $0 "$INSTDIR\stop-task.bat" w
    FileWrite $0 "@echo off$\r$\n"
    FileWrite $0 "powershell -NoProfile -Command $\"Start-Process schtasks -ArgumentList '/End /TN \$\"${TASK_NAME}\$\"' -Verb RunAs$\"$\r$\n"
    FileClose $0

    ; ── Start Menu shortcuts ──────────────────────────────────────────────────
    CreateDirectory "$SMPROGRAMS\${APP_NAME}"

    ; "Control Panel" — native Tkinter GUI (pythonw = no console window)
    CreateShortcut \
        "$SMPROGRAMS\${APP_NAME}\Control Panel.lnk" \
        "$INSTDIR\python\pythonw.exe" \
        '"$INSTDIR\printer_control_gui_windows.py"' \
        "$INSTDIR\printer.ico" 0 SW_SHOWNORMAL \
        "" "Open the ${APP_NAME} Control Panel"

    ; "Web Panel" — HTML panel in the default browser. Kept as a fallback in
    ; case Python or Tkinter fails to launch (the service binds to localhost,
    ; so this is not reachable from other machines on the LAN).
    CreateShortcut \
        "$SMPROGRAMS\${APP_NAME}\Web Panel.lnk" \
        "explorer.exe" "${APP_URL}" \
        "" 0 SW_SHOWNORMAL \
        "" "Open the ${APP_NAME} web panel in the default browser"

    CreateShortcut \
        "$SMPROGRAMS\${APP_NAME}\Start Service.lnk" \
        "$INSTDIR\start-task.bat" "" \
        "$INSTDIR\start-task.bat" 0 SW_SHOWMINIMIZED \
        "" "Start the ${APP_NAME} background task"

    CreateShortcut \
        "$SMPROGRAMS\${APP_NAME}\Stop Service.lnk" \
        "$INSTDIR\stop-task.bat" "" \
        "$INSTDIR\stop-task.bat" 0 SW_SHOWMINIMIZED \
        "" "Stop the ${APP_NAME} background task"

    CreateShortcut \
        "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" \
        "$INSTDIR\Uninstall.exe"

    ; Desktop shortcut for the Control Panel (discoverability — users who
    ; don't dig through the Start Menu will still find the app).
    CreateShortcut \
        "$DESKTOP\${APP_NAME}.lnk" \
        "$INSTDIR\python\pythonw.exe" \
        '"$INSTDIR\printer_control_gui_windows.py"' \
        "$INSTDIR\printer.ico" 0 SW_SHOWNORMAL \
        "" "Open the ${APP_NAME} Control Panel"

    ; ── Auto-start the GUI at user login ────────────────────────────────────
    ; Writes a shortcut to the All-Users Startup folder so the tray icon
    ; shows up for every user that logs in (matches the machine-wide
    ; service). --start-hidden tells the GUI to skip the main window and
    ; only show its tray icon — the user can click it when they need it.
    SetShellVarContext all
    CreateShortcut \
        "$SMSTARTUP\${APP_NAME}.lnk" \
        "$INSTDIR\python\pythonw.exe" \
        '"$INSTDIR\printer_control_gui_windows.py" --start-hidden' \
        "$INSTDIR\printer.ico" 0 SW_SHOWMINIMIZED \
        "" "Run the ${APP_NAME} Control Panel at login"
    SetShellVarContext current

    ; ── Add / Remove Programs entry ───────────────────────────────────────────
    WriteRegStr   HKLM "${APP_UNINST}" "DisplayName"     "${APP_NAME}"
    WriteRegStr   HKLM "${APP_UNINST}" "DisplayVersion"  "${APP_VERSION}"
    WriteRegStr   HKLM "${APP_UNINST}" "Publisher"       "${APP_PUBLISHER}"
    WriteRegStr   HKLM "${APP_UNINST}" "InstallLocation" "$INSTDIR"
    WriteRegStr   HKLM "${APP_UNINST}" "UninstallString" "$INSTDIR\Uninstall.exe"
    WriteRegDWORD HKLM "${APP_UNINST}" "NoModify"        1
    WriteRegDWORD HKLM "${APP_UNINST}" "NoRepair"        1

    WriteUninstaller "$INSTDIR\Uninstall.exe"

SectionEnd

; ── Uninstall section ─────────────────────────────────────────────────────────
Section "Uninstall"

    nsExec::ExecToLog \
        'powershell.exe -ExecutionPolicy Bypass -NonInteractive \
         -File "$INSTDIR\register_service.ps1" \
         -InstallDir "$INSTDIR" -Action uninstall'

    RMDir /r "$INSTDIR"
    RMDir /r "$SMPROGRAMS\${APP_NAME}"
    Delete "$DESKTOP\${APP_NAME}.lnk"

    ; Auto-start shortcut was created with SetShellVarContext all.
    SetShellVarContext all
    Delete "$SMSTARTUP\${APP_NAME}.lnk"
    SetShellVarContext current

    DeleteRegKey HKLM "${APP_UNINST}"
    DeleteRegKey HKLM "${APP_REGKEY}"

SectionEnd
