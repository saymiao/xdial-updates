# XDial 更新元数据

这个公开仓库托管 XDial 的更新 feed：稳定版 `stable.json` 和测试版 `beta.json`。XDial 源码、
Release 和 ZIP 仍在 [`kafeifei/XDial`](https://github.com/kafeifei/XDial)。客户端只读下面两个
地址，不访问 GitHub API，也不持有令牌：

- `https://saymiao.github.io/xdial-updates/stable.json`：所有客户端都读。
- `https://saymiao.github.io/xdial-updates/beta.json`：只有开启 beta 的客户端会同时读它，并在
  两份清单中取较高的版本。

## 初次设置

在仓库 Settings → Pages 中把 Source 设为 **GitHub Actions**。工作流只使用本仓库的
`GITHUB_TOKEN`：读取公开的 XDial Release，提交生成的元数据，并部署 Pages。无需跨仓库
PAT。初始 `release: null` 是未部署的安全占位；第一次应直接发布已验证的正式版本。

## 通道

两个通道的 App 身份相同（`com.kafeifei.xdial.app`），只靠 feed 区分。两份 feed 的结构完全相同
（schema 1，同样的字段），只有 `channel` 字段分别是 `"stable"` 和 `"beta"`。

| | stable | beta |
|---|---|---|
| 标签 | `vX.Y.Z` | `vX.Y.Z-beta.N`（N 从 1 开始，无前导零） |
| 源 Release | 公开，不是 draft，不是 prerelease | 公开，不是 draft，必须是 prerelease |
| feed | `site/stable.json` | `site/beta.json` |
| ledger | `state/ledger.json` | `state/beta/ledger.json` |
| history | `state/history/` | `state/beta/history/` |
| audit | `state/audit/` | `state/beta/audit/` |

- 每个通道有自己的 revision 链、ledger、history 和 audit；对一个通道的操作不会改动另一个通道
  的任何文件。stable 的文件位置和格式与引入通道之前完全相同。
- stable 拒绝任何带 `-beta` 的标签，beta 拒绝正式标签形态，两份 feed 互相写不进对方的标签。
- beta 包仍须是正式身份；三个组件的短版本必须等于标签里的 `X.Y.Z`（不带 `-beta.N`）；
  若包内任一组件带 `XDialReleaseTag`，它必须等于标签；带非空 `XDialUpdateAcceptanceID` 的包被拒绝。
- 同一通道内只能前进：stable 按 `X.Y.Z` 比较，beta 按 `(X.Y.Z, N)` 比较。beta 的 `X.Y.Z` 还必须
  大于所有发布过（含已撤回）的 stable 版本。
- **构建号门禁**（两个通道共用）：新包的构建号必须大于两个通道历史上所有发布过（含已撤回）、
  且 `X.Y.Z` 不高于新包的包的构建号。`X.Y.Z` 更高的包不构成约束，所以 beta 线开放期间发
  `X.Y.Z` 更低的 stable 热修复时，不要求高于 beta 的构建号。ledger 不记录构建号，门禁从每个
  标签首次发布时的 history 记录读取，并核对标签和 SHA-256；history 缺失或不一致时拒绝发布。

## 发布

源 Release 必须已经公开，且不是 draft。它必须包含精确命名的 ZIP 与 `.sha256` 资产
（`XDial-<tag>.zip`、`XDial-<tag>.zip.sha256`），并有非空 Release 正文。发布器会下载真实资产，
核对 SHA-256、API digest（若 GitHub 提供）、ZIP 大小，以及 Host、Settings UI、System Extension
的正式身份、版本、build 和最低系统版本。全部通过后才会提交完整 feed 并部署 Pages。
带非空 `XDialUpdateAcceptanceID` 的验收包会被两个通道的发布器拒绝。部署完成后，工作流还会通过
canonical HTTPS 地址重新验证完整 JSON：`stable.json` 每次都验，`beta.json` 在 `site/` 中存在时也验；
404、重定向、无效 schema 或内容不一致都会让任务失败。代码签名无法在 Linux runner 上复验；
签名、公证和装订仍由 XDial 的 macOS Release 工作流负责，这里的验证不会把 Linux 检查误报成签名验证。

```sh
# stable（channel 省略时默认 stable）
gh workflow run publish.yml -R saymiao/xdial-updates \
  -f operation=publish -f release_tag=v0.8.1 -f request_id=my-release-001

# beta：源 Release 必须是已公开的 prerelease
gh workflow run publish.yml -R saymiao/xdial-updates \
  -f channel=beta -f operation=publish -f release_tag=v0.10.0-beta.1 -f request_id=my-beta-001
```

更新被全局串行执行（两个通道共用一个并发组，因为每次运行都部署整个 `site/`）。旧版本不能
覆盖新版本；同一 Release 的重复发布只增加审计记录，不改变 feed。history 保存每次有效 feed，
audit 保存每次成功调用的结果。验证、API 或部署失败不会把现有 Pages feed 清空；部署失败后可用
相同命令重跑。run 名称包含操作、通道和 `request_id`，调用方按 `request_id` 找到自己触发的运行。

## 撤回

撤回只接受该通道当前 feed 中的 tag，将 `release` 设为 `null`，不会删除 Release 资产，也不会
要求已安装客户端降级。撤回记录会保留，普通 `publish` 不能意外重新公开同一个 tag。撤回按通道
执行，不影响另一个通道。

```sh
gh workflow run publish.yml -R saymiao/xdial-updates \
  -f operation=withdraw -f release_tag=v0.8.1 -f request_id=withdraw-001

gh workflow run publish.yml -R saymiao/xdial-updates \
  -f channel=beta -f operation=withdraw -f release_tag=v0.10.0-beta.1 -f request_id=withdraw-beta-001
```

## 恢复（restore）

`restore` 把某个通道 history 中指定 revision 的 feed 内容，以新的、更高的 revision 重新发布
（`generatedAt` 更新为当前时间，release 字段与那次记录逐字节一致）。这是唯一能让 feed 指向更旧
release 的操作，用于 feed 出错时退回最后一份已知良好的内容。客户端拒绝 revision 倒退，所以不能
直接回退文件，只能用它。

- 新 revision 与 publish、withdraw 使用同一规则：`max(当前 revision + 1, 当前毫秒时间戳)`。
- 只接受本通道 history 中存在、且不晚于当前 revision 的记录；跨通道的 revision 会被拒绝。
- 目标 release 必须仍是 `published`；已撤回的 release 不能被恢复。目标 release 必须与它首次发布时
  的 history 记录完全一致。
- 被替换下来的当前 release 在 ledger 中标为已撤回（与 withdraw 相同），之后不能被普通 `publish`
  重新公开。下一次发布仍须高于历史最高版本。
- 当前 feed 的 release 已经等于目标时为 noop，只写审计；部署失败后可用相同命令重跑。
- 审计记录额外包含 `channel`、`historyRevision` 和 `replacedReleaseTag`。
- `restore` 不接受 `release_tag`，必须给 `history_revision`。目标 revision 在
  `state/history/`（stable）或 `state/beta/history/`（beta）里按文件名查找。

```sh
gh workflow run publish.yml -R saymiao/xdial-updates \
  -f channel=stable -f operation=restore -f history_revision=1791940209492 \
  -f request_id=restore-001
```

## 本地演练

不要手工编辑 `site/*.json` 或 `state/`。其他静态文件（包括 `site/acceptance/`）不会被发布器删除；
Pages 每次上传整个 `site/`。

需要在正式触发前本地走一次相同协议时，可以运行下面的命令。`--repo-root` 指向一个临时副本，
不要在要提交的工作区里跑，避免把演练生成的 revision 当成线上状态提交。publish 会读取公开
Release、下载并验证真实资产；withdraw 和 restore 不访问网络。

```sh
drill="$(mktemp -d)"
git clone --depth 1 https://github.com/saymiao/xdial-updates.git "$drill"

GH_TOKEN="$(gh auth token)" python3 scripts/publish_feed.py --repo-root "$drill" \
  --operation publish --release-tag v0.8.1 \
  --request-id local-bootstrap-check --run-id local --run-attempt 1

GH_TOKEN="$(gh auth token)" python3 scripts/publish_feed.py --repo-root "$drill" \
  --channel beta --operation publish --release-tag v0.10.0-beta.1 \
  --request-id local-beta-check --run-id local --run-attempt 2

python3 scripts/publish_feed.py --repo-root "$drill" \
  --channel stable --operation restore --history-revision 1791940209492 \
  --request-id local-restore-check --run-id local --run-attempt 3

git -C "$drill" status --short && git -C "$drill" diff --stat
```

演练 restore 时，先在副本里确认 `site/stable.json` 的新 revision 高于原值、release 与所选 history
记录一致、被替换的 tag 在 ledger 中变为 `withdrawn`，再丢弃整个副本。

## 测试

```sh
python3 -m unittest discover -s tests -v
```
