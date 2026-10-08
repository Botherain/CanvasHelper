# 更新日志

本项目的所有重要变更都记录在此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增 · 应用图标

- 新增 `docs/icons/canvashelper.ico`（16/24/32/48/64/128 BMP + 256 PNG 七档）
  与 `docs/icons/canvashelper.png` 预览图，README 顶部同步展示。
  小档走 BMP 是因为 GDI / .NET 的 `Icon.ToBitmap` 只认 BMP，收到 PNG 帧会画成
  一片噪点；256 走 PNG 是为了省体积（BMP 那一档要 260 多 KB，而 shell 从 Vista
  起本来就优先取 PNG）。Pillow 的 `bitmap_format` 只是「整份 save」的开关、不支持
  逐帧，所以先各存一份再按帧拼装。
- `scripts/make_icon.py`：裁掉源图四周边框 → 取中心正方形 → **去外圈、留圆角**
  → 归一到 256px → 写出多尺寸 ico；`--verify` 可离线体检，解析 ico 目录逐档列出
  尺寸与编码方式。
  源图画的是一张带投影的圆角卡片、四角还剩一圈底色：脚本按「与底色的偏差」量出
  卡片外接矩形裁紧（外圈彻底截掉），再用卡片自身的圆角半径画一个 4 倍超采样的
  抗锯齿遮罩当 alpha——圆角保住、四角透明，于是图标压在浅桌面不发灰、压在深
  桌面不发黑。卡片量不出来时退回整幅原样，不会裁成空白。
- `scripts/make_shortcut.ps1`：生成桌面快捷方式并挂上该图标，显示名固定为
  `CanvasHelper`。目标是
  `pythonw.exe -X utf8 -m canvas_helper gui`——`.bat` 本身无法承载图标，而且双击
  `.bat` 时 Windows 必先给 cmd 一个控制台（实测约 0.2 秒黑框，`.bat` 内部消不
  掉），直启 `pythonw` 则一次窗口都不开。`.lnk` 含本机绝对路径，仓库只收生成
  脚本，`*.lnk` 已进 `.gitignore`。
- PyInstaller 打包命令加上 `--icon docs\icons\canvashelper.ico`（入口同时订正为
  `scripts\pyinstaller_entry.py`），`docs/RELEASING.md` 同步。

### 修复 · 命令行启动不再伴随黑框

- `启动Canvas助手.bat` 改为 `start` + `pythonw.exe`：原先 `python.exe` 前台阻塞，
  cmd 黑框与主窗口同生命周期地挂着；现在 cmd 立即返回、`pythonw` 没有控制台，
  启动过程只剩主窗口本身。
- `.bat` 全文改纯 ASCII——`chcp 65001` 会在文件中途切换代码页，中文 `rem` 注释
  被从行中重新解析、尾部当成命令执行；与 `capture_gui.ps1` 是同一条约定。
- 计划任务命令去掉 `cmd /c`：改为 `pythonw.exe` + `scripts/headless_entry.py`，
  入口用 `__file__` 定位源码目录，起始目录不再影响导入，每日同步不再闪黑框。
- `scripts/capture_gui.ps1` 的进程定位改按命令行匹配任意 `python*`，不再写死
  `python.exe`，因而也能截到 `pythonw.exe` 拉起的窗口。

### 修复 · GUI 运行期间不再闪黑框

用户反馈「命令行在测试中经常弹出又自动消失」。用 20ms 轮询顶层窗口的探针定位到
真正的来源：GUI 跑在**没有控制台**的 `pythonw` 里，此时裸调 `tasklist` /
`taskkill` / `cmd`，Windows 会给每个子进程**新建一个可见控制台**，子进程一结束窗
口立刻销毁——于是：

- `deskbox.py` 的 `is_running()`，而 `_refresh_deskbox_state` 每 4 秒调它一次
  → 黑框每 4 秒闪一次，实测每个 0.19~0.41 秒；
- `gui.py` 三处 `cmd /c start`（打开 Canvas / 导出的 `.ics` / 通知链接）
  → 点一下闪一个。

改法：

- `deskbox.py` 的 `tasklist` / `taskkill` / `Popen(DeskBox)` 全部加
  `CREATE_NO_WINDOW`；
- `gui.py` 改用 `os.startfile()`（ShellExecute，根本不派生进程），`subprocess`
  依赖随之移除；ruff 放行 `S606`，理由与已有的 `S603` / `S607` 同一批；
- 桌面快捷方式改为直启 `pythonw`（见「应用图标」一节）——`.bat` 自带的那约 0.2 秒
  控制台无法从 `.bat` 内部消除，只有绕开它才行。

