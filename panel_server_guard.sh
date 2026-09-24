#!/bin/bash
# 守护脚本：panel_server 退出后自动重启
cd /Users/atrenew/doudian-panel
while true; do
    /usr/bin/python3 /Users/atrenew/doudian-panel/panel_server.py >> /Users/atrenew/doudian-panel/panel_server.out 2>&1
    echo "[$(date)] panel_server 已退出，3秒后重启..." >> /Users/atrenew/doudian-panel/panel_server.out
    sleep 3
done
