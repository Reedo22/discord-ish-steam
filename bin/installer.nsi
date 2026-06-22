; discord-ish-steam — one-click Windows installer (elevating bootstrapper).
; This EXE is a THIN WRAPPER: it requests admin (UAC), then runs install.ps1,
; which does all the real work (winget git/Python/ffmpeg, pip pyaudiowpatch,
; clone repo, install plugin+theme, fetch host binaries, register logon task +
; start the daemon). Keeping the logic in install.ps1 keeps Linux parity intact.
;
; Build on Linux:  makensis bin/installer.nsi   (or bin/build-installer.sh)

Unicode true
Name "discord-ish-steam"
OutFile "..\dist\discord-ish-steam-setup.exe"
RequestExecutionLevel admin          ; bake a UAC manifest into the EXE
SetCompressor /SOLID lzma
ShowInstDetails show                  ; stream install.ps1 output to the log window

!include "MUI2.nsh"
!include "LogicLib.nsh"
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"

; We don't install into a dir of our own — install.ps1 owns placement — but the
; MUI framework wants a default; harmless and unused.
InstallDir "$TEMP\discord-ish-steam-setup"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
; Custom finish text: the user must restart Steam for the plugin to load.
!define MUI_FINISHPAGE_TITLE "Almost done"
!define MUI_FINISHPAGE_TEXT "Setup finished.$\r$\n$\r$\n1) Fully restart Steam (right-click the tray icon > Exit, then relaunch).$\r$\n2) Friends settings > enable 'Dock chats to the friends list'.$\r$\n$\r$\nScreen share + noise cancellation are now installed and the daemon auto-starts at logon."
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

Var PS1

Section "Install"
  SetOutPath "$INSTDIR"
  ; Bundle the current install.ps1 as an offline-safe fallback.
  File "..\install.ps1"
  StrCpy $PS1 "$INSTDIR\install.ps1"

  ; Try to overwrite with the very latest install.ps1 from GitHub (TLS 1.2).
  ; On ANY failure we keep the bundled copy — never block the install.
  DetailPrint "Fetching latest installer script..."
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/install.ps1 -OutFile \"$PS1\" } catch { Write-Host \"(using bundled install.ps1)\" }"'
  Pop $0

  ; Run the real installer, elevated (we already hold admin), output -> log window.
  DetailPrint "Running installer (this auto-installs git, Python, ffmpeg, and the plugin)..."
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$PS1"'
  Pop $0
  DetailPrint "install.ps1 exit code: $0"
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "Setup ran but install.ps1 returned code $0. Check the details log above for the failing step (most often: no internet, or Steam/Millennium not installed)."
  ${EndIf}
SectionEnd
