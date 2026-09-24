# 抖店批量上架项目 · AI 交接文档（全量技术记录）

> 面向：接手本项目的技术同学 / AI 助手
> 生成日期：2026-08-07（本次为"不漏任何细节"极详版）
> 项目根目录：`/Users/atrenew/doudian-panel/`
> 配套文档：`交接文档/抖店批量上架_交接手册_运营版.html`（给小白运营看，已改亮色主题）
> 本文档所有行号来自 `test_doudian_batch.py`（共 7598 行）、`panel_server.py`（约 739 行）、`panel_runner.py`（约 25KB），以本次扫描为准。行号随版本漂移时，请以**函数名**定位为主。

---

## 0. 项目一句话定位

macOS 本地 **Python 3 + Playwright** 工具，自动把抖音小店（抖店 / 抖店后台 `fxg.jinritemai.com`）草稿箱里的二手手机**批量上传发布**：自动填品牌 / 型号、传主图+白底图+详情图、填库存价格、关规格开关、发布，并在发布前做型号终检。带一个本地网页控制面板（看板 `panel.html`，后端 `panel_server.py`，worker `panel_runner.py`）。**无任何第三方通知通道，附件/提醒仅依赖抖店任务助手原生能力**。

- 语言：Python 3（项目用 `/usr/bin/python3`，非 venv）
- 浏览器驱动：Playwright（chromium，viewport 1440×900）
- Web 框架：Flask（仅本地面板）
- 守护：macOS launchd（`com.atrenew.doudian-panel`，端口 8080）

---

## 1. 运行环境与部署

| 项 | 值 |
|---|---|
| OS | macOS（用户 `atrenew`） |
| Python | `/usr/bin/python3`（系统 Python 3，无虚拟环境） |
| 持久化登录态 | `storage_state.json`（Playwright 导出，约 1.7MB）+ `chrome_user_data/` 浏览器数据 |
| 面板端口 | **8080** |
| 面板 HOST | 默认 `0.0.0.0`（开放局域网）；环境变量 `DP_HOST=127.0.0.1` 可改仅本机 |
| 守护标识 | `com.atrenew.doudian-panel`（launchd plist：`~/Library/LaunchAgents/com.atrenew.doudian-panel.plist`） |
| plist 关键字段 | `RunAtLoad=true`、`KeepAlive=true`、`WorkingDirectory=/Users/atrenew/doudian-panel`、日志 `panel_data/logs/launchd.out.log` & `launchd.err.log` |
| 固定 IP 段 | `172.16.6.x`（网关 `172.16.6.1`，DNS `223.5.5.5`/`119.29.29.29`），由「设置固定IP.command」设置，便于局域网同事访问 |
| 并行上限 | 同一店铺不可重复启动；最多 4 个任务并行（看板「并行 0/4」） |

启动方式：双击 `启动面板.command`（或装开机自启后本机 `http://127.0.0.1:8080`）；面板「开始运行」→ `panel_server.py` 以**子进程**拉起 `panel_runner.py` 真正驱动 Playwright。

---

## 2. 目录与完整文件清单（精确到每个文件）

### 2.1 顶层文件（`/Users/atrenew/doudian-panel/`）

| 文件 | 角色 | 说明 |
|---|---|---|
| `test_doudian_batch.py` | **核心脚本** | 380KB / 7598 行，批量上架主逻辑（全部自动化动作）。顶层 101 个 `def`、11 个全局常量/字典，无 `class` |
| `panel_server.py` | 面板后端 | Flask 服务，本地 8080，提供 17 个 HTTP 接口（见 §5.1） |
| `panel_runner.py` | 面板 worker | 被 `panel_server` 以子进程拉起，驱动 Playwright 真正操作抖店后台；含「免主图」monkey-patch（见 §3.4） |
| `panel.html` | **看板前端** | 浏览器访问 `127.0.0.1:8080` 的"抖店多店铺控制面板"（店铺选择 / 测试·批量模式 / 实时日志 / 错误汇总 / 访问地址 / 开机自启开关） |
| `album.html` | 素材相册页 | 查看商品素材图片，辅助用（接口 `/album`） |
| `spec_switch_mock.html` | 调试页 | 规格开关定位调试用 mock 页，非运行必需 |
| `启动面板.command` | 启动脚本 | 双击启动面板（launchd 已存在则直开，否则 nohup 拉起 `panel_server.py`） |
| `安装开机自启.command` | 启动脚本 | `launchctl load` 守护（开机自启 + 崩溃自恢复） |
| `卸载开机自启.command` | 启动脚本 | `launchctl bootout` 停止并移除守护（不删 plist） |
| `设置固定IP.command` | 启动脚本 | 设 Wi-Fi 为 `172.16.6.x` 静态 IP（ICMP+ARP 探测挑首个空闲） |
| `恢复自动IP.command` | 启动脚本 | Wi-Fi 恢复 DHCP |
| `storage_state.json` | 登录态 | Playwright 持久化登录凭证（约 1.7MB），删了需重新登录 |
| `chrome_user_data/` `chrome_user_data_2/` `chrome_user_data_3/` | 浏览器数据 | Playwright 用户数据目录（登录态）；**别删** |
| `panel_server_guard.sh` | 守护脚本 | 服务守护/看门狗（360B） |
| `panel_server.out` / `panel_server_boot.log` | 服务日志 | 服务启动/输出 |
| `debug_run.log`（216KB） | 运行日志 | 调试运行输出 |
| `spec_switch_*.png`（`spec_switch_initial.png` / `spec_switch_after_a.png`） | 调试截图 | 规格开关定位前后对比 |
| `spec_switch_*.py` / `spec_switch_*.log` | 调试脚本 | `spec_switch_live_test.py` / `spec_switch_logic_test.py` / `spec_switch_live_test.log`（规格开关逻辑验证） |
| `verify_oppo.py` / `verify_oppo_debug.log` / `verify_oppo_result.json` | OPPO 验证 | OPPO 钉死逻辑的独立验证脚本与产物 |
| `redmi_brand_test.py` | 测试 | Redmi 品牌显示逻辑测试 |
| `click_logo.png` | 资源 | 面板/相册点击 logo 图 |
| `.DS_Store` `__pycache__` `.pytest_cache` | 系统/缓存 | macOS 元数据 / Python 编译缓存（非源码） |

