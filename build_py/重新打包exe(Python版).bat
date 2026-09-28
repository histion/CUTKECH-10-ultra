@echo off
chcp 65001 >nul
setlocal
rem 一键打包 Python 单文件版（自举虚拟环境，无需项目自带 runtime）。
rem 产物：dist_py\cuktech 10 ultra.exe
rem 依赖由 build_py\requirements-build.txt 声明；首次运行会自动建 .venv 并安装。
rem 前置：本机已安装 Python 3.13 且 python 在 PATH（没有 runtime\ 也能编译）。

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

set "VENV=%HERE%\.venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo   首次运行：创建虚拟环境并安装依赖…
  python -m venv "%VENV%"
  if errorlevel 1 (
    echo   [失败] 找不到 python，请先安装 Python 3.13 并加入 PATH。
    pause & exit /b 1
  )
  "%VENV%\Scripts\python.exe" -m pip install -U pip
  "%VENV%\Scripts\python.exe" -m pip install -r "%HERE%\requirements-build.txt"
  if errorlevel 1 ( pause & exit /b 1 )
)

set "PY=%VENV%\Scripts\python.exe"
echo.
echo   正在打包（PyInstaller onefile），请稍候…
echo.
"%PY%" "%HERE%\build.py"
if errorlevel 1 goto :fail

echo.
echo   已生成：%HERE%\..\dist_py\cuktech 10 ultra.exe
echo.
pause
exit /b 0

:fail
echo.
echo   [失败] 打包出错，请看上面的报错信息。
echo.
pause
exit /b 1
