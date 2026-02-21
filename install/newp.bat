@echo off
REM ═══════════════════════════════════════════════════════════
REM BLACK BOX FACTORY — New Project from Windows
REM
REM Usage:
REM   newp "project-name" "description"
REM   newp "project-name" "description" requirements.txt
REM ═══════════════════════════════════════════════════════════
setlocal enabledelayedexpansion
set VPS_IP=100.107.37.108
set VPS_USER=factory
set PROJECT_NAME=%~1
set DESCRIPTION=%~2
set REQS_FILE=%~3

if "%PROJECT_NAME%"=="" (
    echo.
    echo  Usage: newp "project-name" "description" [requirements.txt]
    echo  Example: newp "my-app" "Expense tracker" C:\Users\leofu\reqs.txt
    exit /b 1
)

echo.
echo  === Creating project: %PROJECT_NAME% ===

if not "%REQS_FILE%"=="" (
    if exist "%REQS_FILE%" (
        echo  Uploading requirements...
        scp "%REQS_FILE%" %VPS_USER%@%VPS_IP%:/tmp/factory-reqs-upload.txt
        if errorlevel 1 ( echo ERROR: Upload failed. Is Tailscale running? & exit /b 1 )
    ) else ( echo  WARNING: File not found, using description only. & set REQS_FILE= )
)

echo  Creating on VPS...
if not "%REQS_FILE%"=="" (
    ssh %VPS_USER%@%VPS_IP% "source ~/.bashrc && ~/new-project.sh '%PROJECT_NAME%' '%DESCRIPTION%' && cat /tmp/factory-reqs-upload.txt > ~/projects/%PROJECT_NAME%/artifacts/requirements/raw-input.md && cd ~/projects/%PROJECT_NAME% && git add -A && git commit -m 'Add detailed requirements' && git push && rm /tmp/factory-reqs-upload.txt"
) else (
    ssh %VPS_USER%@%VPS_IP% "source ~/.bashrc && ~/new-project.sh '%PROJECT_NAME%' '%DESCRIPTION%'"
)

if errorlevel 1 ( echo ERROR: Failed. Check Tailscale and try: ssh factory@%VPS_IP% & exit /b 1 )

echo.
echo  === Done! ===
echo  GitHub: github.com/leonaffi-byte/%PROJECT_NAME%
echo.
echo  TO START:
echo    ssh %VPS_USER%@%VPS_IP%
echo    fsg %PROJECT_NAME%    (Gemini)
echo    fsc %PROJECT_NAME%    (Claude)
echo.
endlocal
