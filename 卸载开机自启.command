#!/bin/bash
# 抖店控制面板 —— 卸载「开机自启」
# 用法：双击本文件（在用户图形会话中运行）
echo "正在移除开机自启守护进程..."
launchctl bootout "gui/$(id -u)/com.atrenew.doudian-panel" 2>&1
echo "✅ 已停止守护进程（当前运行的实例会退出）。"
echo "   如需再次启用，双击「安装开机自启.command」。"
echo "   如需临时手动启动，双击「启动面板.command」。"