### 2.2 `tests/`（单元测试，14 个 `.py` 源码 + `__pycache__` 编译缓存）

| 文件 | 作用 |
|---|---|
| `test_no_main_mode.py` | 免主图模式测试 |
| `test_model_select_position.py` | 型号选择坐标定位测试 |
| `test_redmi_parse.py` | Redmi 品牌/型号解析测试 |
| `test_publish_logic.py` | 发布流程逻辑测试 |
| `verify_fixes_v2.py` | 修复验证 v2 |
| `test_oppo_model_fix.py` | OPPO 型号修复测试 |
| `test_oppo_pure_model_extract.py` | OPPO 纯型号提取测试 |
| `verify_fixes.py` | 修复验证（总） |
| `test_all_models.py` | 全量机型解析测试 |
| `test_specific_titles.py` | 指定标题解析测试 |
| `test_condition_fix.py` | 成色解析修复测试 |
| `test_prefix_regression.py` | 品牌前缀回归测试 |
| `test_5g_restore.py` | 5G 机型还原测试 |
| `test_redmi_parse.py` 等同名 `*.cpython-313.pyc` | 编译缓存（`__pycache__/`，非源码，运行 pytest 时生成） |

> 运行：`pytest tests/`（项目根）。日常不用管。

### 2.3 `tools/`（37 个日志分析 / 诊断 / 调试辅助脚本，**出复杂问题时技术同学用**）

| 文件 | 作用 |
|---|---|
| `analyze_publish_failures.py` | 发布失败分析（产出失败归因） |
| `analyze_log_failures.py` | 日志失败行提取分析 |
| `diagnose_run_current.py` | 诊断当前运行 |
| `debug_image_match.py` | 图片匹配调试 |
| `diag_stock_cell.py` | 库存单元格诊断 |
| `diag_inventory_dom.py` + `diag_inventory_dom_result.json` | 库存 DOM 诊断 + 结果 |
| `diag_honor200_scroll.py` / `diag_honor200_scroll_v2.py` + `diag_honor200_scroll_result.json` / `diag_honor200_scroll_v2_result.json` | 荣耀 200 虚拟滚动诊断（v1/v2） |
| `test_stock_fix.py` / `test_stock_fix_inject.py` / `test_stock_fix_scroll.py` / `test_stock_fix_v2.py` / `replace_fix_stock.py` | 库存修复（负库存）测试与替换 |
| `test_inventory_fix.py` / `test_inventory_fill.py` / `test_inventory_fill_before.png` / `test_inventory_fix_before.png` / `test_inventory_fix_v2_before.png` / `test_inventory_fix_v2_after.png` / `test_inventory_fix_after.png` | 库存填充前后截图对照（4 张 PNG） |
| `cross_reference_excel_images.py` / `cross_reference_excel_images_v2.py` / `cross_reference_excel_images_v3.py` | Excel 机型 vs 图片目录交叉引用（v1~v3） |
| `verify_excel_titles.py` | Excel 标题校验 |
| `verify_model_select_fix.py` / `verify_title_model_fix.py` | 型号选择修复 / 标题型号修复验证 |
| `scroll_coverage_result.json` / `test_scroll_coverage.py` | 滚动覆盖率测试 |
| `monitor_5b2ddc03.py` + `monitor_5b2ddc03.out` / `monitor_e6dd1a04.py` + `monitor_e6dd1a04.pid` + `monitor_e6dd1a04.out` / `long_monitor_5b2ddc03.out` | 运行监控脚本与输出（按店铺/任务 ID 命名的常驻监控） |

> 注：`tools/` 内 PNG/OUT/PID/JSON 为诊断产物，非运行必需；日常不碰。

### 2.4 `outputs/`（8 个跑出来的统计报告）

| 文件 | 作用 |
|---|---|
| `failure_reanalysis_report.csv` / `failure_reanalysis_report.md` | 失败重分析报告（csv + md） |
| `excel_image_cross_reference.txt` / `excel_image_cross_reference_v2.txt` / `excel_image_cross_reference_v3.txt` | Excel-图片交叉引用全量（v1~v3，txt 超大） |
| `excel_image_cross_reference_summary.csv` | 上述交叉引用摘要 |
| `verify_fixes_20260803_111100.log` / `test_all_models_20260803.log` | 修复验证 / 全量机型测试日志（2026-08-03） |

### 2.5 `panel_data/`（运行态数据）

**`panel_data/logs/`（7 个）**：
- `run_7970406_20260807_130256_d6d662f2.log`（今日最新运行日志，47KB）
- `diagnosis_findx9_model_overwrite.md`（Find X9→Pro 根因 + Fix A/C，5.8KB）
- `diagnosis_model_selection_25595926.md`（25595926 型号选不上归因，5.6KB）
- `publish_failure_analysis_25595926.md`（118 件失败按品牌/子原因分布，3.8KB）
- `launchd.out.log`（1MB，launchd 守护标准输出）
- `launchd.err.log`（1.5MB，launchd 守护错误输出）
- `.DS_Store`

