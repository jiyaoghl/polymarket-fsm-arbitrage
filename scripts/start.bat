@echo off
REM Polymarket 多策略交易系统 — 一键启动脚本
REM 启动顺序：RiskGuard -> MarketScanner -> EVEngine -> OrderExecutor

echo ==========================================
echo  Polymarket 多策略交易系统 启动中...
echo ==========================================

REM 确保 src/ 在 PYTHONPATH（可直接 python -m apps.*）
set "PYTHONPATH=%CD%\src"

REM 确保 tmp\halt 目录存在
if not exist "tmp\halt" mkdir "tmp\halt"

REM 初始化数据库（幂等操作）
echo [1/5] 初始化数据库...
python -c "from db import init_db; from config import DB_PATH; init_db(DB_PATH)"

REM 清除非红牌的 lock 文件（重启时恢复黄/橙状态）
if exist "tmp\halt\YELLOW.lock" del /F /Q "tmp\halt\YELLOW.lock"
if exist "tmp\halt\ORANGE.lock" del /F /Q "tmp\halt\ORANGE.lock"

REM 检查 HALT.lock（红牌需要人工清除）
if exist "tmp\halt\HALT.lock" (
    echo [警告] 检测到 HALT.lock，系统处于红牌熔断状态！
    echo 如需重启，请先手动删除 tmp\halt\HALT.lock 文件。
    pause
    exit /b 1
)

echo [2/5] 启动 RiskGuard（风控守卫）...
start "RiskGuard" cmd /k "python -m apps.risk_guard"
timeout /t 2 /nobreak > nul

echo [3/5] 启动 MarketScanner（市场扫描器）...
start "MarketScanner" cmd /k "python -m apps.market_scanner"
timeout /t 1 /nobreak > nul

echo [4/5] 启动 EVEngine（EV 评分引擎）...
start "EVEngine" cmd /k "python -m apps.ev_engine"
timeout /t 1 /nobreak > nul

echo [5/5] 启动 OrderExecutor（订单执行器）...
start "OrderExecutor" cmd /k "python -m apps.order_executor"

echo.
echo ==========================================
echo  所有进程已启动！
echo  使用 scripts\stop.bat 优雅关停所有进程
echo ==========================================
