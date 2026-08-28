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
DATA_DIR="$PROJECT_DIR/data"

# 创建必要目录
mkdir -p "$LOG_DIR"
mkdir -p "$PROJECT_DIR/tmp"
mkdir -p "$DATA_DIR"

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
        if [ -f "$PROJECT_DIR/.env.example" ]; then
            echo "正在从 .env.example 复制生成 .env 模板..."
            cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
            echo "❗ 请先编辑 .env 文件填入私钥与 API 配置后再启动：nano .env"
            exit 1
        fi
    fi
}

# 3. 获取运行端口
get_port() {
    local port=8888
    if [ -f "$PROJECT_DIR/.env" ]; then
        local env_port
        env_port=$(grep -E "^PORT=" "$PROJECT_DIR/.env" | cut -d '=' -f2 | tr -d ' "\r\n' || true)
        if [ -n "$env_port" ]; then
            port=$env_port
        fi
    fi
    echo "$port"
}

# 4. 停止服务
stop_service() {
    echo "正在停止 Dashboard 进程..."
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
    pkill -f "polymarket.apps.dashboard" 2>/dev/null || true
    sleep 1
    # 强制 kill 残留
    pkill -9 -f "polymarket.apps.dashboard" 2>/dev/null || true
    echo "🛑 服务已完全停止。"
}

# 5. 启动服务
start_service() {
    # 检查是否已经在运行
    if pgrep -f "polymarket.apps.dashboard" > /dev/null; then
        echo "⚠️ 服务已在运行中 (PID: $(pgrep -f "polymarket.apps.dashboard" | head -n 1))，无需重复启动。"
        echo "如需重启，请运行: bash vps.sh restart"
        return
    fi

    check_env

    echo "=================================================="
    echo "  启动 Polymarket Dashboard 服务..."
    echo "=================================================="

    # 尝试提高文件描述符上限，防止高并发或 WS 异常时因 Too many open files 崩溃
    ulimit -n 65535 2>/dev/null || true

    # 启动前自动归档并清空历史 nohup.log，确保每次重启都是纯净最新日志
    if [ -f "$LOG_FILE" ] && [ -s "$LOG_FILE" ]; then
        mkdir -p "$LOG_DIR/archive"
        cp "$LOG_FILE" "$LOG_DIR/archive/nohup_$(date +%Y%m%d_%H%M%S).log" 2>/dev/null || true
        > "$LOG_FILE" 2>/dev/null || true
    fi

    PORT=$(get_port)

    # 后台启动进程
    nohup env PYTHONPATH="$PROJECT_DIR/src" "$VENV_DIR/bin/python3" -m polymarket.apps.dashboard >> "$LOG_FILE" 2>&1 &
    NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"

    sleep 2

    # 验证是否成功启动
    if kill -0 "$NEW_PID" 2>/dev/null; then
        echo "🚀 服务启动成功！[PID: $NEW_PID]"
        echo "🌐 Web 仪表盘访问: http://<你的VPS公网IP>:$PORT"
        echo "📜 查看实时日志请运行: bash vps.sh logs"
    else
        echo "❌ 启动失败，请检查日志:"
        tail -n 20 "$LOG_FILE"
    fi
}

# 6. 查看运行状态
status_service() {
    PIDS=$(pgrep -f "polymarket.apps.dashboard" || true)
    if [ -n "$PIDS" ]; then
        PORT=$(get_port)
        echo "🟢 服务运行正常 [PID: $PIDS] | 端口: $PORT"
    else
        echo "🔴 服务未运行"
    fi
}

# 7. 查看实时日志
view_logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -f -n 50 "$LOG_FILE"
    else
        echo "日志文件不存在: $LOG_FILE"
    fi
}

# 8. 清理日志与缓存
clean_logs() {
    echo "正在清理日志与临时缓存..."
    > "$LOG_FILE" 2>/dev/null || true
    rm -rf "$PROJECT_DIR/tmp/*" 2>/dev/null || true
    echo "✅ 日志与缓存已清空。"
}

# 9. 更新代码并重启
update_and_restart() {
    echo "正在从 Git 拉取最新代码..."
    git pull origin main || echo "Git pull 失败，跳过直接重启..."
    setup_environment
    stop_service
    start_service
}

# 打印使用帮助
show_help() {
    echo "=================================================="
    echo "  Polymarket VPS 运维管理脚本使用指南"
    echo "=================================================="
    echo "  bash vps.sh start     - 启动 Dashboard 服务"
    echo "  bash vps.sh stop      - 停止 Dashboard 服务"
    echo "  bash vps.sh restart   - 重启 Dashboard 服务"
    echo "  bash vps.sh status    - 查看服务运行状态"
    echo "  bash vps.sh logs      - 实时跟踪控制台日志"
    echo "  bash vps.sh update    - 拉取最新代码并平滑重启"
    echo "  bash vps.sh clean     - 清理过大的 nohup 日志"
    echo "  bash vps.sh auth      - 快速测试 API 鉴权与私钥"
    echo "  bash vps.sh check     - 检查链上余额与成交记录"
    echo "  bash vps.sh gen-keys  - 一键派生/生成 API Key"
    echo "  bash vps.sh redeem    - 手动触发所有已到期结算"
    echo "=================================================="
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
    clean)
        clean_logs
        ;;
    clean-trades|clean-db)
        echo "正在清空历史订单与交易数据库..."
        setup_environment
        "$VENV_DIR/bin/python3" -c "from polymarket.db import clean_all_historical_trades; print('清理明细:', clean_all_historical_trades())"
        echo "正在重启服务以生效..."
        stop_service
        start_service
        ;;
    auth|test)
        setup_environment
        "$VENV_DIR/bin/python3" scripts/test_auth.py
        ;;
    check)
        setup_environment
        "$VENV_DIR/bin/python3" scripts/check.py
        ;;
    gen-keys|gen-key)
        setup_environment
        "$VENV_DIR/bin/python3" scripts/generate_api_keys.py
        ;;
    redeem)
        setup_environment
        "$VENV_DIR/bin/python3" scripts/check.py redeem
        ;;
    setup)
        setup_environment
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        # 默认无参数时打印帮助或执行一键启动流程
        if [ -z "$1" ]; then
            setup_environment
            stop_service
            start_service
        else
            show_help
        fi
        ;;
esac
