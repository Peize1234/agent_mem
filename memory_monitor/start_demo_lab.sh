#!/usr/bin/env bash

set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd -- "${script_dir}/.." && pwd)"
cd "${repository_root}"

simulation_root="${MEMORY_MONITOR_SIMULATION_ROOT:-${repository_root}/.memory_monitor_runs}"
server_address="${MEMORY_MONITOR_ADDRESS:-127.0.0.1}"
server_port="${MEMORY_MONITOR_PORT:-8501}"
conda_environment="${MEMORY_MONITOR_CONDA_ENV:-MemoryOS}"

if [[ "${CONDA_DEFAULT_ENV:-}" == "${conda_environment}" ]]; then
    python_command=(python)
elif command -v conda >/dev/null 2>&1; then
    python_command=(conda run --no-capture-output -n "${conda_environment}" python)
else
    echo "错误：当前未激活 ${conda_environment}，并且找不到 Conda。" >&2
    echo "请先运行：conda activate ${conda_environment}" >&2
    exit 1
fi

echo "正在检查 Conda 环境 ${conda_environment}..."
if ! "${python_command[@]}" -c \
    "import openai, qdrant_client, sentence_transformers, streamlit" >/dev/null; then
    echo "错误：Conda 环境 ${conda_environment} 缺少 Demo Lab 所需依赖。" >&2
    exit 1
fi

if [[ ! "${server_port}" =~ ^[0-9]+$ ]] || ((server_port < 1 || server_port > 65535)); then
    echo "错误：MEMORY_MONITOR_PORT 必须是 1 到 65535 之间的端口号。" >&2
    exit 1
fi

mkdir -p "${simulation_root}"
export MEMORY_MONITOR_SIMULATION_ROOT="${simulation_root}"

default_memory_config="${script_dir}/demo_config.json"
using_default_config=false
if [[ -z "${MEMORY_MONITOR_MEMORY_CONFIG:-}" ]]; then
    using_default_config=true
    MEMORY_MONITOR_MEMORY_CONFIG="${default_memory_config}"
elif [[ ! -f "${MEMORY_MONITOR_MEMORY_CONFIG}" ]]; then
    echo "警告：找不到自定义配置文件：${MEMORY_MONITOR_MEMORY_CONFIG}" >&2
    echo "将改用默认配置：${default_memory_config}" >&2
    using_default_config=true
    MEMORY_MONITOR_MEMORY_CONFIG="${default_memory_config}"
fi

if [[ ! -f "${MEMORY_MONITOR_MEMORY_CONFIG}" ]]; then
    echo "错误：找不到默认配置文件：${MEMORY_MONITOR_MEMORY_CONFIG}" >&2
    exit 1
fi
export MEMORY_MONITOR_MEMORY_CONFIG

if [[ "${using_default_config}" == true && -z "${DEEPSEEK_API_KEY:-}" ]]; then
    if [[ -t 0 ]]; then
        read -r -s -p "请输入 DEEPSEEK_API_KEY（仅用于本次进程，不会写入文件）：" DEEPSEEK_API_KEY
        echo
        if [[ -z "${DEEPSEEK_API_KEY}" ]]; then
            echo "错误：DEEPSEEK_API_KEY 不能为空。" >&2
            exit 1
        fi
        export DEEPSEEK_API_KEY
    else
        echo "错误：默认配置使用 DeepSeek，请先设置 DEEPSEEK_API_KEY。" >&2
        exit 1
    fi
fi

export STREAMLIT_SERVER_ADDRESS="${server_address}"
export STREAMLIT_SERVER_PORT="${server_port}"
export STREAMLIT_SERVER_HEADLESS=true
export STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

echo "正在启动 Agent Memory Demo Lab..."
echo "访问地址：http://${server_address}:${server_port}"
echo "Conda 环境：${conda_environment}"
echo "沙盒目录：${MEMORY_MONITOR_SIMULATION_ROOT}"
echo "配置文件：${MEMORY_MONITOR_MEMORY_CONFIG}"
echo "按 Ctrl+C 停止服务。"

exec "${python_command[@]}" -m streamlit run memory_monitor/app.py "$@"
