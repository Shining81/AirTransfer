#!/bin/bash
# ============================================================
# AirTransfer - 一键启动脚本
# Mac 本地文件传输服务器
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "  ╔═══════════════════════════════════════╗"
echo "  ║     📡 AirTransfer 文件传输服务器      ║"
echo "  ╚═══════════════════════════════════════╝"
echo -e "${NC}"

# 解析参数
PORT=""
SAVE_DIR=""
NO_AUTH=""
NO_SSL=""
EXTRA_ARGS=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --port) PORT="$2"; shift 2 ;;
        --dir) SAVE_DIR="$2"; shift 2 ;;
        --no-auth) NO_AUTH="--no-auth"; shift ;;
        --no-ssl) NO_SSL="--no-ssl"; shift ;;
        --tunnel) EXTRA_ARGS="$EXTRA_ARGS --tunnel $2"; shift 2 ;;
        *) EXTRA_ARGS="$EXTRA_ARGS $1"; shift ;;
    esac
done

# 检测 Python
PYTHON=""
if command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    echo -e "${RED}错误: 未找到 Python，请先安装 Python 3.8+${NC}"
    echo "  brew install python3"
    exit 1
fi

PY_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo -e "${GREEN}✓ Python ${PY_VERSION}${NC}"

# 检查 Python 版本 >= 3.8
PY_MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]); then
    echo -e "${RED}错误: 需要 Python 3.8+，当前版本 ${PY_VERSION}${NC}"
    exit 1
fi

# 创建虚拟环境
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${YELLOW}⏳ 创建虚拟环境...${NC}"
    $PYTHON -m venv "$VENV_DIR"
    echo -e "${GREEN}✓ 虚拟环境已创建${NC}"
fi

# 激活虚拟环境
source "$VENV_DIR/bin/activate"

# 安装依赖
echo -e "${YELLOW}⏳ 检查依赖...${NC}"
if ! python -c "import fastapi" 2>/dev/null; then
    echo -e "${YELLOW}⏳ 安装依赖包...${NC}"
    pip install -q -r requirements.txt
    echo -e "${GREEN}✓ 依赖安装完成${NC}"
else
    echo -e "${GREEN}✓ 依赖已就绪${NC}"
fi

# 检查 qrcode 库（二维码显示用）
if ! python -c "import qrcode" 2>/dev/null; then
    pip install -q qrcode 2>/dev/null || true
fi

# 构建启动参数
ARGS=""
[ -n "$PORT" ] && ARGS="$ARGS --port $PORT"
[ -n "$SAVE_DIR" ] && ARGS="$ARGS --dir $SAVE_DIR"
[ -n "$NO_AUTH" ] && ARGS="$ARGS $NO_AUTH"
[ -n "$NO_SSL" ] && ARGS="$ARGS $NO_SSL"
[ -n "$EXTRA_ARGS" ] && ARGS="$ARGS $EXTRA_ARGS"

# 优雅关闭
cleanup() {
    echo ""
    echo -e "${YELLOW}正在关闭 AirTransfer...${NC}"
    kill $SERVER_PID 2>/dev/null
    wait $SERVER_PID 2>/dev/null
    echo -e "${GREEN}✓ 已停止${NC}"
    exit 0
}
trap cleanup SIGINT SIGTERM

# 启动服务器
echo ""
echo -e "${CYAN}启动中...${NC}"
echo ""

python server.py $ARGS &
SERVER_PID=$!

# 等待服务器启动
sleep 2

# 检查是否启动成功
if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo -e "${RED}服务器启动失败，请查看日志: transfer.log${NC}"
    exit 1
fi

# 等待进程结束
wait $SERVER_PID
