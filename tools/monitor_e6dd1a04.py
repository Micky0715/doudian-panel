#!/usr/bin/env python3
import time, requests, json, sys, os
from datetime import datetime

tid = "e6dd1a04"
out = "monitor_e6dd1a04.out"
url = "http://127.0.0.1:8080/api/tasks"

with open(out, "w", encoding="utf-8") as f:
    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] 开始监控任务 {tid}\n")

while True:
    try:
        r = requests.get(url, timeout=10)
        d = r.json()
        for t in d.get("tasks", []):
            if t["id"] == tid:
                p = t["progress"]
                line = f"[{datetime.now().strftime('%H:%M:%S')}] state={t['state']} total={p['total']} processed={p['processed']} success={p['success']} failed={p['failed']} skipped={p['skipped']} current={p['current']}"
                with open(out, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                if t["state"] in ("done", "stopped", "error"):
                    with open(out, "a", encoding="utf-8") as f:
                        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] 任务结束\n")
                    sys.exit(0)
                break
    except Exception as e:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: {e}\n")
    time.sleep(30)
