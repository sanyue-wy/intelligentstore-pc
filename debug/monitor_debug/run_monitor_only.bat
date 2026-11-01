@echo off
chcp 65001 >nul
title 程序监控与健康诊断工具

cd /d "%~dp0\..\.."
python main.py --monitor

if errorlevel 1 (
    echo.
    echo [错误] 启动失败，请检查 Python 环境。
    pause
)
