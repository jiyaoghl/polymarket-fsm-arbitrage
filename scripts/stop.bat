@echo off
REM Polymarket 多策略交易系统 — 一键关停脚本

echo ==========================================
echo  Polymarket 多策略交易系统 关停中...
echo ==========================================

taskkill /FI "WINDOWTITLE eq RiskGuard*"      /F /T 2>nul
taskkill /FI "WINDOWTITLE eq MarketScanner*"  /F /T 2>nul
taskkill /FI "WINDOWTITLE eq EVEngine*"       /F /T 2>nul
taskkill /FI "WINDOWTITLE eq OrderExecutor*"  /F /T 2>nul

echo 所有进程已停止。
echo.
echo 提示：数据库 trading.db 和 tmp\halt 目录保留，下次启动时可复用。