**`panel_data/processed/`（9 个 `products_<店铺ID>.json`）**：各店铺已处理商品记录（去重/续跑依据）
- `products_7970406.json`（207KB，当前重点店铺）
- `products_25595926.json`（270KB）
- `products_168976032.json`（59KB）
- `products_636781.json`（8KB）
- `products_5576543.json`（5KB）
- `products_208501840.json`（5KB）
- `products_129675602.json`（8KB）
- `products_51165770.json`（5KB）
- `products_current.json`（88KB，当前店铺镜像）

> 续跑逻辑：`load_processed_log` / `save_processed_log` / `mark_product_done` / `mark_product_failed` / `mark_product_skipped_no_image`（§4.2）。

### 2.6 `assets/`（看板前端资源，3 个）

| 文件 | 作用 |
|---|---|
| `panel.css`（27KB） | 看板样式（深色星空主题，`panel.html` 引用） |
| `panel.js`（37KB） | 看板逻辑（轮询接口、SSE、渲染任务/错误、复制地址、开关自启） |
| `coder-sprite.png`（154KB） | 看板角落"coder"动图雪碧图 |

### 2.7 `_archive/`（75 个历史归档文件，**勿动**，仅留档）

图片目录整理/去重/命名规范化/型号匹配/上传/解析相关的**历史脚本与日志/截图**全部在此，例如：

`organize_dirs_by_standard.py` / `organize_dirs_by_standard_v2.py` / `organize_dirs_by_standard_v3.py` / `organize_images.py` / `reorganize_images.py` / `organize_by_brand.py`（目录标准化）；
`normalize_image_names.py` / `fix_white_bg.py` / `fix_white_bg_v2.py` / `fix_white_bg_only.py` / `dedup_white_bg.py` / `global_dedup_white_bg.py` / `global_near_dedup_white_bg.py` / `fix_dup_main_white.py` / `white_bg_less_than_3.py` / `check_source_white_bg.py` / `scan_main_white_imbalance.py` / `fix_missing_main_images.py` / `fix_main_images.py`（图片命名/白底图/主图处理）；
`test_parse_title.py` / `test_parse_fix.py` / `fix_parse_title.py` / `test_parse_title_debug.py` / `test_real_models.py` / `test_standard_models.py` / `test_all_503_models.py` / `test_all_511_image_dirs.py` / `test_model_matching.py` / `test_confusion_pairs.py` / `test_brand_model_fix.py` / `scan_abnormal_models.py` / `check_specific_models.py`（解析/型号匹配）；
`test_detail_upload.py` / `test_detail_upload_v2.py` / `test_upload_detail_v3.py` / `test_delete_detail_images.py` / `test_find_detail_images.py` / `test_find_detail_images2.py`（详情图上传）；
`audit_all_images.py` / `audit_summary.py` / `generate_run_report.py` / `generate_error_report.py` / `analyze_dir_diff.py`（审计/报告）；
以及大量 `*.png` 调试截图（`test_screenshot_01_initial.png` / `test_screenshot_02_task.png` / `test_screenshot_03_stopped.png` / `dropdown_menu.png` / `header_area.png` 等）、`brand_scan.json` / `brand_organization.json` / `image_dir_test_report.json` / `check_distribution.py` 等；
**`项目总结.md`**（15KB，早期项目总结，值得一读）。

### 2.8 顶层 `logs/`（额外运行日志，7 个）

`panel_server_boot.log` / `panel_server.out`（服务启动）；`test_output.log` / `test_switch.log` / `test_switch_account.log`（测试）；`debug_run.log`（54KB）/ `debug_run_model_fix2.log`（106KB）（调试）。

### 2.9 外部目录（不在 `doudian-panel/` 内）

| 路径 | 作用 |
|---|---|
| `/Users/atrenew/商品图片/` | 商品图片根。按 `店铺ID/3C数码及配件/二手手机/品牌/机型/主图/` + `/白底图/` 分层（详见 §3） |
| `/Users/atrenew/手机商详/` | 详情图通用库：3 张成色首图（`95新.jpg`/`99新.jpg`/`9新.jpg`）+ 12 张通用图（`1.jpg`~`13.jpg`），**全店铺共享，按成色选首图** |
| `/Users/atrenew/WorkBuddy/抖店自动化商品上架/` | 独立 workspace（仅 `.workbuddy/memory` 空壳） |

---

## 3. 商品图片目录结构（标准 + 真实数据缺口）

### 3.1 标准分层

```
商品图片/
└── <店铺ID>/                         # 如 7970406 / 25595926 / 636781（共 13 个店铺）
    └── 3C数码及配件/
        └── 二手手机/
            └── <品牌>/               # Apple/华为/三星/小米/OPPO/iQOO/真我/vivo/一加/荣耀 等
                └── <机型>/           # 如 "华为 Mate 70 Air"
                    ├── 主图/         # 应有 主图.png（单张，无序号）
                    └── 白底图/       # 白底图_1.png / 白底图_2.png ...（带序号）
```

### 3.2 命名规范（硬性，沉淀于 `product-image-naming-normalize` skill）

- 主图：`主图.png`（单张、不带序号；多张异常时 `主图.png` + `主图_1.png`）
- 白底图：`白底图_N.png`（带序号 N）
- 统一 **PNG** 格式（jpg/jpeg/jfif/bmp 须重编码为 PNG；个别机型出现 `白底图_3.jpg` 属不合规残留）
- **3:4 主图全局禁用**（只传 1:1 正方形），避免被平台拒
- 错位文件：放在 `白底图/` 内却命名为 `主图_*` → 按"所在文件夹决定命名"改为 `白底图_N.png`

### 3.3 各店铺统计

