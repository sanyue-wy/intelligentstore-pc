@echo off
chcp 65001 >nul
title 视觉模块独立调试工具

cd /d "%~dp0\..\.."
python main.py --vision

if errorlevel 1 (
    echo.
    echo [错误] 启动失败，请检查 Python 环境。
    pause
)
