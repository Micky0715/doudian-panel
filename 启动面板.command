#!/bin/bash
# 抖店控制面板 —— 启动 / 接管
# 说明：优先由 launchd 守护进程常驻（开机自启 + 崩溃自恢复）。
# 本脚本作用：
#   1) 若已安装开机自启（launchd 已加载），直接打开浏览器即可；
#   2) 否则确保面板以「图形会话」身份运行（双击 .command 即图形会话），
#      以便面板内的「开机自启」按钮能正常执行 launchctl。
cd "$(dirname "$0")"
PORT=8080
PLIST="$HOME/Library/LaunchAgents/com.atrenew.doudian-panel.plist"

# 已安装开机自启 → 无需接管，直接打开浏览器
if launchctl list 2>/dev/null | grep -q "com.atrenew.doudian-panel"; then
  echo "开机自启已启用（由系统守护进程托管），直接打开浏览器..."
  open "http://127.0.0.1:$PORT"
  exit 0
fi

# 若 8080 被其它进程（如之前手动起的服务）占用，先接管重启，确保运行在图形会话
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "发现旧服务占用 $PORT，正在接管重启（以便支持开机自启）..."
  PID=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}')
  [ -n "$PID" ] && kill "$PID" 2>/dev/null
  sleep 1
  STILL=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}')
  [ -n "$STILL" ] && kill -9 "$STILL" 2>/dev/null
  sleep 1
fi

echo "正在以图形会话启动面板..."
nohup /usr/bin/python3 panel_server.py > panel_server_boot.log 2>&1 &
sleep 2
if ! lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "启动失败，请查看 panel_server_boot.log"
  exit 1
fi

LAN_IP=$(route -n get default 2>/dev/null | grep interface | awk '{print $2}' | xargs -I{} ipconfig getifaddr {} 2>/dev/null)
HOSTNAME=$(scutil --get LocalHostName 2>/dev/null)
echo "本机访问:   http://127.0.0.1:$PORT"
[ -n "$LAN_IP" ] && echo "局域网访问: http://$LAN_IP:$PORT  (发给同事用这个)"
[ -n "$HOSTNAME" ] && echo "固定主机名: http://${HOSTNAME}.local:$PORT"
open "http://127.0.0.1:$PORT"