**顶层共 13 个店铺 ID**：`129675602 156450040 168976032 208501840 221724193 221729127 223523435 25595926 293816952 51165770 5576543 636781 7970406`

**店铺 7970406（当前重点）**：`3C数码及配件/二手手机/` 下 **10 个品牌，499 个机型文件夹**：

| 品牌 | 机型数 | 品牌 | 机型数 |
|---|---|---|---|
| Apple | 19 | iQOO | 47 |
| OPPO | 73 | vivo | 76 |
| 一加 | 19 | 三星 | 29 |
| 华为 | 58 | 小米 | 62 |
| 真我 | 41 | 荣耀 | 75 |
| | | **合计** | **499** |

**其他店铺二手手机品牌级**（部分店铺无 `二手手机` 目录）：
- `129675602`：OPPO iQOO vivo 一加 三星 努比亚 华为 小米 真我 红米 苹果 荣耀 魅族（12）
- `168976032`：OPPO Redmi iQOO vivo 一加 三星 华为 小米 真我 荣耀（9）
- `208501840`：OPPO iQOO vivo 一加 三星 华为 小米 真我 荣耀（8）
- `25595926`：Apple OPPO Redmi iQOO vivo 一加 三星 华为 小米 真我 荣耀 魅族（11）
- `5576543`：Apple Hi nova OPPO iQOO vivo 一加 三星 中兴 努比亚 华为 华硕 小米 摩托罗拉 真我 索尼 联想 荣耀 诺基亚 锤子 魅族 麦芒 黑鲨 鼎桥（22）
- `636781`：Apple OPPO iQOO vivo 一加 三星 华为 小米 真我 荣耀（9）
- `156450040 / 221724193 / 221729127 / 223523435 / 293816952 / 51165770`：无 `二手手机` 目录
- 注：`129675602 / 25595926 / 5576543` 还含 `电器/` `二手影音电器/` 等其他分类目录。

### 3.4 ⚠️ 关键数据缺口（接手人必读）

**扫描 7970406 全部 499 个机型文件夹结论：**
- `主图/` 子目录**全部为空（0 个机型含主图文件）** —— 主图图片目前**完全缺失**（子目录存在但无文件）。
- `白底图/` 子目录**仅 30 个机型含图片**，命名确为 `白底图_N.png`（个别为 `.jpg`）。
- 抽样 10 品牌首个机型（`主图/` 均空）一致印证。

**这意味着**：当前"免主图"是**设计行为**而非数据 bug——见 §4.3 / `panel_runner.apply_no_main_mode`：worker 子进程内把 `get_main_image_path` monkey-patch 成"无主图时用首张白底图顶替主图位"。所以跑批时主图位实际由白底图填充，货品靠白底图出图。**若未来要恢复"真实主图"，需两步走：① 关闭 monkey-patch（`SPECIAL_NO_MAIN=False` 或改 `apply_no_main_mode`）② 把主图文件补齐到 `主图/`。** 接手人不要误以为"主图空=脚本坏了"。

---

## 4. 核心脚本 `test_doudian_batch.py`（7598 行，101 个 def）

### 4.1 全局常量 / 字典（带行号 + 作用）

| 行 | 名称 | 作用 |
|---|---|---|
| 41 | `WORK_DIR` | 脚本所在目录 |
| 42 | `CHROME_USER_DATA` | `chrome_user_data`（浏览器用户数据） |
| 43 | `STORAGE_STATE` | `storage_state.json`（登录态） |
| 44 | `LOG_FILE` | `processed_products.json` |
| 47 | `LAST_PUBLISH_ERROR` | 最近发布错误（配合 get/set/clear 防误标） |
| 67 | `DRAFT_URL` | `https://fxg.jinritemai.com/ffa/g/draft`（草稿箱） |
| 71 | `BRAND_MAP` | 28 品牌 中文→平台标准名（如 `小米→Xiaomi/小米`） |
| 105 | `MODEL_EXACT_OVERRIDE` | 华为 `HUAWEI` 前缀等强制映射（模糊匹配兜底） |
| 125 | `PRODUCT_IMAGE_ROOT` | `/Users/atrenew/商品图片` |
| 127 | `IMAGE_BASE_DIR` | 图片基目录（空，运行时填） |
| 131 | `CURRENT_SHOP_ID` | 当前店铺 ID（运行时填） |
| 133 | `DETAIL_IMAGE_DIR` | `/Users/atrenew/手机商详` |
| 137 | `SHOP_MAPPING` | 13 店铺名→短 ID（见 §9） |
| 154 | `CATEGORY_PATTERNS` | 品类词过滤（二手手机/系列/截断残留"二"） |
| 169 | `SEO_KEYWORD_PATTERNS` | 搜索词过滤；**行 177 已修 `(5G版) 国行 全网通` 误抓"全网二"**：`r'\s*全网[二三四]?\s*'`（覆盖 全网/全网二/三/四） |
| 195 | `_5G_MODEL_PROTECT` | 5G 机型保护名单（`_restore_5g_model` 用） |
| 206 | `DEBUG_LOG_FILE` | `debug_run.log` |
| 223 | `_SNAP_COUNTER` | 截图计数（`snap`） |
| 948-949 | `_TITLE_CONDITION_BRACKET_RE` / `_TITLE_CONDITION_PLAIN_RE` | 成色正则（`【95新】` / `95新`） |
| 1115 | `BRAND_VERIFY_ALIASES` | 品牌校验别名 |
| 5398 | `SPEC_SWITCH_GUARD_SHOP_IDS` | **`{"636781"}`** —— 仅店铺 636781 触发规格开关强制关闭 guard |

### 4.2 全部顶层函数清单（101 个，按区段分组）

