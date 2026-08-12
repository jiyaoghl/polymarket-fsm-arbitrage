#!/bin/bash

# ==========================================
# Polymarket Bot 一键更新与后台启动脚本
# 适用于: Ubuntu 24.04
# ==========================================

# 1. 进入脚本所在目录 (即项目根目录)
cd "$(dirname "$0")" || exit

echo "正在拉取 GitHub 上的最新代码..."
git pull origin main

# 如果有 requirements.txt 可以取消注释下面这行来自动更新依赖
# pip install -r requirements.txt

echo "正在关闭旧的 Dashboard 进程..."
# 寻找并 kill 掉正在运行的 python3 -m apps.dashboard 进程
pkill -f "python3 -m apps.dashboard"

# 等待 2 秒确保端口释放
sleep 2

echo "正在后台启动新的 Dashboard 服务..."
# 确保 logs 目录存在
mkdir -p logs

# 使用 nohup 在后台运行，日志输出到 logs/nohup.log
nohup env PYTHONPATH=src python3 -m apps.dashboard > logs/nohup.log 2>&1 &

echo "启动完成！"
echo "您可以使用命令 'tail -f logs/nohup.log' 来查看实时运行日志。"
