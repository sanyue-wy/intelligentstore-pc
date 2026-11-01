@echo off
chcp 65001 >nul
title 消息中心独立监听与事件注入工具

cd /d "%~dp0\..\.."
python main.py --msg

if errorlevel 1 (
    echo.
    echo [错误] 启动失败，请检查 Python 环境。
    pause
)