**错误/日志/截图**
- `50 get_last_publish_error` / `55 set_last_publish_error` / `61 clear_last_publish_error`：发布错误泄漏防误标
- `208 log(tag, msg)`：统一日志输出（STEP 标签在此）
- `225 snap(page, name)`：截图存盘（调试）

**图片路径**
- `245 get_image_dir(parsed)`：拼机型图片目录
- `443 get_main_image_path(parsed)`：取主图路径（**被 runner monkey-patch**）
- `454 get_aux_image_paths(parsed)`：取白底图（辅助图）列表（**被 runner monkey-patch**）
- `468 get_detail_image_paths(parsed)`：取详情图列表

**标题解析**
- `533 _strip_repeated_brand` / `559 _normalize_condition`（成色） / `579 _cleanup_model` / `617 _normalize_model_spacing` / `653 _restore_5g_model`（5G 保护）
- `687 parse_title`：**核心入口**，从标题拆品牌+型号+成色
- `871 normalize_title` / `890 read_category` / `934 _title_display_len` / `952 _trim_title_overlength`（超长截断） / `966 _write_title_value` / `993 read_and_normalize_title`

**品牌**
- `1081 probe_select_positions`：探测品牌/型号框坐标
- `1119 _brand_matches` / `1143 _display_brand_for_redmi`（Redmi 显示） / `1154 verify_brand` / `1194 change_brand`
- `1401 _ensure_title_suffix`（已禁用）

**型号选择（★最易出问题）**
- `1408 _locate_model_open_result`
- `1516 _try_select_model_with_keyword`：型号搜索+点击+验证**主函数**；内含 JS 函数 `findBest`（约行 1981-1998，`pre.sort((a,b)=>b.len-a.len)` 取最长变体 —— **Fix B 未实施**）
  - `model_cx/model_cy`（约 1534-1549）：**Fix A 已落地**——验证探针钉死型号框中心（pin.cx/cy），不再用被点击选项中心坐标
  - `verify` / `verify2`（约 2130 / 2210）：坐标无关重读型号框真实值，避免误读相邻"二手优品/店铺保修"框
- `2287 _strip_brand_tokens` / `2308 _model_core_token` / `2329 _norm_model_loose` / `2344 _is_acceptable_model_match` / `2386 _model_has_upgrade_field` / `2393 _selected_retains_upgrade` / `2411 _model_has_suffix_field` / `2443 _is_oppo_brand` / `2453 extract_oppo_pure_model` / `2494 _rewrite_oppo_model_value` / `2874 force_write_oppo_model`（OPPO 钉死 main）
- `3314 _pin_and_locate_model_box`：坐标 pin 定位型号框（防漂移）
- `3730 select_model` / `4038 ensure_brand_model`（品牌+型号总调度，返回 `model_value`）

**终检 / 图片上传**
- `4092 final_check`（终检/回看；**Bug4 根因在行 4289 `model in model_value`**）
- `4222 dismiss_popup` / `4273 delete_all_images` / `4342 delete_detail_images` / `4420 upload_images` / `4644 verify_upload` / `4692 upload_detail_images`

**规格开关 / 发布**
- `4890 select_is_original` / `5094 navigate_to_price_stock` / `5116 _find_spec_switch_rect_and_state` / `5274 close_spec_switch`（smallest-container + 中心坐标点击） / `5274 _probe_spec_switch_state` / `5311 _maybe_close_spec_switch_for_shop`（仅 636781） / `5325 save_draft`
- `5421 _click_publish_button` / `5476 _dismiss_attribute_optimization_popup`（关"商品属性优化"弹窗，点"不修改，继续发布"） / `5509 _detect_publish_result` / `5652 _try_fix_negative_stock`（虚拟滚动 `page.mouse.wheel` 修负库存） / `6074 _is_stock_error_reason` / `6086 _is_on_price_stock_area` / `6105 publish_product`

**续跑/去重**
- `6232 load_processed_log` / `6243 save_processed_log` / `6253 mark_product_done` / `6267 mark_product_failed` / `6281 mark_product_skipped_no_image`

**草稿箱扫描**
- `6297 scan_draft_page` / `6325 go_to_next_page` / `6446 collect_all_draft_ids` / `6528 _relocate_model_box_for_guard` / `6555 guard_model_before_publish` / `6700 refix_oppo_model_after_spec_switch`（规格开关后重钉 OPPO 型号）

**主流程**
- `6736 process_single_product`（单品主流程：品牌→型号→图→库存→规格开关→发布→终检）
- `7043 run_batch`（批量主流程，支持中途停止 `should_stop`）
- `7140 detect_shop_info` / `7260 switch_shop` / `7427 main`（程序入口）

### 4.3 单品主流程链路（接手人读代码主线）

```
main(7427)
 → run_batch(7043)                # 批量循环，支持 stop 信号
   → process_single_product(6736)  # 单品
     → ensure_brand_model(4038)    # 选品牌(verify_brand) + 选型号(_try_select_model_with_keyword / select_model)
     → upload_images(4420)         # 主图(可能=白底图顶替) + 白底图 + 详情图
     → navigate_to_price_stock(5094) → _try_fix_negative_stock(5652)
     → _maybe_close_spec_switch_for_shop(5311) → close_spec_switch(5274)  # 仅 636781
     → publish_product(6105)       # 关弹窗 + 点发布 + 检测发布结果
     → final_check(4092)           # ★ 终检（Bug4 所在）
```

**关键说明**：`panel_runner.py` 在子进程入口调用 `apply_no_main_mode()`（行 242），通过 monkey-patch 把核心脚本的 `get_main_image_path` / `get_aux_image_paths` 替换为"无主图用首张白底图顶替"版本（`SPECIAL_NO_MAIN=True`，全局生效，不再按店铺区分）。**该 patch 只作用于当前 worker 子进程，不改动 `test_doudian_batch.py` 源码、不污染其他店铺**——这是"最小改动"原则的典范实现，接手人若要调整主图逻辑，应在 `panel_runner.apply_no_main_mode` 改，而非改核心脚本。

