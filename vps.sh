#!/bin/bash

# ==============================================================================
# Polymarket 交易机器人 - VPS 一键管理脚本 (Ubuntu 22.04 / 24.04 兼容)
# ==============================================================================

set -e

# 进入项目根目录
cd "$(dirname "$0")" || exit 1
PROJECT_DIR="$(pwd)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/nohup.log"
PID_FILE="$PROJECT_DIR/tmp/dashboard.pid"
VENV_DIR="$PROJECT_DIR/venv"

# 创建必要目录
mkdir -p "$LOG_DIR"
mkdir -p "$PROJECT_DIR/tmp"

# 1. 检查并准备 Python 虚拟环境与依赖
setup_environment() {
    echo "=================================================="
    echo "  [1/4] 检查系统环境与依赖..."
    echo "=================================================="

    # 检查是否安装了 python3
    if ! command -v python3 > /dev/null 2>&1; then
        echo "未检测到 Python3，正在通过 apt 安装..."
        sudo apt update && sudo apt install -y python3 python3-pip python3-venv
    fi

    # 检查并安装 venv 模块
    if ! dpkg -s python3-venv > /dev/null 2>&1 && ! dpkg -s python3-pip > /dev/null 2>&1; then
        echo "正在安装 python3-venv 与 python3-pip..."
        sudo apt update && sudo apt install -y python3-pip python3-venv
    fi

    # 创建或修复虚拟环境
    if [ ! -f "$VENV_DIR/bin/pip" ] || [ ! -f "$VENV_DIR/bin/python3" ]; then
        echo "正在初始化/重建 Python 虚拟环境: $VENV_DIR ..."
        rm -rf "$VENV_DIR"
        python3 -m venv "$VENV_DIR"
    fi

    # 激活虚拟环境并安装/更新依赖
    echo "安装/更新 requirements.txt 依赖..."
    "$VENV_DIR/bin/python3" -m pip install --upgrade pip -q
    "$VENV_DIR/bin/python3" -m pip install -r requirements.txt -q
    echo "✅ 环境与依赖检查完毕！"
}

# 2. 检查 .env 配置文件
check_env() {
    if [ ! -f "$PROJECT_DIR/.env" ]; then
        echo "⚠️ 警告: 未检测到 .env 配置文件！"
        if [ -f "$PROJECT_DIR/configs/.env.example" ]; then
            echo "正在从 configs/.env.example 复制生成 .env 模板..."
            cp "$PROJECT_DIR/configs/.env.example" "$PROJECT_DIR/.env"
            echo "❗ 请先编辑 .env 文件填入私钥与 API 配置后再启动：nano .env"
            exit 1
        fi
    fi
}

# 3. 停止服务
stop_service() {
    echo "正在停止旧的 Dashboard 进程..."
    # 优先根据 PID 文件停止
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID" 2>/dev/null || true
            sleep 2
            if kill -0 "$PID" 2>/dev/null; then
                kill -9 "$PID" 2>/dev/null || true
            fi
        fi
        rm -f "$PID_FILE"
    fi

    # 兜底通过进程特征停止
    pkill -f "apps.dashboard" 2>/dev/null || true
    sleep 1
    echo "✅ 进程已停止。"
}

# 4. 启动服务
start_service() {
    # 检查是否已经在运行
    if pgrep -f "apps.dashboard" > /dev/null; then
        echo "⚠️ 服务已在运行中 (PID: $(pgrep -f "apps.dashboard" | head -n 1))，无需重复启动。"
        echo "如需重启，请运行: bash vps.sh restart"
        return
    fi

    check_env

    echo "=================================================="
    echo "  启动 Polymarket Dashboard 服务..."
    echo "=================================================="

    # 尝试提高文件描述符上限，防止高并发或 WS 异常时因 Too many open files 崩溃
    ulimit -n 65535 2>/dev/null || true

    # 后台启动进程
    nohup env PYTHONPATH="$PROJECT_DIR/src" "$VENV_DIR/bin/python3" -m apps.dashboard >> "$LOG_FILE" 2>&1 &
    NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"

    sleep 2

    # 验证是否成功启动
    if kill -0 "$NEW_PID" 2>/dev/null; then
        echo "🚀 服务启动成功！[PID: $NEW_PID]"
        echo "🌐 Web 仪表盘访问: http://<你的VPS公网IP>:8888"
        echo "📜 查看实时日志请运行: bash vps.sh logs"
    else
        echo "❌ 启动失败，请检查日志:"
        tail -n 20 "$LOG_FILE"
    fi
}

# 5. 查看运行状态
status_service() {
    PIDS=$(pgrep -f "apps.dashboard" || true)
    if [ -n "$PIDS" ]; then
        echo "🟢 服务运行正常 [PID: $PIDS]"
    else
        echo "🔴 服务未运行"
    fi
}

# 6. 查看实时日志
view_logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -f -n 50 "$LOG_FILE"
    else
        echo "日志文件不存在: $LOG_FILE"
    fi
}

# 7. 更新代码并重启
update_and_restart() {
    echo "正在从 Git 拉取最新代码..."
    git pull origin main || echo "Git pull 失败，跳过直接重启..."
    setup_environment
    stop_service
    start_service
}

# 主入口分发
case "$1" in
    start)
        setup_environment
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        stop_service
        start_service
        ;;
    update)
        update_and_restart
        ;;
    status)
        status_service
        ;;
    logs)
        view_logs
        ;;
    setup)
        setup_environment
        ;;
    *)
        # 默认无参数时执行一键启动流程（环境检测 + 启动）
        setup_environment
        stop_service
        start_service
        ;;
esac
