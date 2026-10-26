# 发布流程

本文件记录「程序发行还需要哪些工作」里**需要花钱或需要你本人操作**的部分。
代码侧能自动化的都已经进 CI 了，这里只讲剩下的。

## 1. 每次发布前的固定动作

```powershell
python -m ruff check canvas_helper tests scripts  # 0 错误
python -m mypy                             # Success: no issues found
python tests\smoke.py                      # 347 通过, 0 失败
python -m pytest                           # 39 passed
```

改了 `pyproject.toml` 的 `version` 就必须同步改
`canvas_helper/__init__.py` 的 `__version__`——自检第 [9] 节会断言两者相等，
不一致直接红。

CI（`lint` + `smoke` × Python 3.9/3.10/3.11/3.12/3.13/3.14）全绿后才能发。

## 2. 版本与变更

- 版本号：语义化版本，改 `pyproject.toml` 与 `__init__.py` 两处。
- `CHANGELOG.md`：把 `[未发布]` 下的内容挪进新的版本小节，补日期。
- **`CHANGELOG` 的这一节会原样变成应用内的「更新公告」**：`release.yml` 用
  `^## \[<version>\]` 抓出正文写成 Release body，客户端（`canvas_helper/updater.py`）
  再把 body 显示在公告窗里。所以：写给人看、别写只有开发者懂的内部编号；
  这一节里的 `###` / `**` 会在公告里被压成纯文本。

## 3. 转公开（public）前必须 scrub 的内容 ✅ 已执行

本节清单已在转公开时全部执行完毕，留档以备复查：

| 文件 | 内容 | 处理 |
| --- | --- | --- |
| `canvas_helper/auth.py` | `CANVAS_HOME`、cookie 白名单里的学校域名 | 改为配置项 `canvas_home`（`config.json`），cookie 整上下文保存，SSO 域名不再硬编码 |
| `canvas_helper/canvas_api.py` | 回退用的 `html_url` 兜底 | 改走 `self.s.url(...)`，由 `canvas_home` 拼接 |
| `canvas_helper/extras.py` | 公告回退链接里的域名 | 同上（`api.s.url(...)`） |
| `canvas_helper/deskbox.py` | 待办备注链接正则里的域名 | 按 `canvas_home` 动态编译 |
| `canvas_helper/gui.py` | 「打开 Canvas」按钮固定链接 | 改读 `canvas_home`；设置页新增「Canvas 站点地址」输入框 |
| `canvas_helper/classify.py`、注释 | 真实课程号示例 | 全部换成示例号 `CS0001`…`CS0006` |
| `tests/smoke.py`、`tests/test_canvas_api.py` | 真实课程 id、课程号、域名 | 换成 `10001`…`10006` / `CS000x` / `canvas.example.edu` |
| **`README.md`** | 「安全与隐私」等节的本校域名 | 改为「你配置的 Canvas 站点与 SSO 域名」的通用表述 |
| **`docs/screenshots/gui-main.png`** | 整张图（课程号 + 通知标题） | **已删除**（无法文本替换），README 移除引用 |

另在**历史层面**（当前树 scrub 不覆盖旧提交）执行了：

- `git filter-repo`：全部提交的作者/提交者邮箱 → GitHub noreply；文件内容里的
  域名、课程号、课程 id、本机路径逐字替换；截图从所有历史提交中移除；
- `git filter-branch --msg-filter`：提交标题/正文里的同类词一并替换；
- 完成后 force push，`git log -S` 复核敏感词零命中，再 `gh repo edit` 转 public。

`LICENSE` 与 README 徽章里的 GitHub 用户名 `Botherain` 保留，那是署名。

## 4. 打包分发（双轨）

### 轨道 A · PyInstaller（给不想碰 Python 的人）

**推 tag 就自动出包**——`.github/workflows/release.yml` 在 `v*` 标签推送时构建
wheel / sdist / 单目录 exe，并把三者挂到对应的 GitHub Release 上，本地不用装
任何打包工具：

```powershell
git tag v1.2.0
git push origin v1.2.0
```

workflow 会先校验 tag 与 `__version__` 一致（不一致直接失败，不会误发）。想单独
验证打包链路而不发版，就在 Actions 页面手动触发 `Release`（只构建、产物存为
artifact）。

