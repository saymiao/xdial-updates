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

`site/beta.json` 的占位（`release: null`、revision 1）合入后不会自己上线：它随下一次任意通道的
publish / withdraw / restore 运行部署（每次运行都上传整个 `site/`），之后每次运行的部署后校验都会
检查它。

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
- 两个通道的状态都必须完整：工作流在运行发布器之前检查 `site/stable.json`、`state/ledger.json`、
  `site/beta.json`、`state/beta/ledger.json` 都存在，缺任何一个就失败。发布器只在 `site/beta.json`
  和整个 `state/beta/` 都不存在时才当作「从未有过 beta」；只要留有任何一个 beta 文件，beta 状态
  就必须完整通过校验，否则拒绝发布。不要手工删除或「重置」beta 状态，要清空 beta feed 用撤回。

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
  -f operation=publish -f release_tag=<vX.Y.Z> -f request_id=<request-id>

# beta：源 Release 必须是已公开的 prerelease
gh workflow run publish.yml -R saymiao/xdial-updates \
  -f channel=beta -f operation=publish -f release_tag=<vX.Y.Z-beta.N> -f request_id=<request-id>
```

旧版本不能覆盖新版本；同一 Release 的重复发布只增加审计记录，不改变 feed。history 保存每次
有效 feed，audit 保存每次成功调用的结果。验证或 API 失败不会提交任何东西，也不会把现有 Pages
feed 清空。run 名称包含操作、通道和 `request_id`，调用方按 `request_id` 找到自己触发的运行。

**一次只派发一个运行。** 两个通道的所有操作共用一个并发组（每次运行都部署整个 `site/`）。
GitHub 对同一个并发组最多保留一个运行中和一个排队中的运行：再派发一个时，已在排队的那个会被
取消，没有执行。所以派发下一个之前，先等上一个结束（例如 `gh run watch --exit-status`）；被取消的
运行需要重新派发。stable 热修复要同批发 beta.N+1 时也是如此，逐个派发。

**部署失败时 main 上的状态已经是最终状态。** feed、ledger 和 history 在部署之前就已经提交并推送到
main。部署失败后，这份状态会跟着**任一通道**的下一次运行一起上线，因为每次运行都上传整个
`site/`。例如 beta 发布推送成功、部署失败，之后发 stable 时会把这份 beta 一起部署出去，部署后的
`beta.json` 校验也会通过。所以部署失败后，在运行另一个通道之前，必须先处理失败的那个通道：
用相同命令重跑（会是 noop 加部署），或者对该通道执行 withdraw / restore 把 feed 改成想要的内容。
工作流在部署前会把另一个通道已提交的 feed 和线上地址比对一次；不一致（或线上读不到）时在日志里
给出 warning，写明这次部署会让它从哪个 revision 变成哪个 revision。这一步只提示，不会让运行失败。

## 撤回

撤回只接受该通道当前 feed 中的 tag，将 `release` 设为 `null`，不会删除 Release 资产，也不会
要求已安装客户端降级。撤回记录会保留，普通 `publish` 和 `restore` 都不能重新公开同一个 tag。
撤回按通道执行，不影响另一个通道。部署失败的后果与发布相同，见上一节。

```sh
gh workflow run publish.yml -R saymiao/xdial-updates \
  -f operation=withdraw -f release_tag=<当前 stable tag> -f request_id=<request-id>

gh workflow run publish.yml -R saymiao/xdial-updates \
  -f channel=beta -f operation=withdraw -f release_tag=<当前 beta tag> -f request_id=<request-id>
