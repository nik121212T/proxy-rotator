@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
echo ==============================================
echo       Proxy Rotator - lancement automatique
echo ==============================================
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap_windows.ps1" %*
set "launcher_exit=%errorlevel%"
if not "%launcher_exit%"=="0" echo [ERREUR] Le lancement a echoue. Consultez les lignes ci-dessus.
echo.
echo Application arretee. Appuyez sur une touche pour fermer.
pause >nul
exit /b %launcher_exit%