---

## 5. 面板服务 `panel_server.py` / `panel_runner.py`

### 5.1 `panel_server.py`（Flask，17 个 HTTP 接口，端口 8080）

| 方法 | 路径 | 函数 | 作用 |
|---|---|---|---|
| GET | `/` | `index` | 面板主页 HTML（`panel.html`） |
| GET | `/album` | `album` | 相册/图片浏览页 |
| GET | `/assets/<path>` | `serve_assets` | 静态资源（js/css/png）转发 `assets/` |
| GET | `/api/shops` | `api_shops` | 店铺列表 |
| GET | `/api/tasks` | `api_tasks` | 批量任务列表（状态/进度） |
| GET | `/api/task/<tid>/logs` | `api_task_logs` | 某任务日志 |
| GET | `/api/task/<tid>/errors` | `api_task_errors` | 某任务错误明细 |
| GET | `/api/errors/all` | `api_errors_all` | 全部任务累计错误 |
| POST | `/api/run` | `api_run` | 启动一次批量任务（拉起 `panel_runner.py` 子进程） |
| POST | `/api/stop` | `api_stop` | 停止运行中的任务 |
| DEL | `/api/task/<tid>` | `api_remove` | 删除某任务记录 |
| POST | `/api/clear-logs` | `api_clear_logs` | 清空全部日志 |
| POST | `/api/clear-task-logs` | `api_clear_task_logs` | 清空任务级日志 |
| GET | `/api/network` | `api_network` | 本机网络信息（局域网 IP/主机名） |
| GET | `/api/autostart/status` | `api_autostart_status` | 开机自启（launchd）状态 |
| POST | `/api/autostart` | `api_autostart` | 开/关开机自启（调 `launchctl`） |
| GET | `/api/stream` | `api_stream` | SSE 事件流，实时推送进度/日志到前端 |

辅助函数：`get_network_info()`（584）/ `_pids_on_port(port)`（631，端口占用检测）/ `main()`（731）。

### 5.2 `panel_runner.py`（worker，被 server 子进程拉起）

- 入口 `main()`（528）：读环境变量 → 启动 chromium（1440×900）→ `run_batch_with_stop`
- `run_batch_with_stop`（344）：批量主循环，`should_stop()`（182）轮询停止信号（`panel_data/stop`）
- 结构化事件输出（SSE 源）：`emit` / `emit_log` / `emit_progress` / `emit_state` / `emit_errors`（75~166）
- `classify_image_error`（124）：图片类错误归类
- `apply_no_main_mode`（242）：**免主图 monkey-patch**（见 §3.4 / §4.3）
- `ensure_login`（292）：登录态检测与保活（`is_login_page` / `is_draft_page`）
- `panel_log`（211）：本地日志
- **无 HTTP 服务**，仅作 worker。

### 5.3 启动脚本与 launchd 守护

| 脚本 | 关键命令 |
|---|---|
| `启动面板.command` | `PLIST=~/Library/LaunchAgents/com.atrenew.doudian-panel.plist`；若 `launchctl list` 已有则直开 `http://127.0.0.1:8080`；否则 `nohup /usr/bin/python3 panel_server.py > panel_server_boot.log 2>&1 &`；打印本机/局域网/主机名地址并 `open` |
| `安装开机自启.command` | `launchctl bootout` 旧实例 → `launchctl load "$PLIST"`（开机自启+崩溃自恢复）；校验 8080 监听 |
| `卸载开机自启.command` | `launchctl bootout` 停止移除守护（不删 plist） |
| `设置固定IP.command` | 候选 `172.16.6.66/.88/.166/.199/.250` 挑首个空闲（ICMP+ARP）→ `sudo networksetup -setmanual "Wi-Fi" <IP> 255.255.255.0 172.16.6.1` + DNS `223.5.5.5 119.29.29.29` |
| `恢复自动IP.command` | `sudo networksetup -setdhcp "Wi-Fi"` + 清空 DNS |

> plist：`Label=com.atrenew.doudian-panel`，`Program=/Users/atrenew/doudian-panel/panel_server.py`，`WorkingDirectory=/Users/atrenew/doudian-panel`，`DP_HOST=0.0.0.0`，`RunAtLoad=true`，`KeepAlive=true`。

---

## 6. 日志体系

### 6.1 命名规则

`panel_data/logs/run_<店铺ID>_<YYYYMMDD>_<HHMMSS>_<hash>.log`
例：`run_7970406_20260807_130256_d6d662f2.log`（店铺 7970406，2026-08-07 13:02，本次运行 hash）。按日期+店铺找对应文件。

### 6.2 日志标记（排查用）

- 成功：`发布商品成功` / `处理完毕` / 结尾 `成功: N | 失败: M`
- 失败：`发布失败` / `存草稿` / `ERROR` / `终检未通过`
- 进度标签（log tag）：`[INIT]`（初始化，含"🔧 全局启用「免主图」特例"） / `[STEP0]` / `[SWITCH]` / `[SCAN]` / `[BATCH]` / `[MODEL]` / `[UPLOAD]` / `[VERIFY]` / `[STEP8/9/10]` / `[CHECK]` / `[MODEL]` 等
- OPPO 相关：`OPPO` 高频出现；`型号框中心`（model_cx/model_cy 探针）；`OPPO 重新固定型号结果`

### 6.3 三份诊断 MD（已沉淀，遇到老问题先翻）

