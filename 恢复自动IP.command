#!/bin/bash
# 抖店面板 —— 把本机 Wi‑Fi 恢复为自动获取 IP（DHCP）
# 用法：双击本文件，在弹出的终端里输入管理员密码即可。
# 适用：用了「设置固定IP.command」后若出现网络异常，或想取消固定 IP。

echo "恢复 Wi‑Fi 为自动获取 IP（DHCP，需要输入管理员密码）"
sudo networksetup -setdhcp "Wi-Fi"
sudo networksetup -setdnsservers "Wi-Fi" Empty

echo
echo "完成。当前本机 IP："
ipconfig getifaddr en1
echo
echo "提示：恢复后其他设备改用当前新分配的 IP 或 http://seedream.local:8080 访问面板。"
