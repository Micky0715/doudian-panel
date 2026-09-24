"""监控批量任务 5b2ddc03，结束时打印完整结果统计"""
import json
import time
import sys
import urllib.request

API = "http://127.0.0.1:8080"
TID = "5b2ddc03"
LOG_PATH = "/Users/atrenew/doudian-panel/panel_data/logs/run_20260803_140405_5b2ddc03.log"

# 起始时间戳
start = time.time()
last_processed = 0
last_log_lines = 0
heartbeat = 0
_last_total = 0

print("[monitor] 启动任务监控: tid=%s" % TID)
print("[monitor] 日志文件: %s" % LOG_PATH)
print("=" * 70)

while True:
    elapsed = int(time.time() - start)
    try:
        with urllib.request.urlopen("%s/api/task/%s/logs" % (API, TID), timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print("[%5ds] ERR api: %s" % (elapsed, e))
        time.sleep(10)
        continue

    p = data["task"]["progress"]
    state = data["task"]["state"]
    cur = p.get("current", "")
    processed = p["processed"]
    total = p["total"]
    success = p["success"]
    failed = p["failed"]
    skipped = p["skipped"]
    err_count = data["task"].get("error_count", 0)
    msg = data["task"].get("message", "")

    # 进度变化时打点
    if processed != last_processed or state in ("done", "error", "stopped") or (total and total != _last_total):
        elapsed_d = max(1, elapsed - heartbeat)
        rate = (processed - last_processed) / elapsed_d if processed > last_processed else 0
        eta = ""
        if rate > 0 and processed < total:
            eta_sec = int((total - processed) / rate)
            eta = " ETA=%dm%02ds" % (eta_sec // 60, eta_sec % 60)
        print("[%5ds] state=%s | %d/%d | ✅%d ❌%d ⏭%d ⚠%d (错) | cur=%s%s"
              % (elapsed, state, processed, total, success, failed, skipped, err_count,
                 cur[:40] if cur else "", eta))
        last_processed = processed
        _last_total = total
        heartbeat = elapsed

    if state in ("done", "error", "stopped"):
        print("=" * 70)
        print("[monitor] 任务结束 state=%s msg=%s" % (state, msg))
        print("[monitor] 统计: 成功=%d 失败=%d 跳过=%d 总处理=%d/%d"
              % (success, failed, skipped, processed, total))
        print("[monitor] 错误图片异常: %d" % err_count)
        print()
        print("[monitor] === 日志末尾 80 行 ===")
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
                for line in lines[-80:]:
                    print("  " + line.rstrip())
        except Exception as e:
            print("[monitor] 读取日志失败: %s" % e)
        break

    time.sleep(15)
