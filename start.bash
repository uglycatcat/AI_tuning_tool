#!/usr/bin/env bash
# 启动 web_tool 并在默认浏览器中打开页面

set -euo pipefail

CONDA_ENV="ai_env"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_PID=""

sync_with_remote() {
  if ! command -v git &>/dev/null; then
    echo "未检测到 git，跳过远程同步检查。"
    return 0
  fi
  if ! git -C "${SCRIPT_DIR}" rev-parse --is-inside-work-tree &>/dev/null; then
    echo "当前目录不是 git 仓库，跳过远程同步检查。"
    return 0
  fi

  local remote upstream branch behind ahead choice
  remote="$(git -C "${SCRIPT_DIR}" remote | head -n1)"
  if [[ -z "${remote}" ]]; then
    echo "未配置远程仓库，跳过同步检查。"
    return 0
  fi

  echo "正在检查远程仓库最新提交..."
  if ! git -C "${SCRIPT_DIR}" fetch "${remote}" --quiet; then
    echo "警告：无法连接远程仓库，跳过同步（将使用本地代码启动）。"
    return 0
  fi

  upstream="$(git -C "${SCRIPT_DIR}" rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || true)"
  if [[ -z "${upstream}" ]]; then
    branch="$(git -C "${SCRIPT_DIR}" rev-parse --abbrev-ref HEAD)"
    for candidate in "${remote}/${branch}" "${remote}/main" "${remote}/master"; do
      if git -C "${SCRIPT_DIR}" rev-parse --verify --quiet "${candidate}" &>/dev/null; then
        upstream="${candidate}"
        break
      fi
    done
  fi
  if [[ -z "${upstream}" ]]; then
    echo "无法确定远程跟踪分支，跳过同步。"
    return 0
  fi

  if [[ "$(git -C "${SCRIPT_DIR}" rev-parse HEAD)" == "$(git -C "${SCRIPT_DIR}" rev-parse "${upstream}")" ]]; then
    echo "本地已是最新提交（${upstream}）。"
    return 0
  fi

  behind="$(git -C "${SCRIPT_DIR}" rev-list --count HEAD.."${upstream}" 2>/dev/null || echo 0)"
  ahead="$(git -C "${SCRIPT_DIR}" rev-list --count "${upstream}"..HEAD 2>/dev/null || echo 0)"

  if [[ "${behind}" -eq 0 ]]; then
    echo "本地领先远程 ${ahead} 个提交，跳过自动同步。"
    return 0
  fi

  echo "远程 ${upstream} 有 ${behind} 个新提交："
  git -C "${SCRIPT_DIR}" log --oneline --no-decorate HEAD.."${upstream}" | sed 's/^/  /'

  if [[ -n "$(git -C "${SCRIPT_DIR}" status --porcelain)" ]]; then
    echo "检测到本地未提交的修改，无法自动拉取远程更新。"
    read -rp "是否仍继续启动（保持当前本地代码）？[Y/n] " choice
  elif [[ "${ahead}" -gt 0 ]]; then
    echo "本地与远程已分叉（本地领先 ${ahead}，落后 ${behind}）。"
    read -rp "是否尝试 git pull 同步远程更新？[y/N] " choice
    [[ "${choice}" =~ ^[Yy]$ ]] || return 0
    if ! git -C "${SCRIPT_DIR}" pull --no-rebase "${remote}" "${upstream#${remote}/}"; then
      echo "警告：同步失败，将使用当前本地代码启动。"
    else
      echo "已同步到远程最新提交。"
    fi
    return 0
  else
    read -rp "是否同步到远程最新提交？[Y/n] " choice
  fi

  if [[ -n "${choice:-}" && "${choice}" =~ ^[Nn]$ ]]; then
    echo "已跳过同步，将使用当前本地代码启动。"
    return 0
  fi

  if [[ -n "$(git -C "${SCRIPT_DIR}" status --porcelain)" ]]; then
    echo "将继续使用当前本地代码启动。"
    return 0
  fi

  if ! git -C "${SCRIPT_DIR}" pull --ff-only "${remote}" "${upstream#${remote}/}"; then
    echo "警告：快进同步失败，将使用当前本地代码启动。"
    return 0
  fi

  echo "已同步到远程最新提交。"
}

cleanup() {
  if [[ -n "${SERVER_PID}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

source_conda() {
  if command -v conda &>/dev/null; then
    # shellcheck disable=SC1091
    source "$(conda info --base)/etc/profile.d/conda.sh"
    return 0
  fi
  if [[ -f "${HOME}/miniconda3/etc/profile.d/conda.sh" ]]; then
    # shellcheck source=/dev/null
    source "${HOME}/miniconda3/etc/profile.d/conda.sh"
    return 0
  fi
  if [[ -f "${HOME}/anaconda3/etc/profile.d/conda.sh" ]]; then
    # shellcheck source=/dev/null
    source "${HOME}/anaconda3/etc/profile.d/conda.sh"
    return 0
  fi
  return 1
}

open_browser() {
  local url="$1"
  if command -v xdg-open &>/dev/null; then
    xdg-open "${url}" >/dev/null 2>&1 &
    return 0
  fi
  if command -v sensible-browser &>/dev/null; then
    sensible-browser "${url}" >/dev/null 2>&1 &
    return 0
  fi
  echo "请手动在浏览器中打开：${url}"
}

wait_for_server() {
  local port="$1"
  python - <<PY
import time
import urllib.error
import urllib.request

url = "http://127.0.0.1:${port}/api/health"
for _ in range(60):
    try:
        with urllib.request.urlopen(url, timeout=1):
            raise SystemExit(0)
    except urllib.error.HTTPError:
        raise SystemExit(0)
    except Exception:
        time.sleep(0.5)
raise SystemExit("服务启动超时，请检查终端中的报错信息。")
PY
}

cd "${SCRIPT_DIR}"

sync_with_remote

if ! source_conda; then
  echo "错误：未检测到 conda，请先运行 bash setup.bash 完成环境配置。"
  exit 1
fi

conda activate "${CONDA_ENV}"

if [[ ! -f config.json ]]; then
  echo "警告：未找到 config.json，请先运行 setup.bash 或从 config_example.json 复制配置。"
fi

read -r WEB_HOST WEB_PORT BROWSER_URL < <(python -c "
from web_tool.app.config import default_host, default_port

host = default_host()
port = default_port()
browser_host = '127.0.0.1' if host in ('0.0.0.0', '::') else host
print(host, port, f'http://{browser_host}:{port}/', sep=' ')
")

echo "正在启动 web_tool（${WEB_HOST}:${WEB_PORT}）..."
python -m web_tool &
SERVER_PID=$!

echo "等待服务就绪..."
if ! wait_for_server "${WEB_PORT}"; then
  echo "错误：web_tool 未能成功启动。"
  exit 1
fi

if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
  echo "错误：web_tool 进程已退出。"
  exit 1
fi

open_browser "${BROWSER_URL}"
echo "已在浏览器中打开：${BROWSER_URL}"
echo "按 Ctrl+C 停止服务。"

wait "${SERVER_PID}"
