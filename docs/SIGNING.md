# 代码签名（SignPath Foundation / Certum）

本文件解决一件事：**怎么让 Windows 用户下载后不再看到「未知发布者 / 已保护你的电脑」**。

结论先写在这：

| 方案 | 价格 | 适合 | CI 无人值守签名 |
| --- | --- | --- | --- |
| **SignPath Foundation** | **0**（开源项目） | 本项目首选 | ✅ 官方 GitHub Action |
| Certum Open Source Code Signing | ≈ €49/年 | 想签在自己名下 | ⚠️ 云签名要手机确认，只能本地签 |
| 传统 OV（DigiCert / Sectigo / 代理） | $150–300/年 | 需要显示公司名 | 视 CA：云签名或 U 盾 |
| EV | $400+/年 | **不要买** | — |

两个前提（2026 年）：

- 2023-06 起，OV/EV 的私钥**必须**放 HSM / U 盾，CA 不再给 `.pfx` 文件；
- 2024 年起 **EV 不再「秒过」SmartScreen**，和 OV 一样要累积信誉（微软官方文档原话：
  “no longer recommended for SmartScreen bypass”）；
- Azure Artifact Signing（原 Trusted Signing，$9.99/月）个人只对美国/加拿大开放，
  港/内地个人不适用。

---

## 一、SignPath Foundation（免费，推荐）

微软官方文档与 SignPath 都把这条列为开源项目的标准路径：**证书签发给 SignPath
Foundation，你通过他们的托管流水线签名，私钥永远不落到你机器上**。用户看到的发布者
是 `SignPath Foundation`。

### 1. 申请前自检（缺任何一条都会被拒）

| 条件 | 本项目现状 |
| --- | --- |
| 公开仓库 | ✅ `github.com/Botherain/CanvasHelper` |
| OSI 认可的开源许可证 | ✅ MIT（`LICENSE`） |
| 不含恶意 / 可疑组件 | ✅ 只依赖 `playwright` 与 `tzdata`；`_internal` 里的 `node.exe` 是 Playwright 官方分发 |
| 无可商用专有模块（不能有「开源版 + 付费版」双许可） | ✅ 全部 MIT |
| 能从源码重现发布物 | ✅ `CanvasHelper.spec` + `release.yml`（tag → 构建 → 压包） |
| 构建全部跑在 GitHub 托管 runner 上 | ✅ `windows-latest` |
| README 写明怎么构建 | ✅「从源码运行」+ `docs/RELEASING.md` |

### 2. 提交申请

1. 打开 <https://signpath.org/apply>（GitHub 账号登录）；
2. 表单里通常要填：项目名、仓库地址、许可证、维护者、发布方式、构建流程简述；
3. 审核是人工的，**几天到两周**都算正常；期间他们可能会在仓库里开 issue 让你补构建说明。

可直接复制的申请文案（英文，按需改）：

```text
Project: CanvasHelper
Repository: https://github.com/Botherain/CanvasHelper
License: MIT
What it is: A Windows desktop app (Python + tkinter) that syncs Canvas (LMS)
course files to local folders and writes assignment deadlines into DeskBox.
Distribution: GitHub Releases only. Each release ships a PyInstaller one-dir
build zipped as CanvasHelper-vX-win64.zip (main exe: CanvasHelper.exe),
plus a wheel and sdist.
Build: fully reproducible from source on GitHub-hosted runners via
.github/workflows/release.yml (Windows). PyInstaller spec is committed
(CanvasHelper.spec); the workflow runs the built exe with --selftest as a gate.
Signing: we only need the main CanvasHelper.exe signed (one PE file per release).
No proprietary components; no dual licensing.
```

### 3. 通过后接入 CI（三步）

他们会给你：`organization-id`、`project-slug`、`signing-policy-slug`，以及一个
**API Token**（存成仓库 Secret `SIGNPATH_API_TOKEN`，放在 Settings → Secrets）。

`release.yml` 里，**在「冒烟：exe 必须能启动」之前、打 zip 之前**插入：

