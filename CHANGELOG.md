# 更新日志

本项目的所有重要变更都记录在此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

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
