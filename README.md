# MirrorChyan Git Pack Uploading

把当前检出的 commit 作为快照上传到 Mirror酱 的 pack 资源，供客户端用 git pack 热更新源码。

- 快照：这个 commit 加上它完整的树和文件，不带历史，是一个非 thin 的 git pack。`depth: 1` 的检出就够了。
- 版本名：commit 的提交时间，按 UTC 格式化为 `%Y%m%d%H%M%S`。客户端只有 HEAD，要按同一规则算出自己的版本。
- 只上传分支当前最新的提交：commit 已经不是 `origin` 上该分支的最新提交时，跳过且不报错。这样重跑旧任务、或任务晚完成，都不会把旧 commit 发布成最新版本。
- 同一个 commit 再次上传：服务端会把它重新发布为最新版本，force-push 回滚到旧 commit 就是这么生效的。
- release note：不传时自动生成。push 触发时列出这次推送的提交，其他触发方式只写这个 commit 本身。

资源的更新类型必须是 `pack`，os 和 arch 留空。脚本只用 Python 标准库，不需要安装依赖。

## 用法

```yaml
name: Upload to MirrorChyan

on:
  push:
    branches: [master]
  workflow_dispatch:

# 同一时间只跑一个，后来的排队等待，避免旧 commit 晚于新 commit 上传完成
concurrency:
  group: mirrorchyan-pack
  cancel-in-progress: false

jobs:
  upload:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: master
      - uses: MirrorChyan/git-pack-uploading-action@v1
        with:
          mirrorchyan_rid: your-resource-id
          upload_token: ${{ secrets.MIRRORCHYAN_UPLOAD_TOKEN }}
          branch: master
```

- 用 `GITHUB_TOKEN` 推送或合并 PR 产生的提交不会触发 `push`。仓库里有这类自动提交时，再加上 `workflow_run`（在那个 workflow 结束后触发）或 `schedule`，由本 action 判断要不要上传。
- `actions/checkout` 需要保留默认的 `persist-credentials: true`，私有仓库检查分支最新提交时要用到这份凭据。
- 非 `push` 触发时 `github.ref_name` 是默认分支，跟踪其他分支时要设置 `branch`，并让 `actions/checkout` 检出同一个分支。

## 输入

| 名称 | 默认值 | 说明 |
|---|---|---|
| `mirrorchyan_rid` | 必填 | 资源 ID，更新类型必须是 `pack` |
| `upload_token` | 必填 | 上传 token |
| `branch` | `github.ref_name` | 资源跟踪的分支 |
| `channel` | `stable` | `stable`、`beta` 或 `alpha` |
| `working-directory` | `.` | 已检出的仓库所在目录 |
| `release_note` | 空 | 为空时按 `auto_release_note` 生成 |
| `auto_release_note` | `true` | 是否自动生成 release note |
| `api_base` | `https://mirrorchyan.com/api` | Mirror酱 API 地址 |
| `dry_run` | `false` | 只生成快照并打印要上传的内容，不实际上传 |

## 输出

| 名称 | 说明 |
|---|---|
| `skipped` | commit 已经不是分支最新提交、什么都没上传时为 `true` |
| `version_name` | 版本名 |
| `commit` | 上传的 commit |

## 测试

需要 Python 3.12+ 和 git：

```bash
cd python && python -m unittest -v test_pack_upload
```
