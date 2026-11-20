#!/usr/bin/env bash
# 一键部署：安装 Miniconda（没有时）、创建 homepilot_agent 环境并安装依赖，在 tmux 中启动服务。
#
#   ./deploy.sh         首次部署，或 git pull 后重新部署（会重启服务）
#   ./deploy.sh stop    停止服务
#
# 可用环境变量覆盖：CONDA_HOME（Miniconda 安装位置）、PIP_INDEX（pip 镜像）、
# MINICONDA_URL（Miniconda 安装包下载地址前缀）、CONDA_CHANNEL（创建环境用的频道）
set -euo pipefail

cd "$(dirname "$0")"
ROOT=$(pwd)
ENV_NAME=homepilot_agent
SESSION=homepilot
CONDA_HOME=${CONDA_HOME:-$HOME/miniconda3}
PIP_INDEX=${PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}
MINICONDA_URL=${MINICONDA_URL:-https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda}
CONDA_CHANNEL=${CONDA_CHANNEL:-conda-forge}

log() { printf '\033[32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

stop_service() {
    tmux has-session -t "$SESSION" 2>/dev/null || return 0
    log "停止正在运行的服务"
    tmux send-keys -t "$SESSION" C-c
    for _ in $(seq 10); do
        tmux has-session -t "$SESSION" 2>/dev/null || return 0
        sleep 1
    done
    tmux kill-session -t "$SESSION"
}

install_packages() {  # 用系统包管理器安装缺少的命令
    local sudo=""
    [ "$(id -u)" -eq 0 ] || sudo="sudo"
    if command -v apt-get >/dev/null; then
        $sudo apt-get update -qq && $sudo apt-get install -y -qq "$@"
    elif command -v dnf >/dev/null; then
        $sudo dnf install -y -q "$@"
    elif command -v yum >/dev/null; then
        $sudo yum install -y -q "$@"
    else
        die "请先手动安装：$*"
    fi
}

if [ "${1:-}" = "stop" ]; then
    command -v tmux >/dev/null && stop_service
    exit 0
fi

# ---- 系统命令 ----
missing=()
for cmd in tmux curl; do
    command -v "$cmd" >/dev/null || missing+=("$cmd")
done
if [ ${#missing[@]} -gt 0 ]; then
    log "安装 ${missing[*]}"
    install_packages "${missing[@]}"
fi

# ---- conda 与 Python 环境 ----
if command -v conda >/dev/null; then
    CONDA_BASE=$(conda info --base)
elif [ -x "$CONDA_HOME/bin/conda" ]; then
    CONDA_BASE=$CONDA_HOME
else
    arch=$(uname -m)
    case "$arch" in x86_64 | aarch64) ;; *) die "不支持的 CPU 架构：$arch" ;; esac
    log "安装 Miniconda 到 $CONDA_HOME"
    installer=Miniconda3-latest-Linux-$arch.sh
    curl -fL --retry 3 -o /tmp/miniconda.sh "$MINICONDA_URL/$installer" \
        || curl -fL --retry 3 -o /tmp/miniconda.sh "https://repo.anaconda.com/miniconda/$installer"
    bash /tmp/miniconda.sh -b -p "$CONDA_HOME"
    rm -f /tmp/miniconda.sh
    CONDA_BASE=$CONDA_HOME
fi
PY=$CONDA_BASE/envs/$ENV_NAME/bin/python

if [ ! -x "$PY" ]; then
    log "创建 conda 环境 $ENV_NAME（Python 3.11）"
    # 用 conda-forge 而不是默认频道，避免新版 conda 要求交互确认服务条款
    "$CONDA_BASE/bin/conda" create -y -q -n "$ENV_NAME" -c "$CONDA_CHANNEL" --override-channels python=3.11
fi

log "安装 Python 依赖"
"$PY" -m pip install -q --upgrade pip -i "$PIP_INDEX"
"$PY" -m pip install -q -r requirements.txt -i "$PIP_INDEX"

# ---- 配置 ----
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    token=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')
    sed -i "s|^HOMEPILOT_AUTH_TOKEN=.*|HOMEPILOT_AUTH_TOKEN=$token|" .env
    log "已从 .env.example 生成 .env，并随机生成了客户端令牌"
fi
if grep -qE '^HOMEPILOT_(DASHSCOPE|DEEPSEEK)_API_KEY=(sk-xxx)?$' .env; then
    die "请先在 $ROOT/.env 中填写 HOMEPILOT_DASHSCOPE_API_KEY 和 HOMEPILOT_DEEPSEEK_API_KEY，再重新运行 ./deploy.sh"
fi
TOKEN=$(grep -E '^HOMEPILOT_AUTH_TOKEN=' .env | cut -d= -f2- || true)
PORT=$(grep -E '^HOMEPILOT_PORT=' .env | cut -d= -f2- || true)
PORT=${PORT:-8000}
[ -n "$TOKEN" ] || warn "HOMEPILOT_AUTH_TOKEN 为空：任何人都能连接你的服务并消耗 API 额度，公网部署务必设置"

# ---- 自检 ----
log "运行单元测试"
"$PY" -m pytest -q -p no:cacheprovider >/tmp/homepilot-test.log 2>&1 || { cat /tmp/homepilot-test.log; die "单元测试失败"; }

# ---- 在 tmux 中启动 ----
stop_service
mkdir -p data logs
log "在 tmux 会话 $SESSION 中启动服务"
# 异常退出时 5 秒后自动重启；Ctrl+C 或 ./deploy.sh stop 正常退出时不再重启。tee -i 不响应 Ctrl+C，保证日志写完
tmux new-session -d -s "$SESSION" -c "$ROOT" \
    "export PYTHONUNBUFFERED=1; set -o pipefail
     until '$PY' -m app.main 2>&1 | tee -i -a logs/agent.log; do
         echo \"\$(date '+%F %T') 服务异常退出，5 秒后重启\" | tee -a logs/agent.log; sleep 5
     done"

for _ in $(seq 15); do
    sleep 1
    tail -n 50 logs/agent.log 2>/dev/null | grep -q "服务已启动" && break
done
tmux has-session -t "$SESSION" 2>/dev/null && tail -n 50 logs/agent.log | grep -q "服务已启动" \
    || { tail -n 30 logs/agent.log 2>/dev/null; die "服务没有正常启动，查看上面的日志"; }

cat <<EOF

$(log "部署完成")
  查看实时日志：tmux attach -t $SESSION（按 Ctrl+B 再按 D 退出，服务继续运行）
  日志文件：    $ROOT/logs/agent.log
  停止服务：    ./deploy.sh stop
  更新代码：    git pull && ./deploy.sh

  客户端配置（Windows 上的 homepilot-client/.env）：
    HOMEPILOT_SERVER_URL=ws://<服务器公网IP>:$PORT/ws/voice/
    HOMEPILOT_AUTH_TOKEN=$TOKEN
  别忘了在云服务器的安全组 / 防火墙中放行 TCP $PORT 端口。
EOF

if ! grep -q '"context_token"' data/wechat.json 2>/dev/null; then
    cat <<EOF

  微信提醒尚未绑定（服务运行时操作，无需重启）：
    1. $PY -m app.wechat login   扫码登录
    2. 用微信给 Bot 发一条消息，收到「已绑定 HomePilot」即完成
EOF
fi