复验（同一场景、同一探针，修复前后对比）：

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| 双击 `.bat` 启动、GUI 随后跑十几秒 | 4 个（1 个 `.bat` 自带 + 3 个 `tasklist`） | 1 个（只剩 `.bat` 自带的） |
| 桌面快捷方式启动、GUI 跑 14 秒 | — | **0 个** |

### 改进 · 通知不再一直保留

- **通知正文彻底不落盘**。旧版把每次拉到的通知写进 `state.json` 的
  `notices_cache`，而公告接口 `/api/v1/announcements` 返回的是**全部历史**——
  于是只要登录过，公告就从首次同步起一直攒在本地（实测 **25 条、跨 40 天、
  占 12 972 字节**，约 state.json 的三分之一）；课程内容却是每次现拉、不带历史。
  现在 `state.json` 只留**已读标记** `notices.seen`（一串 key，用于判定未读），
  正文只活在内存里，由界面用最近一次拉取的结果渲染。
- **打开 GUI 时现拉一次**（`_startup` 自动触发），与手动点「刷新通知」同一条路径。
  登录态已知过期时**跳过自动拉取**，免得刚开窗口就弹出浏览器登录框——
  那种情况下顶部横幅本来就在提示了。
- 同步完成后用 `SyncResult.notices` 把本轮结果带回界面刷新；
  该字段用 `None` 表示「本轮回调没拉」（失败或关掉了通知聚合），
  与「拉到 0 条」区分开，避免误清空表格上已有的内容。
- **老版本的残留自动消失**：`mark_notices_read()` 是同步与刷新两条路径的必经
  写入点，在那里顺手 `pop("notices_cache")`，不用手动清理。
- 一键清理项改名为「下载状态 / 已读标记」，扫描时若发现旧版通知缓存会照实
  报出并一并抹掉；勾选清理后通知会被重新标为未读。
- 实测（本机 `state.json`）：**36 818 → 23 846 字节（-35%）**，
  跑一轮 `collect_notice_list()` 后体积不回弹，`notices_cache` 保持 absent，
  `notices.seen` = 25、`unread = 0`，已读判定不受影响。
- 新增 `tests/test_notices.py`（5 项离线回归）锁住新行为：正文与标题不进
  文件、老残留被抹掉、未读判定不变、通知条数涨十倍而 state 顶层键不变、
  `SyncResult.notices` 默认 `None`。

### 改进 · 文件发现

- **课程文件改用五条来源按序采集并互相降级**，不再依赖单一接口。
  新增来源：模块内 Page 的正文、公告 / 讨论正文。
- 修掉一个真实遗漏：CS0003 / CS0005 这类课的课程级 `/files` 返回 **403**、
  逐文件夹列文件**也** 403（46/46 全被挡）、模块里没有一条 File 型条目，
  而模块条目的 `content_details` 又是空的——课件全挂在模块内的 Page 正文里，
  此前完全抓不到。现在用模块条目自带的 `page_url` 调页面 show 接口取正文
  （该接口实测 14/14 与 12/12 均 200），必要时退回抓 `html_url` 源码。
  实测净增：**CS0003 21 → 58（+176%）、CS0005 9 → 41（+356%）**，
  CS0001 保持 2061 无回归。
- 单文件 `/files/{id}` 在索引 403 时仍可读，因此每条新挖出的文件都能补齐
  `folder_id` → `folder_path` 拿到率 **58/58、41/41**，归档分类不会因为
  索引被挡而退化成全进 `6.Others`。
- 死链（`/files/{id}` → 404）与 hidden / locked 文件容错跳过，不中断整轮收集；
  同一文件被多来源命中时按 `file_id` 去重，不重复下载。
- 新增 `tests/test_canvas_api.py`（7 项离线回归）锁死这条退化路径：
  索引 403 时从页面正文取文件、show 404 退回 HTML、正文抓取缓存、死链跳过、
  索引与页面来源去重、讨论区来源、文件 id 正则不误伤 `/api/v1/files/`。

## [1.0.0] - 2026-10-06

首个完整版本。功能、质量与分发三条线同时到位。

### 新增 · 同步核心

