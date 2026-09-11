# XDial 更新元数据

这个公开仓库只托管 XDial 稳定版更新 feed。XDial 源码、Release 和 ZIP 仍在
[`kafeifei/XDial`](https://github.com/kafeifei/XDial)。客户端只读
`https://saymiao.github.io/xdial-updates/stable.json`，不访问 GitHub API，也不持有令牌。

## 初次设置

在仓库 Settings → Pages 中把 Source 设为 **GitHub Actions**。工作流只使用本仓库的
`GITHUB_TOKEN`：读取公开的 XDial Release，提交生成的元数据，并部署 Pages。无需跨仓库
PAT。初始 `release: null` 是未部署的安全占位；第一次应直接发布已验证的正式版本。

## 发布

源 Release 必须已经公开，且不是 draft 或 prerelease。它必须包含精确命名的 ZIP 与
`.sha256` 资产，并有非空 Release 正文。发布器会下载真实资产，核对 SHA-256、API digest
（若 GitHub 提供）、ZIP 大小，以及 Host、Settings UI、System Extension 的正式身份、版本、
build 和最低系统版本。全部通过后才会提交完整 `stable.json` 并部署 Pages。
带非空 `XDialUpdateAcceptanceID` 的验收包会被 stable 发布器拒绝。部署完成后，工作流还会通过
canonical HTTPS 地址重新验证完整 JSON；404、重定向、无效 schema 或内容不一致都会让任务失败。
代码签名无法在 Linux runner 上复验；签名、公证和装订仍由 XDial 的 macOS Release 工作流
负责，这里的验证不会把 Linux 检查误报成签名验证。

```sh
gh workflow run publish.yml -R saymiao/xdial-updates \
  -f operation=publish -f release_tag=v0.8.1 -f request_id=my-release-001
```

更新被全局串行执行。旧版本不能覆盖新版本；同一 Release 的重复发布只增加审计记录，不改变
feed。`state/history/` 保存每次有效 feed，`state/audit/` 保存每次成功调用的结果。验证、API 或
部署失败不会把现有 Pages feed 清空；部署失败后可用相同命令重跑。

## 撤回

撤回只接受当前 feed 中的 tag，将 `release` 设为 `null`，不会删除 Release 资产，也不会要求
已安装客户端降级。撤回记录会保留，普通 `publish` 不能意外重新公开同一个 tag。

```sh
gh workflow run publish.yml -R saymiao/xdial-updates \
  -f operation=withdraw -f release_tag=v0.8.1 -f request_id=withdraw-001
```

不要手工编辑 `site/stable.json` 或 `state/`。其他静态文件（包括 `site/acceptance/`）不会被发布器
删除；Pages 每次上传整个 `site/`。

需要在正式触发前本地走一次相同协议时，可在仓库根目录运行下面的命令。它会读取公开 Release、
下载并验证真实资产，然后只修改本地 `site/` 和 `state/`；使用临时目录或干净 checkout，避免把
演练生成的 revision 当成线上状态提交。

```sh
GH_TOKEN="$(gh auth token)" python3 scripts/publish_feed.py \
  --operation publish --release-tag v0.8.1 \
  --request-id local-bootstrap-check --run-id local --run-attempt 1
```
