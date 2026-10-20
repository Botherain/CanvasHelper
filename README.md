# CanvasHelper · Canvas 课程同步与学习辅助

[![CI](https://github.com/Botherain/CanvasHelper/actions/workflows/ci.yml/badge.svg)](https://github.com/Botherain/CanvasHelper/actions/workflows/ci.yml)

访问你学校的 Canvas（LMS），读取课程内容与通知，按你已有的目录规则归档到
`<课程根目录>\<课程>\`，并把 tutorial / assignment / project 的截止时间
自动写入 **[DeskBox](https://github.com/Tianyu199509/DeskBox) 待办**。

> 本工具面向**个人学习用途**，请勿用于批量抓取或再分发课程内容。
> 与 Instructure / Canvas 及你所在院校无任何关联。见文末 [许可与免责](#许可与免责)。

<p align="center">
  <img src="docs/icons/canvashelper.png" alt="应用图标" width="96">
</p>

## 功能

| 模块 | 说明 |
| --- | --- |
| 课程同步 | 自动识别在读课程 → 勾选 → 拉取模块/文件/页面 → 归档到 `1.Lecture session` / `2.Tutorial session` / `3.Assignments` / `4.Projects` / `5.Books\|Useful links` / `6.Others` |
| 讨论区内容 | 帖子正文/回帖里的 **Colab / GitHub 链接**转 raw 直链下载（教程笔记本不再手动另存），帖子本身存 HTML 快照 |
| 待办写入 | 解析 Canvas 截止时间 → **先写副本 `todo.json.new`** → 关闭 DeskBox → 原子替换 → 立即重启（停机约 1 秒），并附带 Canvas 原链接 |
| 通知聚合 | 各课公告 + Activity Stream，可直接跳转原页；**通知正文只在内存里，打开时现拉一次**，`state.json` 只留已读标记 |
| 截止日历 | 一键导出 `.ics`，可导入 Windows / 手机日历（含提前 1 天提醒） |
| 成绩追踪 | 读取 Canvas 已出分项目与当前总评分，回写每门课的 `GradeNeed.txt` |
| 每日同步 | Windows 计划任务每天定时后台同步（无窗口，用已保存的登录态） |
| 每周摘要 | 每周生成 `本周摘要-YYYY-Wnn.md` 到各课程文件夹 |
| 缓存清理 | **一键清理**下载状态、已读标记、导出日历、日志、浏览器缓存（默认不动登录态） |
| 完成去重 | 已完成的作业**不会**再次写入；同名同截止的重复条目在写入时自动清理 |

## 快速开始

四步从零到可用，照抄即可（前置条件：Windows 10/11、Python 3.11+；浏览器用系统
自带的 Edge——Playwright 直接驱动它，**不用另下浏览器**）。

### ① 取得项目文件

```powershell
git clone https://github.com/Botherain/CanvasHelper.git
cd CanvasHelper
```

没有 git 也行：仓库页面 → **Code → Download ZIP**，解压后进入该目录。

### ② 环境配置（装依赖）

```powershell
py -3 -m pip install -e .        # 没有 py 启动器就写 python -m pip install -e .
```

运行时依赖只有 `playwright` 与 `tzdata`，pip 会一并装好。要跑自检 / 静态检查再补
一句 `py -3 -m pip install -e ".[dev]"`（pytest / ruff / mypy / Pillow）。

### ③ 首次启动（自动生成配置与数据目录）

```powershell
canvas-helper              # 安装后的命令行入口
# 或（在源码目录里）
python -m canvas_helper
# 或
.\启动Canvas助手.bat
```

**不需要手写任何配置**，首次运行会创建：

| 生成的文件 | 作用 |
| --- | --- |
| `%LOCALAPPDATA%\CanvasHelper\config.json` | 配置（课程根目录、DeskBox 路径），设置页可改 |
| `%LOCALAPPDATA%\CanvasHelper\state.json` | 同步状态：已下载文件、已读通知、已完成待办 |
| `%LOCALAPPDATA%\CanvasHelper\sync.log` | 运行日志 |
| `%LOCALAPPDATA%\CanvasHelper\session.json` + `profile\` | 登录态，详见 [安全与隐私](#安全与隐私) |

同时弹出 Edge 登录窗口，完成学校 SSO / 2FA 即可；之后长期免登录，掉线时会自动
重新弹窗。

### ④ 桌面快捷方式（带图标、全程无控制台）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
```

生成的快捷方式显示名 `CanvasHelper`、挂上应用图标，直接以 `pythonw` 启动 GUI。
`.bat` 本身无法承载图标，且双击时 Windows 必先给它一个控制台（约 0.2 秒黑框），
所以快捷方式不走 `.bat`。

装好之后还有两个小命令：

```powershell
canvas-helper --version          # 版本号
canvas-helper sync --headless    # 后台同步（计划任务用的就是它）
```

> 想让它每天自动跑：**设置** 页勾选「启用每日自动同步」并点「保存设置」，
> 之后每天后台同步一次（无窗口，用已保存的登录态）。

### 首次配置

程序不需要预先写配置——四项会自动/引导处理：

| 配置项 | 默认行为 |
| --- | --- |
| `canvas_home` Canvas 站点地址 | **必填**：在「设置」页填你学校的 Canvas 地址（如 `https://canvas.example.edu`），未填写时同步会明确报错 |
| `classes_root` 课程根目录 | 取 `<用户目录>\CanvasCourses`，在「设置」页**浏览…**改成你的目录 |
| `timezone` 时区 | 留空 = **跟随本机系统时区**；要固定就填 IANA 名（如 `Asia/Hong_Kong`、`America/New_York`、`Asia/Shanghai`） |
| `course_code_pattern` 课程代码正则 | 留空 = **自动识别**（字母前缀 + 数字）；学校代码形态古怪时才需要填 |
| `deskbox.todo_path` | 自动扫描 `%LOCALAPPDATA%\DeskBox\data\widgets\*\todo.json`，取条目最多的那个 |
| `deskbox.exe_path` | 按常见安装位置搜索 `DeskBox.exe` |

猜错了在「设置」页手改并保存即可。配置模板见
[`config.example.json`](config.example.json)，实际配置在
`%LOCALAPPDATA%\CanvasHelper\config.json`（首次运行自动生成，不入库）。

### 适配其他 Canvas 站点

本工具面向**任何用 Canvas（LMS）的学校**，不绑定某一所。站点地址、时区、课程代码
三处最容易因校而异，其余差异已由代码自动适配。

**1. 站点地址**：填你学校的 Canvas 主页即可（自建托管、Canvas SaaS 都一样）。
登录走真实浏览器 + 学校 SSO，因此各校的登录域名、2FA 流程都不用你配置。

**2. 时区**（影响截止时间的显示与「是否已过期」的判定）
留空跟随本机系统时区——多数人这就是对的。要固定就在「设置」页填 IANA 名。
时区填错会让待办里的截止时刻整体偏几个小时。

**3. 课程代码**：自动识别「字母前缀 + 数字」，并自动剥掉开头的学期前缀：

| Canvas 的 `course_code` | 本工具认出的课程代码 |
| --- | --- |
| `202609CS5187` | `CS5187` |
| `MATH 101` | `MATH101` |
| `ECON2201` | `ECON2201` |
| `BIO-220` | `BIO220` |
| `SD_GEN_SFTY_CS_MCS_2026`（多段码） | 整段保留，不拆 |

课程代码同时用于**匹配本地课程文件夹**（`MATH 101 - Calculus` 会被认成 `MATH101`，
同步时不会另建一个重复文件夹）。若你学校的代码形态特殊，在「设置」页的
「课程代码正则」里写一个正则（如 `^([A-Z]{2,4})-?(\d{4})$`）：有捕获组取第一个
非空捕获组，没有则取整个匹配。

**4. 已自动适配的 Canvas 差异**（无需配置，遇到即降级）

| 差异 | 本工具的做法 |
| --- | --- |
| 课程级 `/files` 接口 403 | 改从模块条目、作业描述、页面正文里挖文件 |
| `/courses/:id/pages` 列表 404 | 改用模块条目的 `page_url` 调 show 接口 |
| 模块条目不带 `content_details` | 逐个补查文件元信息 |
| 公告接口不存在 | 自动改用 `discussion_topics?only_announcements=true` |
| 文件被隐藏 / 锁定 | 跳过并记日志，不打扰你 |
| 课程代码带学期前缀 | 自动剥掉再匹配本地目录 |

### 数据目录

所有可写数据都在 `%LOCALAPPDATA%\CanvasHelper\`，与安装位置解耦，
换盘符、换机器、打包成 exe 都不受影响：

```
%LOCALAPPDATA%\CanvasHelper\
├─ config.json               配置（首次运行自动生成）
├─ state.json                同步状态（已下载文件、已读通知、已完成待办）
├─ sync.log                  日志
├─ pending_todos.json        后台排队的待办
├─ canvas-deadlines.ics      导出的截止日历
├─ session.json              Canvas 登录 cookie
└─ profile\                  持久化浏览器 profile
```

从旧版本（数据放在源码目录）升级时会**自动搬一次**，原文件保留作备份。

## 基础教程：它能做什么、怎么用

一句话：**替你把 Canvas 上的课件按既有目录规则归档到本地，把各课截止时间变成
桌面上的待办**。它接手四件日常麻烦事：

| 以前的做法 | 用本工具之后 |
| --- | --- |
| 一门课点进 Files 下载讲义 / 作业，再手动分文件夹 | 勾选课程 → **开始同步**，自动归档到 `1.Lecture session` … `6.Others`，讲义按章分文件夹、压缩包自动解压，本地已有的跳过 |
| 截止时间散在各课作业页 / 公告里，容易漏 | 同步时统一写进 **DeskBox 待办**（附 Canvas 原链接），或导出 `.ics` 进 Windows / 手机日历 |
| 想知道这周该干什么、考了多少分 | 通知 / 公告、成绩聚合在一页看；每周生成 `本周摘要-YYYY-Wnn.md` |
| 每天手动重复一遍 | 注册计划任务，每天后台同步一次（无窗口） |

**第一次上手，照着点六下**：

1. **登录** —— 点 **登录 / 刷新课程** → 弹出 Edge，完成学校 SSO / 2FA。
   课程列表出现后会自动匹配本地文件夹并预勾选。
2. **确认课程根目录** —— 设置页看 `classes_root`（默认 `<用户目录>\CanvasCourses`），
   不是你的目录就点 **浏览…** 改。
3. **先预览再动手** —— 点 **预览归档方案**，只读地列出每个文件将落到哪个目录，**不写盘**。
4. **同步一次** —— 点 **开始同步**：下载 + 归档 + 写待办 + 更新成绩 + 导出日历。
5. **看截止与通知** —— **待办** 页可单独读取截止时间、写入 DeskBox、导出 `.ics`
   （含提前 1 天提醒）；**通知 / 公告**、**成绩** 在另外两个标签。
6. **交给它自动跑** —— 设置页勾选 **启用每日自动同步** → 点 **保存设置**
   （同时注册计划任务），之后每天后台同步；登录过期会弹窗自愈，见
   [登录失效时会怎样](#登录失效时会怎样)。

按需再查这些章节：[归档规则](#归档规则)、
[DeskBox 待办写入策略](#deskbox-待办写入策略)、[一键清理缓存](#一键清理缓存)、
[命令行](#命令行)。

## 安全与隐私

- **联网范围很窄**：只访问你在配置里填写的 Canvas 站点与完成登录所需的 SSO 域名
  （由你学校的登录流程决定，如 `sso.canvaslms.com`），
  **不连任何第三方服务器**；无遥测、无统计、无任何数据上报。
- **数据只在本机**：配置、同步状态、日志、登录态全在 `%LOCALAPPDATA%\CanvasHelper\`
  （见 [数据目录](#数据目录)），课程文件落在你自己的课程根目录。
  这些目录都在仓库之外；`config.json` / `state.json` / `sync.log` 等可再生文件
  以及 `session.json` / `profile\` 还额外进了 `.gitignore`，双保险不会被提交。
- **登录态 = 凭据**：学校 Cookie 存在 `session.json` 与浏览器 `profile\` 目录，
  **不要发给别人、不要连同打包分享**；`canvas-helper clean --login` 可连登录态一起清掉。
- **日志不记凭据内容**：`sync.log` 只记录 cookie 条数与出错信息，不写 cookie /
  密码的值；不想留痕就用设置页「一键清理缓存」把日志一并清掉。
- **通知正文不落盘**：通知内容只在内存里、打开时现拉一次，`state.json` 只留已读标记。
- **动 DeskBox 数据前先备份**：写 `todo.json` 前自动备份为 `todo.json.pre-canvas`，
  只增不删、不碰你手写的条目（见 [已完成作业的处理](#已完成作业的处理)）。
- **可审计**：本项目以 MIT 开源，代码就在本仓库，以上每条都能在源码里核对。
- 风险自负：请遵守所在院校的可接受使用政策与 Canvas / Instructure 的服务条款，
  见 [许可与免责](#许可与免责)。

## 命令行

```text
python -m canvas_helper              # 图形界面（默认）
python -m canvas_helper login        # 交互式登录并保存会话
python -m canvas_helper courses      # 列出已识别课程
python -m canvas_helper sync --headless    # 后台同步（计划任务用）
python -m canvas_helper report --headless  # 生成本周摘要
python -m canvas_helper flush              # 把排队的待办写入 DeskBox
python -m canvas_helper preview            # 只预览归档计划，不写盘
python -m canvas_helper clean --dry        # 预览可清理的缓存与体积
python -m canvas_helper clean              # 清理默认项（状态/日历/日志/浏览器缓存）
python -m canvas_helper clean --login      # 连登录态一起清（之后需重新登录）
python -m canvas_helper tidy              # 预览讲义整理方案（不写盘）
python -m canvas_helper tidy --apply      # 确认后执行：散件移进章节目录
```

## 文件发现

不同课程在 Canvas 里的挂载方式差得很远，**任何单一接口都会漏**。同步时按序采
五条来源，任一条失败（403/404/限流）自动降级，不影响其余：

1. **模块条目**——含模块内的 Page 正文；
2. **作业描述**——截止时间与作业附件的唯一可靠来源；
3. **公告 / 讨论正文**——含 Colab / GitHub 链接抽取（见「归档规则」）；
4. **课程级文件索引** `/api/v1/courses/:id/files`。

其中第 1 条里藏着最麻烦的一种课。以 CS0003 / CS0005 为例，实测：

| 通道 | 结果 |
|---|---|
| 课程级 `/files` | **403** |
| 逐个文件夹列 `/folders/:id/files` | **403**（46/46 个文件夹全被挡） |
| 模块里的 File 型条目 | **0 条** |
| 模块条目的 `content_details.body` | **空**（14/14 个 Page 连 `body` 键都没有） |
| 页面 show `/courses/:id/pages/:page_url` | **200** ✅ |

也就是说课件全挂在模块内的 **Page 正文**里，而模块条目恰好带着 `page_url`——用它
去调页面 show 接口是唯一能挖到文件的路。这条路走通后：

| 课程 | 改造前 | 改造后 |
|---|---|---|
| CS0003 | 21 个 | **58 个**（+176%） |
| CS0005 | 9 个 | **41 个**（+356%） |
| CS0001 | 2061 个 | 2061 个（无回归） |

补充策略：

- 页面 show 接口也失败时，**退回抓 `html_url` 的源码**再正则抽链接；
- 单文件 `/files/{id}` 即使在索引 403 时也通常可读，拿它补齐 `folder_id`
  → 于是 `folder_path` 100% 拿到，归档分类不会因为索引被挡而退化成全进 `6.Others`；
- 死链（`/files/{id}` 返回 404）与 hidden/锁定文件被容错跳过，不中断整轮收集；
- 同一文件被多条来源命中时按 `file_id` 去重，不会重复下载。

这套退化路径有离线回归测试锁着（`tests/test_canvas_api.py`），改坏会红。

## 归档规则

判定优先级：**Canvas 文件夹路径 > 文件名/模块名 > 内容类型 > 根目录演示稿**。

- 关键词按「词边界 + 归一化」匹配：`_` `-` `/` 转空格、字母与数字之间断开，
  所以 `Assignment1` 能命中 assignment，而 `syllabus` 不会误判成 `lab`、`past_paper` 不会误判成 `paper`。
- 文件夹路径是最强信号（`course files/Lecture/Week3` → 讲义，`course files/Tutorial/Week2` → 习题课）；
  例外：文件夹只是泛泛的 `Week N`，而文件名明确写了作业/项目时，以文件名为准。
- 目标目录**镜像 Canvas 文件夹层级**，并复用本地既有目录名：
  `course files/Assignment1/CV_guideline/data/gallery` → `3.Assignments/Assignment_1/CV_guideline/data/gallery`；
  `Tutorial` / `Tutorials` 这类本身就是分类词的首段不再多建一层。
- **讲义按章分组，不再平铺**：识别到章节号就归进子目录（
  `1.Lecture session/Lecture3/Lecture3a.pdf`），编号来源优先级为
  **Canvas 文件夹里的编号 > 模块名/文件名的「关键字+编号」（`Lecture3a` `Week 2`）
  > 文件名纯数字开头（`1.Review of Algorithms.pptx`、`0.Introduction.pptx`）**；
  目录名跟着本地既有样式走（已有 `Lecture1` 就一律 `Lecture{n}`）。
  识别不到编号的散件（`data.zip`、`jupyter-shortcuts.pdf`）留在根目录平铺，
  `CS0003 - Lecture slides.pdf` 这类也不会被「名字里第一个数字」误分成 `Lecture54`。
- **压缩包自动解压**（设置页可关）：内容包（含 `.ipynb` / `.py` / `.pdf` …）解压到就近
  目录（`Lecture3\Lecture3.zip` 解进 `Lecture3\`），**数据包原样保留**——包名命中
  `data` / `dataset` / `train` …，或忽略 `README*` 后包内没有任何讲义/代码文件
  （`.sh` 装机脚本与 `.txt` 不算内容信号）的，一律不解压
  （`data.zip` 这类正是 ipynb 要读的原始数据）。判定在本机 35 个真实课程压缩包上标定过：
  17 个讲义/作业包全解、10 个数据包全留。解压逐条手工写盘、拒绝 `..` 越界
  （zip-slip）、跳过 `__MACOSX` / `._*` 垃圾、带体积上限；判定结果记进 `state`，
  重复同步不会解第二遍，压缩包本身永远保留。
- **讨论区里的链接文件**（设置页「保存讨论区内容」可关）：Colab 教程笔记本直接
  下载——`colab.research.google.com/drive/{文档id}`（Drive 型，公开分享可匿名
  下载，用帖子标题命名）与 `colab.research.google.com/github/.../blob/...`、
  `github.com/.../blob/...`（GitHub 型，转 raw 直链）都支持。列表接口不给正文
  时自动补 show 接口；正文里没链接时才去翻回帖。帖子本身另存一份 HTML 快照
  （含正文与「打开原文」链接），私有链接下不了时也不丢；公告只扫链接不快照。
- **整理已有的散件**：设置页 **整理讲义目录** 或 `python -m canvas_helper tidy`
  先列出「哪些文件移到哪个章节目录」的方案，确认后才移动——只移动不删除，
  重名自动加 `(1)`，`state.json` 里的路径跟着回写。
- 分类目录以磁盘上已存在的 `N.` 目录为准，例如 `5.Useful links` 与 `5.Books` 二选一。
- 本地已有内容不重复下载：按**归一化文件名 + 大小**判断，
  Canvas 的 `1. Review of Algorithms.pptx` 能对上本地 `1.Review of Algorithms.pptx`、
  `0.Introduction.pptx` 能对上 `Introduction.pptx`；纯数字名（`0.jpg` / `1.npy`）不会被误判。
- 只增不删：同名同大小跳过，同名不同大小自动加 `(1)` 后缀。

## 关于 DeskBox

[DeskBox](https://github.com/Tianyu199509/DeskBox)（官网 [deskbox.fun](https://deskbox.fun)）
是一个**免费开源的 Windows 桌面整理工具**：用 WinUI 3 原生质感的小组件把桌面文件
收进格子，自带待办、随记、桌面搜索等能力，数据全部保存在本机，支持 Windows 10/11
的 x64 与 ARM64，GPL-3.0 协议。

本工具与它的关系：

- CanvasHelper 解析出 Canvas 的截止时间后，**写进你电脑上 DeskBox 的「待办」小组件**
  （数据文件 `todo.json`），课业安排因此直接出现在桌面上；写入规则见
  [DeskBox 待办写入策略](#deskbox-待办写入策略)。
- 两者是**各自独立的项目**：CanvasHelper 不是 DeskBox 的一部分，未获其作者背书；
  本仓库只按对方公开的数据文件格式读写，不包含 DeskBox 的任何代码。
- **没装 DeskBox 也不影响使用**：`deskbox.todo_path` 为空时同步会自动跳过待办写入，
  课程归档、通知、成绩、日历等照常；想要提醒可以改用 `.ics` 导出。
  装了 DeskBox 后路径会被自动发现，猜错了在设置页「浏览…」里手选。

## DeskBox 待办写入策略

| 场景 | 行为 |
| --- | --- |
| GUI 手动同步 | 有新增待办且 DeskBox 在运行 → 关闭 → 替换 → 重启（约 1 秒）；未运行则直接写文件 |
| 后台计划任务（默认 `queue`） | 不打扰 DeskBox，写入 `pending_todos.json`，下次打开 GUI 自动补写 |
| 后台 `restart` | 后台也执行关闭/替换/重启 |
| 后台 `skip` | 后台同步不写待办 |

待办去重键是 Canvas 原链接（写在 notes 里），重复同步不会产生重复条目。
可选 **自动修复** 历史待办里缺少 `Classes\` 一层的坏附件路径。

数据文件：DeskBox 的 `todo.json`（默认自动发现为
`%LOCALAPPDATA%\DeskBox\data\widgets\<widget-id>\todo.json`，可在设置页手改；
改写前自动备份为 `todo.json.pre-canvas`）。

## 已完成作业的处理

**问题**：某项作业已经在 DeskBox 里勾了完成，下一次同步/一键写入时又把它加进来了怎么办？

按下面四条判定，命中即停止：

1. **清单里已有同 key 的条目**（Canvas 原链接，或 `文本+截止时间`）→ 不重复添加；
   如果那条已完成，把草稿标成 `已完成`，并在 `state.json → todo_done` 里记一笔。
2. **记过完成、后来又被删掉**（截止时间没变）→ 仍然不添加。
   覆盖「完成了 → 从清单里删掉 → 同步时又冒出来」这种最烦的情况。
3. **记过完成、但 Canvas 的截止时间变了** → 说明有了新的截止，**重新添加**并标记 `截止已更新`，
   同时刷新记录。否则作业延期了你就再也收不到提醒。
4. **key 对不上**（没链接、文本被改过）→ 用归一化文本兜底匹配，规则同上。

另外，写入时会顺手**清理重复项**：同一 `文本 + 截止时间` 的条目里只要有一条已完成，
就删掉其中未完成的那些（`deskbox.remove_duplicates`，默认开启）。
截止时间不同的同名任务（比如你自己的每周重复任务）完全不会被碰。

同步/预览的待办列表会直接标出 `已完成` / `已存在` / `截止已更新`，
写入完成后弹窗也会说明「新增几条、跳过几条已完成」。

## 一键清理缓存

**设置** 页的「一键清理缓存」按钮（或命令行 `clean`）。

| 项目 | 默认 | 说明 |
| --- | --- | --- |
| 下载状态 / 已读标记 | ✅ | 清 `state.json` 的下载记录与已读标记；下次同步重新核对，有本地内容识别兜底不会重复下载，通知会被重新标为未读 |
| 导出的 `.ics` | ✅ | 随时可重新导出 |
| 运行日志 | ✅ | 清空 `sync.log`（FileHandler 保持打开，继续记录新日志） |
| 浏览器缓存 | ✅ | `Default/Cache`、`ShaderCache` 等 14 个缓存目录，**不含 Cookie，登录态仍然有效** |
| 后台排队待办 | ❌ | 会丢掉还没写入 DeskBox 的截止提醒，需要手动勾选 |
| DeskBox 重复待办 | ❌ | 删除重复条目并重启 DeskBox，需要手动勾选 |
| 登录态 | ❌ | `session.json` + 浏览器 Cookie，勾了就得重新登录 |

浏览器缓存按**目录名**匹配，`Cookies` / `Login Data` / `Local State` / `Local Storage`
等永远不在清理范围内（自检里有对应断言）。

## 登录失效时会怎样

学校 SSO / 2FA 的登录态**大约 7 小时后过期**，而计划任务一天才跑一次，所以后台同步
大概率会撞上过期。这条路径不会被悄悄吞掉：

1. **先试着自愈** —— 后台发现过期时弹出一个可见的 Edge 登录窗口，等你完成 SSO
   （默认 `popup_on_expiry: true`，等 `popup_timeout_s: 180` 秒，超时自动关闭）。
   你若就在电脑前，顺手登录即可，本轮同步照常完成。
2. **没救回来就留下记号** —— 把 `auth_needed` 写进 `state.json`，`sync.log` 记一条
   `ERROR`，且 `python -m canvas_helper sync --headless` **退出码为 2**
   （区别于表示其他错误的 1）。
3. **下次打开 GUI 一眼看到** —— 顶部「登录状态」变成
   `⚠ 已过期（2026-10-05 08:30），点击重新登录`，点它或点「登录 / 刷新课程」；
   登录成功后标记自动清除。

同步途中 cookie 失效同样会中止整轮（不会带着过期会话继续下载 / 写待办）并置同一标记。

相关配置（`config.json` 的 `auth` 段，模板见 [`config.example.json`](config.example.json)）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `popup_on_expiry` | `true` | 后台过期时是否弹窗自愈；设为 `false` 则只记标记，绝不打扰无人值守的机器 |
| `popup_timeout_s` | `180` | 弹窗等待秒数，下限 10 秒 |

判定「能不能弹」看的是 **Windows 会话号**（0 = 服务，1+ = 交互），而**不是**
`SESSIONNAME`——后者只存在于登录时的会话环境，计划任务的环境是从用户注册表重建的、
实测为 `null`，拿它当判据会把每日同步这条主路径一起判死，永远弹不出窗口。
每周摘要过期时**只记标记不弹窗**——每日同步在前已经弹过一次，不再连弹两次。

## 目录结构

```
CanvasHelper\
├─ canvas_helper\
│  ├─ __init__.py    APP_NAME / __version__（与 pyproject 的 version 必须一致，自检会断言）
│  ├─ paths.py       路径 / 配置 / 日志 / 状态（数据目录 + 自动发现 + 过期标记）
│  ├─ auth.py        持久化浏览器 profile + 登录判定 + 过期标记清除
│  ├─ canvas_api.py  /api/v1 分页封装（课程/模块/页面正文/文件/作业/公告/讨论/成绩）
│  ├─ coursecode.py  课程代码提取与归一化（跨院校通用）
│  ├─ classify.py    分类与子目录推断
│  ├─ filesync.py    下载计划（dry-run）与执行
│  ├─ deskbox.py     待办合并 / 副本预写 / 关-换-启
│  ├─ extras.py      通知、成绩、.ics、每周摘要
│  ├─ cleaner.py     一键清理缓存（扫描 / 执行）
│  ├─ scheduler.py   Windows 计划任务
│  ├─ runner.py      流程编排（含登录过期的弹窗自愈）
│  ├─ gui.py         tkinter 界面
│  └─ __main__.py    命令行入口（无参数开 GUI / sync / report / clean / preview）
├─ tests\
│  ├─ __init__.py        让用例以 tests.xxx 形式被发现（少了它 mypy 的 tests.* 规则不生效）
│  ├─ smoke.py           离线自检的实现（分类 / 归档 / 待办合并 / 完成去重 / 缓存清理 / 登录过期）
│  ├─ conftest.py        pytest 夹具（副作用隔离、课程目录、待办草稿上下文）
│  ├─ test_smoke.py      把自检按小节交给 pytest 执行（14 个用例）
│  └─ test_canvas_api.py 文件发现退化路径的离线回归（7 个用例，不联网）
├─ scripts\
│  ├─ pyinstaller_entry.py  打包入口（__main__.py 是相对导入，不能当独立脚本分析）
│  ├─ headless_entry.py     计划任务入口（__file__ 定位源码，配 pythonw 免黑框）
│  ├─ make_icon.py          源图 → 裁边框 → 去外圈留圆角（四角透明）→ 多尺寸 ico（--verify 体检）
│  ├─ make_shortcut.ps1     生成桌面快捷方式（直启 pythonw 免控制台，挂 ico；.lnk 不入库）
│  └─ capture_gui.ps1       截 README 用的窗口截图（源码版 / 打包版，兼容 pythonw）
├─ docs\
│  ├─ icons\canvashelper.ico    应用图标（圆角、四角透明；16~128 BMP + 256 PNG 七档）
│  ├─ icons\canvashelper.png    图标预览（README 顶部展示）
│  └─ RELEASING.md              发布流程 / 打包双轨 / 代码签名方案
├─ .github\
│  ├─ workflows\ci.yml      lint（ruff + mypy）+ 离线自检 × Python 3.11~3.14
│  └─ ISSUE_TEMPLATE\       Bug / 功能建议的 issue forms
├─ pyproject.toml     元数据 + ruff + mypy + pytest 配置（唯一版本号来源）
├─ CHANGELOG.md       变更记录
├─ config.example.json  配置模板（可写数据不在这里，见「数据目录」）
├─ LICENSE
└─ 启动Canvas助手.bat   用 %~dp0，位置随意；start + pythonw，启动不留黑框
```

## 版本历史与发布

- 变更记录见 [CHANGELOG.md](CHANGELOG.md)（[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，语义化版本）。
- 发布流程、转公开前需要 scrub 的位置、PyInstaller / PyPI 双轨打包，以及
  **代码签名的方案与现状**（需要花钱的那部分，见文档里的说明）见
  [docs/RELEASING.md](docs/RELEASING.md)。
- 有问题请提 [Issue](https://github.com/Botherain/CanvasHelper/issues)，
  模板里已列好需要一并提供的系统、版本、日志等信息。

## 许可与免责

本项目以 [MIT License](LICENSE) 发布。

- **仅限个人学习使用**。请遵守所在院校的可接受使用政策与 Canvas/Instructure 的服务条款，
  不要用于批量抓取、爬取或向第三方再分发课程内容。
- 本工具依赖 Canvas 的 `/api/v1` 接口，**接口变动可能导致功能失效**，不提供可用性保证。
- 对因使用本工具造成的任何数据丢失、账号问题，作者不承担责任。
- 与 Instructure、Canvas 及任何院校**均无关联**，也未获其背书。

## 自检与静态检查

```powershell
python tests\smoke.py      # 脚本入口：逐条 PASS/FAIL，任一失败退出码 1
python -m pytest           # 同一套断言，按小节拆成 14 个用例
python -m ruff check canvas_helper tests scripts
python -m mypy
```

后三个都是 dev extra 里的工具：`python -m pip install -e ".[dev]"`。

208 项断言全部**离线**执行，且与本机数据解耦——没有真实课程目录 / `todo.json` 的机器
（含 CI runner）会自动按蓝本造 fixture，结果应当一致为 208/208。

两种入口共用同一份实现（`tests\smoke.py` 里的 `s1_…`～`s9_…`），因此不会出现
「脚本绿、pytest 红」的分叉。小节之间只通过夹具共享数据，**任意一节都能单独跑**：

```powershell
python -m pytest -k test_7      # 只跑「附加功能」，它依赖的 [5] 草稿由夹具提供
```

失败时 pytest 直接列出该小节**全部**失败项（不是在第一条就中止）。

每次推送到 `main` 及每个 PR 都会由
[GitHub Actions](https://github.com/Botherain/CanvasHelper/actions/workflows/ci.yml)
在 Windows × Python 3.11/3.12/3.13/3.14 上各跑一遍；另有一个 `lint` job 跑
ruff + mypy。

## 注意

- 清空 `%LOCALAPPDATA%\CanvasHelper\` 会导致登录态丢失，需要重新登录。
- 后台同步需要系统里有可用的登录态（先在 GUI 登录过一次）；过期时按
  [登录失效时会怎样](#登录失效时会怎样) 处理，**不会静默失败**。
- 计划任务在当前用户下运行，注销后不执行；弹窗自愈也依赖交互式会话。