```

## 恢复（restore）

`restore` 把某个通道 history 中指定 revision 的 feed 内容，以新的、更高的 revision 重新发布
（`generatedAt` 更新为当前时间，release 字段与那次记录逐字节一致）。这是唯一能让 feed 指向更旧
release 的操作，用于 feed 出错时退回最后一份已知良好的内容。客户端拒绝 revision 倒退，所以不能
直接回退文件，只能用它。

- 新 revision 与 publish、withdraw 使用同一规则：`max(当前 revision + 1, 当前毫秒时间戳)`。
- 只接受本通道 history 中存在、且不晚于当前 revision 的记录；跨通道的 revision 会被拒绝。
- 目标 release 在 ledger 中必须仍是 `published`；用 withdraw 撤回过的 release 不能被恢复。目标
  release 必须与它首次发布时的 history 记录完全一致。
- **工作流中的 restore 会先向 GitHub 确认源 Release 仍然可用：** 源仓库公开；Release 存在、不是
  draft、prerelease 标志与通道一致；两个资产都在，并重新下载归档，大小与 SHA-256 必须和记录一致，
  `.sha256` 资产也必须一致。任何一项不满足就拒绝，避免 feed 指向已删除、改回 draft 或被替换的
  Release。目标是 `release: null` 的 revision 时不需要这一步。
- **被替换下来的当前 release 在 ledger 中保持 `published`**，只在审计的 `replacedReleaseTag` 里记录。
  因此选错 revision 的 restore 可以再用一次 restore（指向被替换 release 的 revision）撤销。普通
  `publish` 仍然不能把它重新发出来，因为它不高于已发布的最高版本；下一次发布仍须高于历史最高版本。
  要永久退役某个 release，先把它 restore 成当前 release，再 withdraw。
- 当前 feed 的 release 已经等于目标时为 noop，只写审计；部署失败后可用相同命令重跑。
- 审计记录额外包含 `channel`、`historyRevision` 和 `replacedReleaseTag`。
- `restore` 不接受 `release_tag`，必须给 `history_revision`。目标 revision 在
  `state/history/`（stable）或 `state/beta/history/`（beta）里按文件名查找；先在 history 里核对它的
  `release` 是不是想要的内容。

```sh
gh workflow run publish.yml -R saymiao/xdial-updates \
  -f channel=<stable 或 beta> -f operation=restore -f history_revision=<revision> \
  -f request_id=<request-id>
```

## 本地演练

不要手工编辑 `site/*.json` 或 `state/`。其他静态文件（包括 `site/acceptance/`）不会被发布器删除；
Pages 每次上传整个 `site/`。

需要在正式触发前本地走一次相同协议时，在仓库根目录运行下面的命令，`--repo-root` 指向一个临时
clone。不要在要提交的工作区里跑，避免把演练生成的 revision 当成线上状态提交。演练完丢弃整个
临时目录。

```sh
drill="$(mktemp -d)"
git clone --depth 1 https://github.com/saymiao/xdial-updates.git "$drill"

# publish 会读取公开 Release、下载并验证真实资产（stable 包约 56 MB）。
# tag 必须高于该通道 ledger 的 highestPublishedVersion，且源 Release 已公开；
# beta 的 X.Y.Z 还必须高于 stable 的 highestPublishedVersion。
GH_TOKEN="$(gh auth token)" python3 scripts/publish_feed.py --repo-root "$drill" \
  --operation publish --release-tag <下一个 stable tag> \
  --request-id local-stable-check --run-id local --run-attempt 1

GH_TOKEN="$(gh auth token)" python3 scripts/publish_feed.py --repo-root "$drill" \
  --channel beta --operation publish --release-tag <已公开的 beta prerelease tag> \
  --request-id local-beta-check --run-id local --run-attempt 2

# restore 默认也会向 GitHub 确认并下载目标 Release 的归档。
# --skip-release-check 只用于离线演练，跳过这一步；在 GitHub Actions 里会被拒绝。
python3 scripts/publish_feed.py --repo-root "$drill" \
  --channel stable --operation restore --history-revision <revision> --skip-release-check \
  --request-id local-restore-check --run-id local --run-attempt 3

git -C "$drill" status --short && git -C "$drill" diff --stat
```

演练 restore 时，在副本里确认：`site/stable.json` 的新 revision 高于原值；release 与所选 history
记录一致；被替换的 tag 在 ledger 中仍是 `published`；再 restore 回原来的 revision 可以恢复原状。
withdraw 不访问网络，可以同样离线演练。

## 测试

```sh
python3 -m unittest discover -s tests -v
```