```yaml
      - name: 上传待签名 exe
        id: upload-unsigned
        uses: actions/upload-artifact@v4
        with:
          name: unsigned-exe
          path: dist/CanvasHelper/CanvasHelper.exe
          archive: false            # 不包成 zip：签名后直接拿回单个 exe
          if-no-files-found: error

      - name: 提交签名请求
        uses: signpath/github-action-submit-signing-request@v3
        with:
          api-token: ${{ secrets.SIGNPATH_API_TOKEN }}
          organization-id: ${{ vars.SIGNPATH_ORG_ID }}
          project-slug: canvashelper
          signing-policy-slug: release-signing
          github-artifact-id: ${{ steps.upload-unsigned.outputs.artifact-id }}
          wait-for-completion: true
          output-artifact-directory: signed
          skip-decompress: true     # 对应 archive: false

      - name: 用签名后的 exe 覆盖
        shell: pwsh
        run: |
          Copy-Item signed\CanvasHelper.exe dist\CanvasHelper\CanvasHelper.exe -Force
          $sig = Get-AuthenticodeSignature dist\CanvasHelper\CanvasHelper.exe
          if ($sig.Status -ne 'Valid') { throw "签名无效：$($sig.Status)" }
          Write-Host "签名者：$($sig.SignerCertificate.Subject)"
```

顺序必须是：**构建 exe → 签名 → 跑 `--selftest` → 打 zip → 发 Release**。
签名改动了文件字节，所以签名后再校验、再压包；更新包与 Release 的 sha256 会随之变化，
不需要改代码（`updater` 读的是 GitHub 自动算的 digest）。

若仓库是私有的，还要给 job 加 `permissions: {contents: write, actions: read}`。

### 4. 验收

```powershell
Get-AuthenticodeSignature .\CanvasHelper.exe | Format-List Status, SignerCertificate
# Status 应为 Valid，Subject 里能看到 SignPath Foundation
```

注意：签了名**不等于立刻没有 SmartScreen 提示**——新证书同样要累积信誉，但
「未知发布者」会消失，后续版本会比未签名时少很多拦截。

---

## 二、备选：Certum Open Source Code Signing（≈ €49/年）

适合「想让发布者写自己名字」的情况。购买页：
<https://shop.certum.eu/open-source-code-signing-on-simplysign.html>（云签名，不需 U 盾；
官网偶尔显示缺货，也可买「卡 + 读卡器」版本 €69 起）。

**要交的材料**（一项都不能少）：

1. 身份验证：在线自动验证（推荐）/ 本人持证件拍照 / 公证 / 就近登记点；
2. **本人名义的水电或电话账单**（地址证明，宿舍账单不在你名下会被打回）；
3. **公开的项目地址**，且能看出你是作者（GitHub 仓库 + 提交记录）；
4. 证书内容固定为 `Open Source Developer <你的名字>`，**不得用于商业分发**（会被吊销）；
5. 2026-02-27 起个人代码签名证书有效期约 **459 天**，到期要重新签发。

**签法**：

```powershell
signtool sign /fd SHA256 /tr http://time.certum.pl /td SHA256 CanvasHelper.exe
Get-AuthenticodeSignature .\CanvasHelper.exe
```

**CI 限制**：SimplySign 云签名要手机动态码确认，**不适合 GitHub Actions 无人值守**。
两条路：本地签完再上传 Release 资产，或自建一台 runner 插卡/登录后由它签。

---

## 三、常见坑

- **签名必须在打 zip 之前**：先签名再压包，否则 zip 里还是旧字节；
- **只签 `CanvasHelper.exe` 就够**（SmartScreen 看的是用户双击的那个 PE）；
  `_internal\` 里的第三方二进制不要改，改了反而破坏 Playwright 的完整性；
- **别把 `README.txt` 放进 `CanvasHelper\`**：会被「一键更新」的安全阀判成「目录里有额外文件」，
  从此无法自动更新（`README.txt` 应放在 zip 根，与 `CanvasHelper\` 平级）；
- **`--selftest` 要在签名之后跑**：既验证签名没破坏 exe，也验证打包没漏模块；
- 签名不改变我们的更新流程：`updater.download()` 校验的是 GitHub 提供的 sha256 digest。

---

## 四、当前状态（2026-10-26）

- [x] 仓库公开、MIT、可从源码构建、CI 在 GitHub-hosted runner
- [x] 构建链路就绪（`release.yml`：tag → exe → `--selftest` → zip → Release）
- [x] 未签名说明与验签方法写进 README / zip 内 `README.txt`
- [ ] SignPath Foundation 申请（<https://signpath.org/apply>）
- [ ] 通过后：加 Secret `SIGNPATH_API_TOKEN`、Variable `SIGNPATH_ORG_ID`，插入上面的签名步骤
- [ ] 签名后的首个版本：README 里把「未签名」改成「已签名（SignPath Foundation）」
