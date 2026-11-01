@echo off
chcp 65001 >nul
title STM32 智能分拣系统 - 上位机调试工具

echo ========================================================
echo   正在启动 STM32 智能分拣系统 Windows 上位机调试工具...
echo ========================================================

cd /d "%~dp0\..\.."
python main.py

if errorlevel 1 (
    echo.
    echo [错误] 启动失败，请检查 Python 是否已正确安装并添加到环境变量 PATH。
    pause
)
