# colab-dfs-ext —— google-colab-cli 的零浏览器 Drive 挂载扩展

把「无浏览器认证的 Google Drive 挂载」做成 `colab` 的子命令组 `dfs`，
**不改动 google-colab-cli 的任何文件**：colab-cli 升级不受影响、无需重新集成，
最坏情况一条命令修复。本目录自成一个项目，可单独拷走使用（纯 Python 标准库，无第三方依赖）。

```
colab dfs mount   [-s SESSION] [path]   # 零浏览器挂载（DriveFS + 运行时内的凭据 shim）
colab dfs umount  [-s SESSION] [path]
colab dfs status  [-s SESSION] [path]
colab dfs consent [--flavor dfs] [--minimal] ...   # 一次性 Google 同意 -> refresh token
colab dfs show | token
colab dfs doctor                        # 体检：注入/凭据/会话，附修复指引
colab dfs install                       # 重新注入本扩展（升级后如果 dfs 消失就跑这个）
```

所有子命令接受 `-- -任意参数` 透传给底层工具，本扩展没暴露的 flag 依然可用。

## 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/deluxebear/colab-dfs-ext/main/install.sh | sh
```

（wget 亦可：`wget -qO- ... | sh`。脚本只做三件事：下载仓库 tarball → 解压到临时目录 →
跑 `python3 install.py`，最后打印后续步骤；可用 `COLAB_DFS_REPO` / `COLAB_DFS_REF` 覆盖来源。）

## 环境要求

- Python 3.8+（`install.py` 与扩展本体均为纯标准库）
- 已安装并可用 [`google-colab-cli`](https://pypi.org/project/google-colab-cli/)（`uv tool install google-colab-cli`），即 `colab` 命令
- 一个已连接的 colab 会话（`colab connect -s <session>`）
- Colab 运行时自带 `/opt/google/drive`（DriveFS），无需自行上传

## 使用步骤

### 第 1 步 · 安装扩展

一键安装（见上）或手动安装：

```bash
git clone https://github.com/deluxebear/colab-dfs-ext
cd colab-dfs-ext            # 即本目录
python3 install.py          # 任意 python3 均可
colab dfs doctor            # 验证注入成功
```

`install.py` 做两件事：

1. 把扩展源码拷到 `~/.colab-dfs-ext/`（**在 colab-cli 的 venv 之外**，升级永不触碰）；
2. 往 colab-cli 环境的 site-packages 里写**唯一的一个文件** `colab_dfs.pth`（~600 字节）。

常用安装参数：

```bash
python3 install.py --status        # 查看 .pth / 源码 / colab_cli 各自的位置
python3 install.py --link          # 开发模式：符号链接本仓库，改代码即生效
python3 install.py --site-dir DIR  # 指定目标 site-packages
```

### 第 2 步 · 一次性授权（只需做一次）

```bash
colab dfs consent            # 浏览器弹出 Google 同意页 -> 允许
colab dfs show               # 查看已存凭据（refresh token / GaiaId / email）
```

默认使用逆向出来的 DriveFS 内置客户端（flavor `dfs`，Google 自家
“Google Drive for desktop”，已验证应用），localhost 回调自动接住，无需复制授权码。
其他 flavor：`--flavor sdk`（colab-cli 自带客户端，headless 可用但易被 Google 拦）、
`--flavor custom --client-id ... --client-secret ...`（自己的 GCP 项目，长期最稳）。
已同意过的机器可以 `colab dfs consent --import`，完全无浏览器。

凭据存于 `~/.config/colab-cli/dfs_credentials.json`，之后所有挂载零浏览器。

### 第 3 步 · 日常挂载

```bash
colab connect -s test        # 连上 colab 会话（如未连接）
colab dfs mount -s test      # 挂到 /content/drive，几秒完成
colab dfs status -s test     # 查看挂载状态
colab dfs umount -s test     # 卸载