- **一次登录长期复用**：内嵌浏览器（Chromium，跟随系统 Edge）登录一次，
  cookie 与 profile 持久化在 `%LOCALAPPDATA%\CanvasHelper\profile\`，掉线自动重弹登录窗。
- **课程自动匹配**：运行时经 `/api/v1/courses` 拉取全部课程并多选，
  用课程代码匹配本地既有文件夹（`CS0001 → CS0001 - Vision and Image`），6 门课零手工配置。
- **按既有目录规则归档**：`1.Lecture session / 2.Tutorial session / 3.Assignments /
  4.Projects / 5.Books|5.Useful links`，未匹配的一律进 `6.Others`——**不丢弃任何文件**。
  子目录跟随本地现状：已有的 `Assignment_1` 不会再建 `Assignment1`，
  `Week` 编号靠目录与文件名双向推断。
- **文件只增不删**：同名同大小跳过，同名不同大小追加 `(1)`，
  且改名后的副本下次仍能被认成"本地已有"，不会反复下载。
- **dry-run 下载计划**：先出计划再执行，执行前能看到每个文件落在哪。

### 新增 · 截止与提醒

- **DeskBox 待办写入**：按"先写好副本 json → 关闭 DeskBox → 原子替换（留 `.pre-canvas` 备份）
  → 拉起 exe"的顺序写入，把 DeskBox 的关闭时间压到最短。
- **已完成作业不重复写入**：已标记完成的草稿跳过；完成后又被删除的靠记录不再复活；
  截止时间变了则重新提醒。
- **截止日历 `.ics`**：可导入任意日历客户端，默认提前一天提醒。
- **每日定时同步**：Windows 计划任务，默认每日 08:00。
- **通知聚合 / 成绩回写 / 每周 Markdown 学习摘要**。

### 新增 · 界面与运维

- tkinter 图形界面：课程多选、同步进度、待办预览、一键跳转 Canvas、
  **一键清理缓存**（默认不勾登录态与排队待办，扫描阶段不删任何东西）。
- **登录过期不再静默失败**，三层策略：
  1. 后台可见窗口自愈（`popup_on_expiry=true`，`popup_timeout_s=180`）；
  2. 救不回则标记 `auth_needed`，`sync --headless` 退出码 `2` 并写 ERROR 日志；
  3. GUI 启动时顶部横幅提示"点击重新登录"。
- 退出码契约：`0` 成功 / `1` 有失败 / `2` 需要重新登录。

### 修复

- **计划任务在 Windows 任务计划程序下定位不到脚本**：任务默认从 `System32` 起跑，
  `_cmd()` 现在显式 `cmd /c cd /d "<项目目录>" && "<解释器>" -m canvas_helper <子命令>`。
- **登录态已过期时后台自愈判断错误**：原判定依赖 `SESSIONNAME`，
  而计划任务环境里它是 `null`——即使确实在交互会话也不会弹窗。
  改用内核会话号（`ProcessIdToSessionId` / `WTSGetActiveConsoleSessionId`）判定，
  在真实计划任务里端到端复验通过（弹窗出现、`last_result=0`、登录态自愈、同步完整跑通）。
- **硬编码路径**：可写数据全部迁至 `%LOCALAPPDATA%\CanvasHelper\`，
  项目目录变成只读，可整体移动位置。
- `self.busy` 与 tkinter 自带的 `busy()` 方法撞名，改名 `_busy`。
- 一键清理对话框里 `command=lambda: _update_total()` 靠"调用时才查名"侥幸成立，
  函数上移后不再依赖延迟求值。

### 打包与质量

- `pyproject.toml` 作为**唯一**元数据来源，版本 `1.0.0`，
  与 `canvas_helper.__version__` 由自检断言保持一致。
- 入口点：`canvas-helper`（CLI）/ `canvas-helper-gui`（图形界面）。
  零额外运行时依赖，只用标准库 tkinter。
- **ruff**：显式规则集（`E/W/F/I/UP/B/SIM/C4/PIE/PERF/RUF/FURB/S/PLW`），
  每条 ignore 都写明理由，全绿。
- **mypy**：`check_untyped_defs`，17 个源文件零问题；
  `tests/` 因鸭子类型替身设计显式声明 `ignore_errors`。
- **pytest**：129 项离线断言拆成 13 个小节用例，小节之间靠夹具解耦，
  `pytest -k test_7` 可单独执行；脚本入口 `python tests\smoke.py` 与 pytest
  共用同一份实现，不会分叉。
- **CI**：`lint`（ruff + mypy）+ `smoke`（Python 3.11/3.12/3.13/3.14）×
  脚本入口与 pytest 双跑，Windows runner。
- 计划任务在真实环境跑通验证，`schedule.enabled` 默认仍为 `False`（不替用户做决定）。

[未发布]: https://github.com/Botherain/CanvasHelper/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Botherain/CanvasHelper/releases/tag/v1.0.0
