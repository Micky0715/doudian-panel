#!/bin/bash
# 抖店面板 —— 把本机 Wi‑Fi 设为一个「顺口」的固定 IP（局域网地址更稳定）
# 用法：双击本文件，在弹出的终端里输入管理员密码即可。
# 说明：脚本会自动从候选列表里挑第一个没被占用的「顺口」IP（如 172.16.6.166），
#       避免和别人冲突。设置后面板服务无需重启，其他设备改用新 IP 或 seedream.local 访问。

CANDIDATES=(172.16.6.66 172.16.6.88 172.16.6.166 172.16.6.199 172.16.6.250)
CHOSEN=""
for ip in "${CANDIDATES[@]}"; do
  if ping -c2 -W 1500 "$ip" >/dev/null 2>&1; then
    echo "$ip 被占用，跳过"
    continue
  fi
  out=$(arp -n "$ip" 2>/dev/null)
  if echo "$out" | grep -qiE "at [0-9a-f]{2}:"; then
    echo "$ip 被占用(ARP)，跳过"
    continue
  fi
  CHOSEN="$ip"
  break
done

if [ -z "$CHOSEN" ]; then
  echo "候选 IP 都不可用，请稍后重试，或手动指定一个空闲 IP。"
  exit 1
fi

echo "选定固定 IP: $CHOSEN （需要输入管理员密码）"
sudo networksetup -setmanual "Wi-Fi" "$CHOSEN" 255.255.255.0 172.16.6.1
sudo networksetup -setdnsservers "Wi-Fi" 223.5.5.5 119.29.29.29

echo
echo "完成。当前本机 IP："
ipconfig getifaddr en1
echo
echo "其他设备现在可用以下地址访问面板（服务无需重启）："
echo "  http://$CHOSEN:8080"
echo "  http://seedream.local:8080"
echo
echo "注意：手动固定 IP 落在路由器 DHCP 地址池内，极个别情况下日后可能被路由器分给别人造成冲突；"
echo "若遇到网络异常，双击「恢复自动IP.command」即可还原。"
