# 更新日志

本项目的所有重要变更都记录在此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

## [1.1.0] - 2026-10-20

本版把工具从「只适配一所学校」推到「任何用 Canvas 的院校」，并完成一轮资源占用
优化。所有改动向后兼容：新增配置项都有安全默认值，`now_hk()` / `HK` 等旧名保留
为兼容别名，既有用户升级后行为不变（本机时区为 +08:00 时与原 `Asia/Hong_Kong`
硬编码结果一致）。

### 变更 · README 重构

- 按「项目介绍 / 快速开始 / 详细使用教程 / 相关项目 DeskBox / 常见问题与解答 /
  免责声明」六段重写，新增目录导航与 12 条可跳转的内部链接（已用脚本校验锚点全部有效）。
  原先散落在 12 个平级小节里的内容按用途归位：安装步骤进「快速开始」，归档规则、
  文件发现降级、命令行、设置项进「详细使用教程」，登录过期与注意事项改写为 FAQ 问答。
  技术细节与实测数据（CS0003/CS0005 的接口降级对照表、压缩包判定标定结果等）全部保留。
- 新增「设置项一览」表（17 项配置的默认值与含义）、「院校清单与在线更新」小节，
  以及 11 条 FAQ（学校不在清单、时区偏差、登录过期、重复待办、无 DeskBox、
  磁盘占用、文件拿不全、换学校 / 换机器、第三方连接、清空数据目录、接口变更）。
- 排版按项目行文约定统一：段落不再句中硬折行、箭头统一用 ASCII `->`、
  路径与标识符一律反引号。

### 新增 · 登录前先选学校（不再与任何单一院校绑定）

- **首次启动引导选院校**：没配过站点地址时，程序启动后弹出「选择你的学校」窗口
  ——搜索已核实的院校清单（126 所，涵盖美加澳新英荷韩日新港台等），或直接手填
  地址；选完自动写入 `canvas_home` 并进入该校的 Canvas 登录流程。已配置过的
  机器不受打扰，设置页与顶栏也各留一个「选择院校」入口随时可改。
- **清单里的每条地址都经程序核实**：请求 `<地址>/api/v1/users/self/profile`，
  返回 HTTP 401 且响应体是含「未认证」语义的 JSON 才算 Canvas 实例（非 Canvas
  站点返回 404 / HTML 页面）。实测约 190 个候选地址中有 98 个其实不是 Canvas
  或根本不存在，已全部剔除；院校名称取自站点自身（登录页标题 / logo / 版权行），
  抓不到时以域名为准。
- **程序里不含任何与具体院校绑定的逻辑**：选完只得到一个站点地址，后续登录、
  课程读取、归档全部走通用流程；cookie 仍是整上下文保存，各校 SSO 域名自动适配。
- **地址归一化**：手填时不用写 `https://`、不用删结尾斜杠，直接从浏览器地址栏
  整段粘贴 `…/login` 也会被收拾成站点根地址。
- **登录跳转改为直达 `/login`**：各校 SSO 都从登录页开始，比开首页少一跳；
  打不开登录页时自动退回首页。
- **命令行新增 `schools`**：列出已核实的院校清单（可带关键词过滤），
  `python -m canvas_helper schools 香港`。
- 离线自检新增第 [14] 节「院校清单与地址归一化」，smoke 断言 208 -> 227、
  pytest 用例 35 -> 36。

### 新增 · 院校清单可在线更新

- **在线源**：Instructure 官方院校目录接口（公开 API，官方「Find My Canvas URL」
  用的就是它），返回官方托管租户的「名称 + 域名」。实测约 100 条原始记录。
- **本地缓存**：拉到的结果写入 `%LOCALAPPDATA%\CanvasHelper\schools_cache.json`
  （约 8 KB），与内置清单合并去重后供界面使用；合并时**内置优先**——同一地址
  保留内置的机构名（经站点核实、带中英文），在线源只补充它没有的。
- **拉取时机**：GUI 启动时后台静默拉取（缓存超过 30 天才拉，不阻塞界面）；
  也可随时手动触发——选择院校窗口的「更新清单」按钮，或
  `python -m canvas_helper schools --update`。
- **降级链**：在线源不可达 -> 用本地缓存 -> 都没有就只用内置清单。任一环节
  失败都只记一条警告，绝不打断用户操作。
- **清洗**：官方目录里混着测试 / 演示租户（`*-dev`、`*-qa`、`*-test`、纯编号
  租户等），拉取时自动剔除（实测 100 条 -> 84 条可用）。
