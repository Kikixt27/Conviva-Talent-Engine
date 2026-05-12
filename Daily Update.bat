@echo off
chcp 65001 >nul
title Conviva Signal — 每日更新

echo.
echo ╔════════════════════════════════════════╗
echo ║     Conviva Signal · 每日更新          ║
echo ╚════════════════════════════════════════╝
echo.

:: 进入项目文件夹
cd /d "C:\Users\xqi\Desktop\Conviva Signal"

:: 拉取最新数据
echo [1/3] 正在从 GitHub 同步最新数据...
git pull
echo.

:: 自动找到最新的报告文件（按文件名排序，取最后一个）
echo [2/3] 正在查找最新报告...
for /f "delims=" %%i in ('dir /b /o:n /a:-d "reports\*.html" 2^>nul') do set LATEST=%%i

:: 检查是否找到报告
if not defined LATEST (
    echo ⚠  reports 文件夹里还没有报告。
    echo    请等待今晚自动任务运行后再试。
    echo.
    pause
    exit /b
)

echo     最新报告：%LATEST%
echo.

:: 打开报告
echo [3/3] 正在打开报告...
start "" "reports\%LATEST%"

echo.
echo ✓ 完成！%LATEST% 已在浏览器中打开。
echo.
echo ════════════════════════════════════════
echo   今日候选人请在浏览器中查看
echo   如需分享给 HM，拖拽 HTML 到
echo   https://drop.netlify.com 获取链接
echo ════════════════════════════════════════
echo.
pause
