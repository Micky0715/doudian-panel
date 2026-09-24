#!/bin/bash
# 抖店控制面板 —— 启用「开机自启 + 崩溃自恢复」
# 用法：双击本文件（在用户图形会话中运行一次即可，之后每次登录自动启动）
cd "$(dirname "$0")"
PLIST="$HOME/Library/LaunchAgents/com.atrenew.doudian-panel.plist"
echo "正在启用开机自启守护进程..."

# 若手动实例占用 8080，先停掉，避免端口冲突
OLD=$(lsof -nP -iTCP:8080 -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}')
if [ -n "$OLD" ]; then echo "停止手动实例 PID=$OLD"; kill "$OLD" 2>/dev/null; sleep 1; fi

# 注销旧实例（如有）并加载
launchctl bootout "gui/$(id -u)/com.atrenew.doudian-panel" 2>/dev/null
launchctl load "$PLIST" 2>&1
sleep 2

if lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
  LAN_IP=$(route -n get default 2>/dev/null | grep interface | awk '{print $2}' | xargs -I{} ipconfig getifaddr {} 2>/dev/null)
  echo "✅ 已启用并启动成功"
  echo "   本机:   http://127.0.0.1:8080"
  [ -n "$LAN_IP" ] && echo "   局域网: http://$LAN_IP:8080"
  HOSTNAME=$(scutil --get LocalHostName 2>/dev/null)
  [ -n "$HOSTNAME" ] && echo "   固定主机名: http://${HOSTNAME}.local:8080"
else
  echo "⚠️ 启动失败，查看日志："
  echo "   $HOME/doudian-panel/panel_data/logs/launchd.err.log"
fi
