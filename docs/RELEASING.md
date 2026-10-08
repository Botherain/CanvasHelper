# 发布流程

本文件记录「程序发行还需要哪些工作」里**需要花钱或需要你本人操作**的部分。
代码侧能自动化的都已经进 CI 了，这里只讲剩下的。

## 1. 每次发布前的固定动作

```powershell
python -m ruff check canvas_helper tests scripts  # 0 错误
python -m mypy                             # Success: no issues found
python tests\smoke.py                      # 129 通过, 0 失败
python -m pytest                           # 20 passed
```

改了 `pyproject.toml` 的 `version` 就必须同步改
`canvas_helper/__init__.py` 的 `__version__`——自检第 [9] 节会断言两者相等，
不一致直接红。

CI（`lint` + `smoke` × Python 3.11/3.12/3.13/3.14）全绿后才能发。

## 2. 版本与变更

- 版本号：语义化版本，改 `pyproject.toml` 与 `__init__.py` 两处。
- `CHANGELOG.md`：把 `[未发布]` 下的内容挪进新的版本小节，补日期。

## 3. 转公开（public）前必须 scrub 的内容

仓库目前是**私有**的，因为里面硬编码了本校域名与真实课程号。
转 `public` 前，下列位置必须处理干净：

| 文件 | 位置 | 内容 |
| --- | --- | --- |
| `canvas_helper/auth.py` | 常量 | `CANVAS_HOME`、`LOGIN_PROBE` 里的学校域名 |
| `canvas_helper/canvas_api.py` | 约 160 / 384 / 440 行 | 回退用的 `html_url`、`assignments` 的 `html_url` 兜底 |
| `canvas_helper/deskbox.py` | 约 25 行 | DeskBox 数据目录相关的本机路径示例 |
| `canvas_helper/extras.py` | 约 57 行 | `.ics` / 摘要里出现的域名 |
| `canvas_helper/gui.py` | 约 394 行 | 「打开 Canvas」按钮的固定链接 |
| `canvas_helper/classify.py` | 约 79 / 180 行 | 示例里的课程号 `CS0001` 之类 |
| `canvas_helper/canvas_api.py` | 约 331 / 332 行 | 注释里举的课程号 |
| `tests/smoke.py`、`tests/test_canvas_api.py` | fixture 与断言 | `10001` / `10003` / `10005` 一类的课程 id、`CS0003` 等 |
| **`docs/screenshots/gui-main.png`** | 整张图 | 截图里有课程号与通知标题 |

> ⚠️ **截图无法文本替换**。转公开前请**重新截一张干净的**
> （先在 GUI 里清空或换一个无敏感数据的账号），或者直接从 README 里移除。
> 用 `scripts\capture_gui.ps1 -OutPath <路径>` 重新生成。

`LICENSE` 与 README 徽章里的 GitHub 用户名 `Botherain` 保留，那是署名。

## 4. 打包分发（双轨）

### 轨道 A · PyInstaller（给不想碰 Python 的人）

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean ^
    --name CanvasHelper ^
    --windowed ^
    --icon "docs\icons\canvashelper.ico" ^
    --collect-all playwright ^
    --hidden-import canvas_helper ^
    scripts\pyinstaller_entry.py
```

- `--windowed` 不起控制台（tkinter 主程序）。
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

> **刻意不上传 PyPI。** 上传即等于把代码公开，与第 3 节的 scrub 冲突——
> 而 scrub 需要重新设计配置项（域名与课程号改成可配置），
> 那是转公开时才做的事。CI 每次推送都跑 `pip install -e .`，
> 已经足以持续验证 `pyproject.toml` 没写坏。

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
- [ ] CI 四个 Python 版本全绿
- [ ] `pyproject.toml` 与 `__version__` 一致
- [ ] `CHANGELOG.md` 的 `[未发布]` 挪进新小节并写日期
- [ ] 打标签 `git tag vX.Y.Z && git push --tags`
- [ ] `python -m build` 出包并**本地试装**一次
- [ ] （可选）PyInstaller 出单目录包，实机跑一遍 GUI + 同步
- [ ] （转公开时）完成第 3 节 scrub 并重新截图
- [ ] （转公开时）申请签名并更新 README 的安装说明