colab dfs mount -s test --force --purge-state   # 重挂：清旧进程/旧目录/旧本地库
colab dfs mount -s test --dry-run               # 只打印配置和远端 payload
colab dfs mount -s test --readonly              # 只读挂载
```

### 可选 · 让官方 `drivemount` 也走零浏览器

```bash
export COLAB_DFS_DEFAULT=1     # 写进 shell profile 即可
colab drivemount -s test       # 走 dfs 扩展；凭据缺失时自动回落官方浏览器流程
```

## 升级共存（实测）

| 场景 | 结果 |
|---|---|
| `uv tool install google-colab-cli --reinstall`（重建工具环境） | `.pth` **幸存**（uv 只重装自己的包，不清理外来文件），`colab dfs` 直接可用 |
| `colab update --install` | 内部会跑 `uv tool install -U`；扩展注册时包装了 `update` 命令，升级完成后自动检查并重新接线（`[colab-dfs] extension still wired: ...`） |
| 极端情况（换 Python 版本、手动清 venv） | `colab dfs` 消失，但 `~/.colab-dfs-ext` 还在：`python3 ~/.colab-dfs-ext/install.py` 一条命令恢复 |

## 卸载

```bash
python3 install.py --uninstall          # 摘掉 .pth
rm -rf ~/.colab-dfs-ext                 # （可选）删掉扩展源码副本
```

## 排障

```bash
colab dfs doctor                # 体检：注入/凭据/会话，附修复指引
COLAB_DFS_DEBUG=1 colab dfs …   # 加载器/注入的详细诊断
```

- `colab dfs` 命令消失 → `python3 ~/.colab-dfs-ext/install.py` 重新接线。
- 改了本仓库代码但行为没变 → `~/.colab-dfs-ext` 里是安装时的快照，重跑
  `python3 install.py`（或改用 `--link`），`source_dir()` 的查找顺序是
  环境变量 `COLAB_DFS_EXT` → 当前代码位置 → `~/.colab-dfs-ext`。

## 注入原理（为什么升级不会破坏它）

- colab-cli 是 Typer 应用，`colab_cli.cli` 暴露模块级 `app` 对象，且官方命令模块本身就是
  `register(app)` 约定 —— 扩展只是往这个 `app` 上再挂一个 `dfs` 子组，**零侵入**。
- `.pth` 是 site.py 唯一会自动执行的钩子：一行 `import os, sys; exec(compile("try: ... import
  colab_dfs_loader ..."))`，把 `~/.colab-dfs-ext` 加进 `sys.path` 并急切 import loader；
  loader 调 `colab_dfs_ext.register(app)`。启动开销实测 **≈0**（323ms vs 324ms：colab 本来就要
  import 这些模块）。
- 防御性：snippet 全部包在 try/except 里，扩展缺失/损坏时静默跳过，**永远不会弄坏 `colab` 本身**
  （`COLAB_DFS_DEBUG=1` 可看跳过原因）。

## 已知实现细节 / 坑

- `remote.exec_code` 用 colab_cli 自己的 `ColabRuntime.execute_code()`（无子进程、无临时文件），
  **必须在 finally 里 `runtime.stop()`**——否则内核客户端的非 daemon 线程会把 `colab` 进程吊住
  不退出（实测症状：结果都打印了但命令不返回）。
- 凭据与独立版工具共用：`~/.config/colab-cli/dfs_credentials.json`。
- 背景技术细节（DriveFS 的凭据契约、metadata shim、三条去浏览器方案）见上层仓库
  `colab-drivemount-re/docs/REVERSE.md`。

## 目录结构

```
extension/
├── install.sh               # curl | sh 一键安装引导脚本（POSIX sh）
├── install.py               # 安装/修复/卸载/查看状态（纯标准库）
├── colab_dfs_loader.py      # .pth 启动钩子加载的 loader
└── colab_dfs_ext/           # 扩展本体（会被拷贝/链接到 ~/.colab-dfs-ext/）
    ├── __init__.py          # register(app) 入口
    ├── inject.py            # .pth 写入与状态管理
    ├── commands.py          # `colab dfs` 各子命令（typer）
    ├── dfs_creds.py         # OAuth 同意 / 凭据存取
    ├── drive_mount.py       # 远端挂载脚本（metadata shim + DriveFS）
    └── remote.py            # 通过 colab_cli API 在运行时内执行代码
```