- **实测**：拉取 + 合并耗时约 0.6 秒，清单从 126 条涨到 210 条；缓存损坏
  时自动回落到内置清单；源不可达时保持现有清单不变。
- 离线自检新增第 [15] 节「院校清单在线更新与缓存」，smoke 断言 227 -> 249、
  pytest 用例 36 -> 37。

### 新增 · 跨院校通用化（不再只适配一所学校）

面向「任何用 Canvas 的学校」的三处通用化改造，站点地址、时区、课程代码都能按校配置。

- **时区可配（新增 `timezone` 配置项）**：原先 `Asia/Hong_Kong` 在三处硬编码，
  别的学校会把所有截止时间算错 8~13 小时（待办显示、「是否已过期」判定、
  写入 `todo.json` 的 `dueDate` 全受影响）。现在统一走 `paths.to_local()` /
  `paths.now_local()`：配置项写 IANA 名（`America/New_York` 等）即按它，
  **留空则跟随本机系统时区**；名称非法时回落系统时区并记一条警告，不让同步崩掉。
  兼容别名 `now_hk()` / `HK` 保留不动（按「只增不删」约定），新代码一律用新名。
- **课程代码通用化（新增 `coursecode.py` 与 `course_code_pattern` 配置项）**：
  原先 `Course.short` 只认 `CS` + 4~5 位数字、`course_dirs` 只认
  `^(CS\s*\d{4,5})` 的目录名——换学校就匹配不上本地目录，**每次同步都新建一个
  重复文件夹**。现在用一套通用规则（字母前缀 + 数字，自动剥掉开头的学期前缀）：
  `202609CS5187` -> `CS5187`、`MATH 101` -> `MATH101`、`ECON2201` -> `ECON2201`、
  `BIO-220` -> `BIO220`；下划线多段码（`SD_GEN_SFTY_CS_MCS_2026`）整段保留不拆。
  课程代码与本地目录键**共用同一套实现**，比较时忽略空格 / 连字符 / 下划线差异。
  形态古怪的学校可在设置页写自定义正则（有捕获组取第一个非空捕获组）。
- **周报时间比较改为 datetime**：原先 `synced_at` 用字符串比较，时区配置不同
  （或历史条目与新条目偏移不一致）时会漏报 / 多报本周新增文件。
- **GUI 设置页新增两项输入**：「时区（留空=跟随本机）」与「课程代码正则（留空=自动）」。
- **已适配的 Canvas 差异写进 README**：课程级 `/files` 403、页面列表 404、
  模块条目无 `content_details`、公告接口不存在等，均已有多路降级，无需用户配置。
- 真实 API 回归：本校 10 门课的 `course_code`（含 `202609CS5187` 与 4 门 `SD_*`）
  新旧实现**逐条一致**；真实同步 exit 0（2244 项已存在、0 错误、待办 0 新增）。
- 离线自检新增第 [13] 节「跨院校通用化」（课程代码提取 / 目录匹配 / 时区换算），
  smoke 断言 180 -> 208、pytest 用例 34 -> 35。

### 优化 · 轻量化与资源占用

- **GUI 空闲不再烧 CPU**：`_deskbox_pids` 由「每次 spawn `tasklist`」换成
  ctypes Toolhelp32 进程快照（进程内查询）——实测旧版单次 tasklist 约 245ms
  CPU，而 GUI 每 4 秒轮询一次、`stop()` 关停循环每 0.15 秒一次，常开一天是
  上万次进程创建；换 Toolhelp32 快照后实测中位数约 10ms（约 23 倍）且
  **零子进程**，子进程控制台黑框问题一并根除（`taskkill` / `Popen(DeskBox)`
  仍走子进程，保留 `CREATE_NO_WINDOW`）。
  顺带把匹配从 `startswith` 收紧为 exe 全名相等，`DeskBox` 不再误命中
  `DeskBoxXxx.exe` 之类。
- **`load_config()` 加 mtime 缓存**：键 = (路径, mtime_ns, size)，`save_config`
  显式失效，返回 deepcopy（调用方改写语义不变）。同步热路径里 `canvas_home()` /
  `cfg_discussions()` 被逐条待办反复调用，原来每次都读盘 + 解析 + 深合并；首装机
  `todo_path` 未配置时还会每次调用都 glob 一遍 `widgets/*/todo.json`。解析失败同样
  按键缓存成默认配置，坏配置只刷一次异常日志。
- **GUI 日志保留最近 500 行**：`log_text` 原先只增不删，长会话文本控件无限增长；
  超限后从顶部按批删行，自动滚底行为不变。
