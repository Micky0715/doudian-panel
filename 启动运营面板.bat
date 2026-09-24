@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================
echo       抖店自动化运营面板
echo ========================================

netstat -ano | findstr /R /C:":8080 .*LISTENING" >nul
if %errorlevel%==0 goto OPEN_PANEL

where py >nul 2>nul
if %errorlevel%==0 (
    echo [1/2] 正在启动后台服务...
    start "抖店运营面板服务" /min py -3 panel_server.py
    goto WAIT_PANEL
)

where python >nul 2>nul
if %errorlevel%==0 (
    echo [1/2] 正在启动后台服务...
    start "抖店运营面板服务" /min python panel_server.py
    goto WAIT_PANEL
)

echo [错误] 未找到 Python，请联系技术人员，不要自行修改文件。
pause
exit /b 1

:WAIT_PANEL
timeout /t 4 /nobreak >nul

:OPEN_PANEL
echo [2/2] 正在打开运营页面...
start "" "http://127.0.0.1:8080"
echo.
echo 页面已打开。运行任务期间请勿关机、休眠或关闭网络。
timeout /t 3 /nobreak >nul
endlocal

