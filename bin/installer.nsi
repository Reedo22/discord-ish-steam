; discord-ish-steam — one-click Windows installer (elevating bootstrapper).
; This EXE is a THIN WRAPPER: it requests admin (UAC), then runs install.ps1,
; which does all the real work (winget git/Python/ffmpeg, Millennium, clone repo,
; install plugin+theme, fetch host binaries, firewall rules, register logon task +
; start the daemon, restart Steam). Keeping the logic in install.ps1 keeps Linux parity.
;
; Build on Linux:  bin/build-installer.sh      (CI builds it on every push; tagged
;                  pushes attach it to a GitHub release)
;
; Command line:
;   /S          silent (no windows; exit code = install.ps1's exit code)
;   /offline    use the install.ps1 bundled in this EXE instead of fetching the latest

Unicode true
!ifndef VERSION
  !define VERSION "0.0.0"
!endif
Name "discord-ish-steam"
OutFile "..\dist\discord-ish-steam-setup.exe"
RequestExecutionLevel admin          ; bake a UAC manifest into the EXE
SetCompressor /SOLID lzma
ShowInstDetails show                  ; stream install.ps1 output to the log window
BrandingText "discord-ish-steam ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "discord-ish-steam"
VIAddVersionKey "FileDescription" "discord-ish-steam setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "github.com/Reedo22/discord-ish-steam"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"

; We don't install into a dir of our own — install.ps1 owns placement — but the
; MUI framework wants a default; harmless and unused.
InstallDir "$TEMP\discord-ish-steam-setup"

!define MUI_WELCOMEPAGE_TEXT "This sets up discord-ish-steam on this PC:$\r$\n$\r$\n  - Millennium (Steam plugin loader), if missing$\r$\n  - the chat plugin + theme$\r$\n  - screen share with desktop audio (Steam's own sounds are left out, so calls don't echo)$\r$\n  - mic noise cancellation$\r$\n$\r$\nIt downloads what it needs (Python, ffmpeg, git) and restarts Steam at the end. Takes a few minutes the first time.$\r$\n$\r$\nRun it again any time to update."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
; Custom finish text (install.ps1 restarts Steam itself now).
!define MUI_FINISHPAGE_TITLE "All set"
!define MUI_FINISHPAGE_TEXT "Steam was restarted with the plugin loaded.$\r$\n$\r$\nChats are docked into the friends list automatically. Screen share and noise cancellation are in the call controls; the share helper starts with Windows."
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

Var PS1

Section "Install"
  SetOutPath "$INSTDIR"
  ; Bundle the current install.ps1 as an offline-safe fallback.
  File "..\install.ps1"
  StrCpy $PS1 "$INSTDIR\install.ps1"

  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/offline" $R1
  ${If} ${Errors}
    ; Try to overwrite with the very latest install.ps1 from GitHub (TLS 1.2), so an
    ; old copy of this EXE still installs current code. Any failure keeps the bundled one.
    DetailPrint "Fetching latest installer script..."
    nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/install.ps1 -OutFile \"$PS1.new\"; Move-Item -Force \"$PS1.new\" \"$PS1\" } catch { Write-Host \"(using bundled install.ps1)\" }"'
    Pop $0
  ${Else}
    DetailPrint "Using the bundled installer script (/offline)."
  ${EndIf}

  DetailPrint "Running installer (Millennium, Python, ffmpeg, the plugin)..."
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$PS1"'
  Pop $0
  DetailPrint "install.ps1 exit code: $0"
  SetErrorLevel $0
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "Setup ran but hit a problem (code $0). Scroll up in the details log for the failing step - usually no internet, or Steam isn't installed." /SD IDOK
  ${EndIf}
SectionEnd