- **import 不再动磁盘**：`_bootstrap()`（建目录 + 搬旧数据）从模块导入时挪到
  `ensure_dirs()`（两个入口都会先调），`--version`、跑测试、被引用导入都变成
  纯内存操作；`now_hk()` 的 `ZoneInfo` 提为模块级常量，与 deskbox/extras 写法
  统一。
- **`state.json` 读缓存**：`load_state()` 新增 `(路径, mtime_ns, size)` 缓存并返回
  deepcopy，`save_state()` 显式失效。通知已读标记、鉴权标记与清理逻辑在一次运行
  中会多次读状态文件，缓存把重复读盘 + JSON 解析压到一次。
- **日志改轮转**：`sync.log` 由 `FileHandler` 改为 `RotatingFileHandler`
  （1MB × 3 备份），长期运行时不再无限增长；`cleaner` 原有 `.1/.2/.3` 清理
  逻辑直接复用。
- **CI 轻量化（网络）**：工作流环境新增 `PIP_DISABLE_PIP_VERSION_CHECK=1`，
  减少每个 job 的 pip 额外网络探测与日志噪音。

### 发布准备 · 转公开前的去标识化

- **院校信息移除**：硬编码的学校域名全部改为配置项 `canvas_home`
  （`config.json` + 设置页「Canvas 站点地址」输入框，未配置时同步明确报错）；
  `auth.py` 的 cookie 白名单改为整上下文保存（SSO 域名随学校自动适配）；
  `canvas_api` / `extras` 的 `html_url` 兜底、`deskbox` 的链接正则、
  GUI「打开 Canvas」按钮全部改读该配置——本工具不再绑定任何特定院校。
- **真实课程号/课程 id 示例化**：文档、注释、测试 fixture 与断言里的
  6 个真实课程号换成 `CS0001`…`CS0006`，课程 id 换成 `10001`…`10006`，
  测试域名统一 `canvas.example.edu`。
- **移除含真实数据的截图**：`docs/screenshots/gui-main.png`（含课程号与通知标题，
  无法文本替换）连同 README 引用一并删除。
- **历史提交同步清洗**：作者/提交者邮箱换 GitHub noreply，旧提交的文件内容与
  提交信息里的域名、课程号、本机路径逐字替换，截图从全部历史中移除，force push。
- `scripts/make_icon.py` 的默认源图路径改 `Path.home()/Downloads/…`，不再含用户名。

### 新增 · 讨论区 Colab/GitHub 链接与帖子快照

- **教程不再手动另存**：CS0004 的 tutorial 以 Colab 链接发在讨论区，现在会直接
  下载成 `.ipynb`。实测两种链接形态都支持：
  * **Drive 型**（CS0004 week2-7 全是这种）`colab.research.google.com/drive/{文档id}`
    → 转 `drive.google.com/uc?export=download` 直链，公开分享可匿名下载；链接
    本身不带文件名，用帖子标题命名（`Colab sharing for week7.ipynb`），同一帖里
    多条无名链接自动加 `-2` 序号避免互相覆盖；
  * **GitHub 型** `colab.research.google.com/github/.../blob/...` 与
    `github.com/.../blob/...` → 转 `raw.githubusercontent.com` 直链，任意文件
    类型都抓；同链接去重、`?usp=sharing` 这类 query 剥掉；私有仓库 404 按普通
    下载失败容错。
- **列表接口不给正文也拦不住**：`discussion_topics` 列表接口实测 7/7 个
  `message` 为空，正文要靠 show 接口补——链接抽取与快照都用这份正文；
  正文里没链接时才去翻回帖（省 API 调用），嵌套 replies 也扫。
- **帖子本身存 HTML 快照**（新增 `discussions` 配置段 + 设置页「保存讨论区内容」
  勾选，一键启停快照与链接下载）：含帖子正文与「打开原文」链接，是私有链接下
  不了时的兜底；公告只扫链接不快照（通知渠道已聚合）。按帖标题分类落盘
  （`Colab sharing for week7` → `2.Tutorial session` 平铺，`colab` 已加入 tutorial
  关键词）；幂等键 = 课程 + topic id，重复同步不重存。
- 真实库落地验证：CS0004 拉回 8 个笔记本（20.5 MB）+ 7 个快照，另 5 门课的
  讨论区 Q&A（组队、迟交询问等）存了 10 个快照，零错误。
- 新增 `CanvasAPI._link_targets` / `discussion_entries`；离线自检新增第 [12] 小节
  （链接解析、按帖标题分类、快照落盘含正文、二次同步幂等），`tests/test_canvas_api.py`
  新增 6 例（colab 转直链、懒拉回帖、公告只扫链接、解析边界、show 补正文、
  Drive 型命名），smoke 断言 165 → 180、pytest 用例 27 → 34。