本地手工打包等价于：

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean CanvasHelper.spec
```

- **参数全在 `CanvasHelper.spec` 里（该文件已入库）**：`collect_all('playwright')`
  带上 driver（node.exe + cli.js，Edge 缺失时「安装 Chromium」要靠它），
  `collect_data_files('tzdata')` 带上 IANA 时区库。这两样漏任何一个，用户那边
  不是起不来就是登录无退路——所以不靠命令行现敲，避免「本地能打、CI 打出来少东西」。
- `console=False` 不起控制台（tkinter 主程序）。
- 入口必须是 `scripts\pyinstaller_entry.py`：`canvas_helper\__main__.py` 里的都是
  相对导入，被当成独立脚本分析会直接失败。
- `--icon` 取仓库里的 `docs\icons\canvashelper.ico`（16~128 BMP + 256 PNG 七档）。
  换图只需改源图后重跑 `python scripts\make_icon.py`，再按本命令重打包。
- 产物在 `dist\CanvasHelper\`，压缩成 zip 即可分发。
- **注意**：单文件模式（`--onefile`）会让 Playwright 的浏览器与
  `%LOCALAPPDATA%` 数据目录解包路径（`_MEIPASS`）产生问题，
  本项目只支持**单目录**模式。

### 轨道 B · PyPI（给会用 pip 的人）

```powershell
python -m build
```

只执行 `python -m build` 出 sdist 与 wheel 来**证明打包配置没坏**。

> **刻意不上传 PyPI。** 上传即等于把代码公开。第 3 节的 scrub 已随转公开
> 执行完毕（域名与课程号已配置化/示例化），是否上 PyPI 另行决定；
> CI 每次推送都跑 `pip install -e .`，已经足以持续验证 `pyproject.toml` 没写坏。

### 关于旧 pip（Python 3.9 / 3.10 自带的那两个）

`requires-python` 已降到 **3.9**，但 3.9 自带 pip 20.2、3.10 自带 pip 21.2，都低于
21.3——**editable 安装（`pip install -e .`）在它们上面走的是 `setup.py develop`
老路**，只认 `setup.py` 里的元数据，不读 `pyproject.toml` 的 `[project]`。
所以仓库根上留了一个 `setup.py` 垫片：当 setuptools >= 61（认 PEP 621）时它只做
一次空 `setup()` 调用，一切以 `pyproject.toml` 为准，不会变成第二份要同步的真相；
只有旧环境才会用到里面的精简元数据副本（版本号仍从 `__init__.py` 读）。

普通（非 editable）`pip install .` 不受影响：它走 PEP 517 构建隔离，会装上
`build-system` 里声明的 setuptools，照样能读 `[project]`。

另一处与 3.9 相关的依赖细节：**playwright 从 1.61 起要求 Python >= 3.10**，1.60 是
最后一个支持 3.9 的版本。所以 `pyproject.toml` 里写的是 `playwright>=1.49`，由 pip 按
解释器挑版本——3.9 的机器落到 1.60，3.10 以上照旧拿最新。在 3.9 上验证时用
`python -m pip show playwright` 确认一下装的是哪个版本即可。

## 5. 代码签名（需要花钱 / 需要你本人操作）

**这一项无法代做**——证书要实名购买，私钥不能由任何人代持。
目前只能给到方案：

### 现状

未签名的 exe 在 SmartScreen 下会弹「已保护你的电脑 / 未知发布者」，
用户必须点「更多信息 → 仍要运行」。这是**所有个人开发者**的常态，
不影响功能。

### 三档方案

| 档位 | 大致年费 | 验证主体 | 适合 |
| --- | --- | --- | --- |
| **OV 代码签名证书**（磁盘/云密码） | US$70–200 | 组织或个人 | 自己用、发给同学 |
| **EV 代码签名证书** | US$300–500 | 组织或个人 | 需要立刻积累 SmartScreen 信誉 |
| **SignPath.io 免费开源计划** | 0 | 走 GitHub 组织 | 仓库公开且满足其开源条件 |

> EV 证书现在也普遍走**云签名（HSM）**，不再发 U 盾，
> 但价格仍是 OV 的好几倍，且对个人项目**没有必要**——
> SmartScreen 信誉会随下载量累积，OV 签名一段时间后同样不再弹窗。

### 推荐：SignPath.io（前提是仓库先转公开）

1. 先完成第 3 节的 scrub，把仓库转 `public`；
2. 在 <https://signpath.io> 申请 OSS 免费计划（需 GitHub 仓库链接）；
3. 在 `.github/workflows/` 里加一个 `release` workflow，
   构建产物交由 SignPath 的 GitHub Action 签名；
4. 签名后校验：

   ```powershell
   Get-AuthenticodeSignature dist\CanvasHelper\CanvasHelper.exe
   # Status 应为 Valid，SignerCertificate.Subject 含组织名
   ```

### 若坚持自己买证书

```powershell
# 安装证书后
signtool sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com ^
    dist\CanvasHelper\CanvasHelper.exe
signtool verify /pa dist\CanvasHelper\CanvasHelper.exe
```

私钥务必放在硬件令牌或云 HSM 里，**不要**以 `.pfx` 文件形式放进仓库或 CI 缓存。

### 不签名的替代方案

在 README 显著位置写清两件事，实际效果也不差：

1. 「首次运行会被 SmartScreen 拦截，点『更多信息 → 仍要运行』即可」；
2. 附上 `Get-FileHash` 的 SHA-256，让用户能自行校验下载包完整性。

## 6. 发布步骤清单

- [ ] `ruff` / `mypy` / `smoke` / `pytest` 四项本地全绿
- [ ] CI 六个 Python 版本全绿
- [ ] `pyproject.toml` 与 `__version__` 一致
- [ ] `CHANGELOG.md` 的 `[未发布]` 挪进新小节并写日期
- [ ] 打标签 `git tag vX.Y.Z && git push origin vX.Y.Z`（自动出包并发布 Release）
- [ ] 发布后跑一次 `python -m canvas_helper update --brief`：确认应用内能看到这条版本
      与公告（它读的就是刚发的 Release；`--download` 可顺带验证下载包与 sha256）
- [ ] 下载 Release 里的 exe 包，在**没装 Python** 的机器上跑一遍 GUI + 同步
- [ ] `python -m build` 出包并**本地试装**一次
- [ ] （转公开时）完成第 3 节 scrub 并重新截图
- [ ] （转公开时）申请签名并更新 README 的安装说明
