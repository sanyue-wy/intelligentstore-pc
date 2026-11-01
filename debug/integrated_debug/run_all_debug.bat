@echo off
chcp 65001 >nul
title 全系统综合联合调试工作台

cd /d "%~dp0\..\.."
python main.py

if errorlevel 1 (
    echo.
    echo [错误] 启动失败，请检查 Python 环境。
    pause
)