### 新增 · 讲义按章归档与压缩包自动解压

- **讲义不再平铺**：识别到章节号就归进 `1.Lecture session/Lecture3/`。编号优先级为
  Canvas 文件夹 > 模块名/文件名「关键字+编号」（`Lecture3a` / `Week 2`）> 文件名纯
  数字开头（`1.Review of Algorithms.pptx` → `Lecture1`），目录名复用本地既有样式
  （已有 `Lecture1` 就一律 `Lecture{n}`）。刻意**不**用「名字里第一个数字」，
  `CS0003 - Lecture slides.pdf` 不会被误分成 `Lecture54`；识别不到编号的散件
  （`data.zip`、`jupyter-shortcuts.pdf`）保持平铺。
- **下载后自动解压**（新增 `archives` 配置段 + 设置页勾选，可关）：内容包解到就近
  目录（`Lecture3\Lecture3.zip` 解进 `Lecture3\`、`data.zip` 解进 `data\`），
  **数据包原样保留**——包名命中 `data` / `dataset` / `train` …，或忽略 `README*` 后
  包内没有任何讲义/代码文件的一律不解压（`.sh` 装机脚本、`.txt` 不算内容信号，
  这正是 ipynb 要读的原始数据）。判定在本机 35 个真实课程压缩包上标定：17 个讲义/
  作业包全解、10 个数据包全留（含 `README.md`+csv 的、`README`+`.sh`+tsv 的）；
  刻意**不**按「数据文件占比」判——真实 `Lecture5/6.zip` 图片占 72% / 91%，会被误伤。
  解压逐条手工写盘（绝不 `extractall`）、拒绝 `..` 越界（zip-slip）、跳过
  `__MACOSX` / `._*` / `.DS_Store` 垃圾、单包 2 GiB 体积上限、同名同大小跳过；
  判定与结果按「路径 + 大小」记进 `state["archives"]`，重复同步不会解第二遍；
  `.tar` / `.tar.gz` 一并支持，`.rar` / `.7z` 如实标注「不支持」；压缩包本身永远保留。
- **预览与摘要都能看到**：计划里每个压缩包带 `压缩包已解压 / 数据包，不解压 /
  下载后自动判定解压` 备注，同步摘要多一行「解压 N 个压缩包（+M 个文件）」。
- **整理已有的散件**：新增 `python -m canvas_helper tidy`（默认只预览，`--apply` 才
  执行）与设置页「整理讲义目录」按钮，把根目录的 `LectureN.zip` / `LectureNa.pdf`
  移进各自章节目录——只移动不删除、重名自动加 `(1)`、`state.json` 的路径与解压记录
  键跟着回写。
- 新增 `canvas_helper/extract.py`、`canvas_helper/tidy.py`；离线自检新增第 [10]
  [11] 小节（判定规则、zip-slip、幂等、tidy 预览/移动/state 回写），
  smoke 断言 129 → 165、pytest 用例 25 → 27。

### 文档 · README 补齐快速开始、基础教程、安全与隐私与 DeskBox 介绍

- **快速开始**扩成四步：① 取得项目文件（clone / ZIP）→ ② 装依赖 → ③ 首次启动
  自动生成配置与数据目录（列出会创建哪些文件）→ ④ 桌面快捷方式，外加注册每日
  同步的一句话；从零到可用照抄即可，环境配置与项目文件下载都在里面。
- 「使用流程」升级为**基础教程**：先用一张对照表讲清它替你省掉的四件事，再给
  第一次上手的六步点法，文末挂上相关章节的索引。
- 新增**安全与隐私**：联网范围（只访问学校 Canvas 与 SSO，无遥测）、数据只在本机、
  登录态即凭据及其清除方式、日志不记 cookie 内容、通知正文不落盘、写 DeskBox 前
  自动备份、MIT 可审计。
- 新增**关于 DeskBox**：介绍这个开源桌面整理项目、本工具与其的读写关系（独立项目、
  互不背书、未装时自动跳过待办），并附仓库与官网链接。
- `.gitignore` 兜底忽略 `session.json` 与 `profile/`：它们本就在 `%LOCALAPPDATA%`，
  这一层是防止有人误拷进源码目录。

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

[未发布]: https://github.com/Botherain/CanvasHelper/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Botherain/CanvasHelper/releases/tag/v1.1.0
[1.0.0]: https://github.com/Botherain/CanvasHelper/releases/tag/v1.0.0