1. **`diagnosis_findx9_model_overwrite.md`**（Find X9→Pro 根因，见 §7.2）
2. **`diagnosis_model_selection_25595926.md`**：25595926 型号选不上归因
   - 整轮统计（375 件）：搜索框正确定位 **0 次**；搜不到搜索框 **898 次**；误扫到**保修下拉**（全国联保/原厂在保/店铺保修）**869 次**；真正扫到目标型号 **15 次**；终检通过 291 / 未通过 67
   - 结论：搜索框从未被定位，兜底 `querySelectorAll('[role=listbox]')` **全页扫描误读保修下拉** → 型号永远不在选项 → 终检拦截存草稿
   - A 类（误扫保修下拉）：华为 Mate X7/X5 典藏版、三星 S25 Edge/S24+；B 类（选项扫到但回弹）：小米 13 Pro、REDMI K80（JS 合成事件未进 React state，重渲染重置）
   - 坐标漂移证据：小米 13 Pro `pin` 重定位后型号框坐标 (926,494) 与空间探测 (692,621) 不一致
   - 建议修复（未实施）：兜底 listbox 解析只认型号 select 自己的 listbox（`aria-controls` 或水平中心对齐），永不误读保修下拉
3. **`publish_failure_analysis_25595926.md`**：118 件失败分布
   - 品牌降序：华为 47 / 三星 37 / 小米 30 / 一加 3 / 荣耀 1
   - 子原因：型号终检未通过 75 / 标题过长 19 / 库存类 15 / hover 超时 6 / 未捕获详情 3
   - 标题过长例：`Mate XT 非凡大师` 标题 >60 字符被拒（平台限 30 汉字）；库存类：预留库存>0 无法清零

### 6.4 `panel_data/processed/`

各店铺已处理商品 JSON（`products_<店铺ID>.json`），用于去重/续跑。`products_current.json` 为当前店铺镜像。

---

## 7. 当前进度与卡点（截至 2026-08-07）

### 7.1 已修复 / 已落地

- ✅ `parse_title` 全网二误抓（行 177，`全网[二三四]?`）
- ✅ 负库存虚拟滚动 `page.mouse.wheel`（`_try_fix_negative_stock` @5652，`ecom-g-table-virtual` 虚拟滚动漏行，荣耀 200 ID `3832638943254413756` 验证通过）
- ✅ 3:4 主图全局禁用（只传 1:1）
- ✅ **Fix A**（验证探针钉死型号框中心，`model_cx/model_cy` @1534-1549，verify/verify2 改用，行 2185/2261 附近）
- ✅ **Fix C**（OPPO 钉死 gate 归一化比较：`_strip_brand_tokens` 剥离前缀后小写比对，页面值已一致则跳过重写）
- ✅ 主图缺失兜底（白底图顶替，**通过 `panel_runner.apply_no_main_mode` monkey-patch 实现**，全局生效）
- ✅ 规格开关自动关闭（`close_spec_switch` @5274，仅店铺 636781 触发 guard）

### 7.2 纯诊断、未修复（用户红线：先诊断不修复）

**Bug2 — OPPO 钉死 rewrite 失败（Find X9→Find X9 Pro）**
- 现象：OPPO Find X9 先选对，脚本又改成 Find X9 Pro；终检却"通过"（因宽松包含判断）。
- 完整时序（日志 `run_7970406_20260806_112111_ab14ae03.log`）：关键字 `[Find X9, Find]` → OPPO 钉死反推纯型号 `Find X9`（正确）→ 强制写入失败（form-instance-not-found）→ 回退通用搜索 → 点 `Find X9`（正确）→ **验证误读相邻"二手优品"框**（选项中心坐标）判失败 → 回退广关键字 `Find` → `findBest` 取最长变体 → 选 `Find X9 Pro` → 终检 `Find X9 Pro 包含 Find X9` 宽松通过。
- 根因①（主因）：`_try_select_model_with_keyword` 验证块用**被点击选项中心坐标**而非型号框中心（与 25595926 同源缺陷）。
- 根因②（次因）：`findBest`（JS，约行 1981-1998）`pre.sort((a,b)=>b.len-a.len)` 取最长，忽略 `originalModel`。
- 修复状态：**Fix A（治本·探针钉死型号框中心）已落地**；**Fix B（`findBest` 优先精确匹配 `original_model`，约行 1840 声明后从未在 findBest 内使用）未实施**（当前 A+C 已覆盖本场景，留作加固）；**Fix C（OPPO gate 归一化）已落地**。

**Bug4 — 终检宽松放行错误型号**
- 位置：`final_check` @4182，内部 **行 4289 `if model and model in model_value:`** 用"包含"判断。
- 后果：`Find X9 Pro` 包含 `Find X9` → 错误型号被放行通过终检。
- 状态：根因已定位，**未改**（符合"先诊断不修复"红线）。修复方向：改为精确相等或归一化等价比较（与 Fix C 同类 `_strip_brand_tokens` 思路）。

### 7.3 已知待办（非阻塞）

- 标题过长（>30 汉字）被平台拒 → 需截断（`_trim_title_overlength` @952 已存在但阈值/平台限需对齐）
- 预留库存 >0 无法清零 → 库存类失败（需人工或标记）
- 三星 S26 Ultra / S24 Ultra 等 hover 超时 → 稳定性（Locator.hover 参数）
- 图片规范性：个别机型 `白底图_3.jpg` 非 PNG、命名不标准 → 需定期整理（见 `product-image-naming-normalize` skill）

---

## 8. 调试与维护 SOP（不伤原架构）

1. **先读日志，再动代码**：90% 问题看 `panel_data/logs/` 对应日期日志即可定位（§6.2 关键词）。
2. **最小改动**：只改出问题的必要逻辑段；文件名/标题类改动只改文件名/标题本身。
3. **先诊断后修复**：复杂问题先出根因报告（文件+函数名+大概行号+为什么错），确认后再改。卡点 Bug2/Bug4 当前即"只诊断"状态。
4. **改前备份**：动 `test_doudian_batch.py` 前复制一份 `test_doudian_batch_备份_日期.py`。
5. **小批量验证**：用面板"测试模式（仅几个）"跑 3~5 个验证，通过再全量；确认未把原本好的功能弄坏。
6. **日志结构化排查**：`tools/analyze_log_failures.py` / `analyze_publish_failures.py` 出失败归因。
7. **免主图逻辑改动在 runner 不在核心脚本**：若调主图/白底图兜底，改 `panel_runner.apply_no_main_mode`（monkey-patch 处），不要碰 `test_doudian_batch.py` 的 `get_main_image_path`。

### 8.1 红线（绝对不可碰）

- ⛔ 禁止修改登录/鉴权逻辑（`requireAuth` / OAuth scope）—— 改了可能整个登不上
- ⛔ 禁止为通知接入任何第三方渠道（微信/邮件等）；附件/提醒仅依赖抖店任务助手原生
- ⛔ 修复仅限数据/服务层，不碰鉴权层
- ⛔ 改核心脚本前必须备份；勿改 `_archive/`（历史归档）

---

## 9. 店铺与品牌速查

**SHOP_MAPPING（13 店铺）**：
`129675602` 爱回收官方旗舰店 / `636781` 爱回收二手手机旗舰店 / `7970406` 爱回收严选二手数码 / `51165770` 爱回收严选二手优品 / `25595926` 拍拍二手悦玥手机专卖店 / `168976032` 拍拍严选手机优品 / `5576543` 拍拍严选二手手机 / `156450040` 拍拍二手数码 / `208501840` 返航新生二手手机旗舰店 / `223523435` 悦玥潮品生活坊 / `221724193` 爱回收优品二手手机数码 / `221729127` 拍机堂Mall线上二号店 / `293816952` 新生严选二手手手

**当前调试店铺**：`7970406`（爱回收严选二手数码）。历史：`25595926`、`636781`。
**测试商品**：荣耀 200（ID `3832638943254413756`）。

---

## 10. 长期优化路线（建议）

1. 型号/店铺/品牌映射从"代码写死"改为外部配置文件（运营可自助改，不依赖写代码的人）
2. 日志结构化导出每日失败归因表（自动统计 TOP 失败原因）
3. 失败自动分类 + 一键重试，避免整轮重跑
4. 图片目录规范性自动检查+修复脚本固化（统一 PNG、命名标准）
5. 看板加失败率告警（店铺失败率突增提醒）
6. 卡点 Bug2（Fix B：`findBest` 优先 `original_model`）/ Bug4（终检 `model in model_value` 改精确/归一化比较）在确认后正式最小改动修复
7. 主图数据补齐：若恢复"真实主图"，需关 `apply_no_main_mode` 的 monkey-patch 并补 `主图/` 文件（见 §3.4）

---

## 11. 交接给 AI 的关键上下文（记忆锚点）

- **用户**：爱回收营销运营部 赖德才（atrenew），macOS，中文沟通，技术能力自评有限，偏好**根因+修复+验证**三段式、结构化表格、分步教程（用户/AI 分栏）。
- **协作偏好**：代码改动最小化（只更必要逻辑，文件标题类改动只改标题/文件名）；报 bug 流程 = 日志现象 → 读具体行号定位；每轮修复后校验+部署+追加日报；通过 WorkBuddy 共享链接恢复会话时以**本地磁盘项目状态**为准。
- **当前最活跃工作线** = 抖店批量上架排障（7970406）；Bug2/Bug4 进入纯诊断阶段（用户明确"暂不负责修复，只做诊断"）。
- **抖店 UI 控件警示**：同高同行控件 x 偏移不同（型号框 vs 品牌/成色框），AI 操作必须避免误触相邻控件 —— 这是 Bug2 / 25595926 选不上问题的共同物理根因。
- **相关历史对话 ID**（运营对话检索）：`d337d262`（25595926 118 件归因） / `39e194f7`（7970406 三品逐品诊断） / `3c679da0`（Excel 对账/OPPO 选型 Fix A/C） / `1d020dcc`（图片改标题重命名） / `7496a6e9`（负库存虚拟滚动）。
- **配套文档**：`交接文档/抖店批量上架_交接手册_运营版.html`（小白运营版，亮色主题）。
- **关键文件指针（快速定位）**：
  - 终检宽松 Bug4 → `final_check` @4182，行 4289 `model in model_value`
  - 型号选择主函数 → `_try_select_model_with_keyword` @1516（findBest JS ~1981）
  - OPPO 钉死 → `force_write_oppo_model` @2874 + gate `_strip_brand_tokens` @2287
  - 规格开关 → `close_spec_switch` @5274（guard 仅 `636781` @5398）
  - 免主图 monkey-patch → `panel_runner.apply_no_main_mode` @242（`SPECIAL_NO_MAIN=True`）
  - 主流程 → `process_single_product` @6736 → `run_batch` @7043 → `main` @7427

---

*本文档覆盖：目录全量文件清单（含 _archive 75 个 / tools 37 个 / tests 14 个 / outputs 8 个 / panel_data 16 个）、主脚本 101 个函数+11 常量带行号、面板 17 接口、启动脚本命令、商品图片 13 店铺统计+主图空缺口、三份诊断报告核心、Bug2/Bug4 根因与修复状态、调试 SOP 与红线。如需某函数逐行解读，直接指定函数名+行号即可。*
