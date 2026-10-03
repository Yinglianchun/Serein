# Serein 排错指南

更新日期：2026-10-03。适用于当前 Serein 一键脚本。第一次安装见 [部署指南与常用命令](deployment-guide.md)。下方示例默认端口是 **18217**；修改过端口时请替换。

## 先按这个顺序查

1. 进入当前 Serein 安装目录，运行 `se`，选 **12 · 故障排查**。
2. 按报告中的 `FAIL` 先修复环境、配置或服务，再处理 `WARN`。最近日志可能包含已经解决的历史错误。
3. 菜单 **4 → 查看状态／最近日志** 查详情；需要重启时用菜单 **2**。
4. 本机能连、别的设备不能连，检查菜单 **5** 的访问入口、防火墙和云安全组。
5. 网页正常但模型／召回失败，检查网页模型配置和检索准备状态。

没有 `se` 或 Docker 没启动时，也可以直接排查：

```sh
python3 scripts/doctor.py
```

Windows 用 `python .\scripts\doctor.py`。排查只读，不重启、不修改配置、不调用模型，只检查当前实例；日志仅显示错误类别和次数，不打印原句。独立排查发现硬错误时退出码为 1。

## 现象速查

| 现象 | 先看哪里 | 下一步 |
| --- | --- | --- |
| 找不到 `se` | 当前目录、PATH | 进入安装目录，重开终端或用原脚本 |
| 未找到 Python／Node／Docker | 环境与部署方式 | 按所选路线安装依赖 |
| Docker daemon 不可用 | Docker 服务 | Linux 检查 daemon；Windows 启动 Desktop |
| 构建下载超时 | 失败发生的步骤 | 分清镜像、pip、npm 或 Git 网络 |
| `not a directory` 挂载错误 | `deploy/config.toml` | 确认是文件，保留误建目录再修复 |
| `address already in use` | 当前端口 | 查冲突实例，不要直接杀未知进程 |
| `/ready` 返回 503 | 网关、memory、页面 | 菜单 4 查两个服务日志 |
| 本机正常、手机／公网不通 | 监听和网络入口 | 菜单 5、防火墙、安全组 |
| 网页要求用户名密码 | 页面鉴权 | 输入安装页面账号；不用 Gateway Key |
| 聊天／MCP 401 或 403 | Gateway 鉴权或上游返回 | 核对对应层的 Key 和地址 |
| 400／404、模型不存在 | 模型名、API 地址、协议 | 保存设置后刷新客户端模型列表 |
| 429 | 上游额度与频率 | 检查余额／配额并减少请求 |
| 聊天正常但没有记忆 | 网关接入、自动记忆、索引 | 查检索校验与召回观察 |
| 更新被拒绝或构建失败 | 更新提示和源码改动 | 保留备份，修复后重试菜单 1 |

## 找不到 se 或启动入口用错

`se` 按当前目录打开实例，先进入目录。看到 `Please enter a Serein directory before running se` 时，是目录不对。

```sh
cd ~/Serein
bash scripts/one_click.sh
```

```powershell
cd D:\Serein
powershell -ExecutionPolicy Bypass -File .\scripts\one_click.ps1
```

首次注册快捷命令后可能需要新开终端；已有其他同名 `se` 命令时不会覆盖。原启动入口始终可用。不要从旧 Ombre 目录运行 `ob` 来管理 Serein。

## Python Node 或 Docker 环境不满足

Docker 菜单需要宿主机 Python 3.9+；直跑安装需要 Python 3.11+、Node.js 22.12+ 和 npm。检查命令：

```sh
python3 --version
node --version
npm --version
docker --version
docker compose version
docker info
```

只检查所选路线需要的工具。Windows 的 Python 可用 `python --version` 或 `py -3 --version`。安装后重开终端，确认命令已加入 PATH。

Windows Docker Desktop 需要启动且使用 Linux containers。Linux 上有 `docker` 命令但 `docker info` 失败，检查 Docker daemon 和当前账号权限；使用 systemd 的机器可查看 `systemctl status docker --no-pager`。不要为了绕过权限错误把 Docker socket 改成所有人可写。

Termux 缺编译工具时按 [部署指南](deployment-guide.md#安卓-termux)补齐。依赖安装失败后，菜单 12 仍可排查；修复后重新执行部署，避免自行更换实例虚拟环境里的零散包。

## SSH 连接超时或被拒绝

SSH 在 Serein 启动前就可能失败，应先解决服务器连接：

```sh
ssh 用户名@服务器地址
```

把用户名、地址和 SSH 端口换成服务器实际值。超时先查云安全组、系统防火墙、当前公网 IP 是否允许；被拒绝检查 SSH 服务是否在监听、端口是否正确；鉴权失败核对登录用户和 SSH 密钥。

使用默认 SSH 端口时放行 TCP 22，并尽量限制为自己的管理 IP。若改过 SSH 端口，放行对应端口。Serein 的 18217 端口与 SSH 端口是两回事，网页访问规则不能替代 SSH 规则。

## 构建时下载超时或机器卡住

先找到失败阶段，不要一律归因于 Serein 配置：

| 失败位置 | 说明 | 排查方向 |
| --- | --- | --- |
| `FROM`／`failed to resolve source metadata` | Docker 基础镜像获取失败 | Docker Hub、DNS、Docker daemon 网络 |
| `pip install` | Python 依赖下载或编译失败 | 包源网络、编译工具、具体失败依赖 |
| `npm ci` | 网页依赖下载失败 | npm 网络与依赖安装报错 |
| `npm run build` | 页面编译失败 | 首条编译错误、内存是否不足 |
| Git 拉取失败 | 更新源码未准备好 | Git 安装与 GitHub 连通性 |

当前镜像基于 `python:3.13-slim` 和 `node:22-bookworm-slim`，可以在 Docker 主机上分别试拉以定位网络：

```sh
docker pull python:3.13-slim
docker pull node:22-bookworm-slim
```

这些命令只用于验证镜像下载，不会安装 Serein。超时应先修复可信网络／代理配置，再重试菜单 1；更换 VPS 不是默认处理方式。需要跨机器传镜像时，必须匹配当前源码、CPU 架构和实例实际镜像名，旧 Ombre 的镜像名与 ACR 重打标签命令不能直接套用。当前一键脚本不提供自动 ACR 中转。

`Killed`、退出码 137 或机器失去响应可能与内存不足有关，结合系统日志确认。先停同机旧服务，留出构建空间；脚本会依次构建两个镜像。不要通过不断同时重试增加负担。

普通重新部署会先停止本实例，构建失败时保持停止；修复后重跑菜单 1。更新的 Git 拉取阶段失败不会停服。不要把“构建失败后网页打不开”误认为数据库已经丢失。

## config.toml 挂载报 not a directory

Docker 需要把 `deploy/config.toml` 文件只读挂到容器 `/config/config.toml`。检查路径类型：

```sh
ls -ld deploy/config.toml
```

```powershell
Get-Item -LiteralPath .\deploy\config.toml | Select-Object FullName, PSIsContainer
```

如果该路径误成目录，先停止当前实例并检查目录内有什么，保留副本或改名，再恢复正确的实例配置文件。已有配置优先从可信备份恢复；全新空实例可让安装流程生成配置。不要直接递归删除这个路径，也不要用旧版 `config.yaml` 替代。

Docker 配置里的数据库路径与直跑不同。不要只把示例配置复制过去就假定可用，也不要打印整份配置公开求助。

## 端口占用或服务没有启动

用菜单 12 核对当前配置，菜单 4 查看状态。网关默认端口 18217；直跑内部默认 memory 18218、页面预览 18219，安装时可以修改。Docker memory 端口在内部网络，不直接映射为主机 18218。

Linux 可用 `ss -ltnp` 查看监听；Windows 用：

```powershell
Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq 18217
```

将端口换成实际值，再确认占用者属于哪个实例。不要因为看到 PID 就直接结束未知服务。需要停止 Serein 时用对应目录的菜单 4，不要手工删除运行控制文件。

## ready 返回 503 或连接被拒绝

```sh
curl --max-time 8 -i http://127.0.0.1:18217/ready
```

Windows 用 `curl.exe`。

- **200**：网关可访问，记忆服务和页面预览通过本轮健康检查；不证明上游模型或召回配置正常。
- **503**：网关在响应，但内部依赖未就绪；先查两个服务日志。
- **连接被拒绝／超时**：检查端口、服务启动结果和当前机器。远程电脑上的 `127.0.0.1` 不指向 VPS。
- **根页面返回 401**：未带页面账号的请求被要求登录是正常情况，不要把它当成 `/ready` 失败。

当前网关健康入口是 `/ready`，不能照旧教程对外测试 `/health`。

## 本机能打开但手机或公网不能

按顺序检查：

1. 在安装机器上测试实际端口的 `/ready`。
2. 菜单 **5 → IP 入口** 是否已应用；直接从其他设备访问需要网关监听 `0.0.0.0`。
3. 客户端填的是安装机器的可访问 IP，而不是 `127.0.0.1` 或 `0.0.0.0`。
4. 局域网检查同一网络、设备隔离和电脑防火墙；VPS 检查云安全组与系统防火墙的实际端口。
5. HTTPS 检查域名解析、证书和 nginx 到本机网关的转发。

本机正常、外部失败通常先看入口和网络，不需要重导记忆或重建向量。已有 HTTPS 的实例切换 IP 入口时，脚本可能要求先处理 nginx 入口，避免残留代理与配置冲突。

## 页面密码 Gateway Key 和上游 Key 混了

| 正在连接谁 | 用什么凭据 |
| --- | --- |
| 浏览器登录 Serein 页面 | 安装时设置的页面用户名／密码 |
| 聊天客户端或静态 MCP 访问 Serein | `deploy/secrets/api-token` 中的 Gateway Key |
| Serein 请求模型提供方 | 网页“设置 → 模型”里对应上游的 API Key |
| MCP OAuth 的 Serein 授权页 | Gateway Key，后续由客户端使用 OAuth 凭据 |

客户端 API Key 输入框填原始 Gateway Key；自定义 `Authorization` 请求头才写 `Bearer ` 前缀。改页面密码用菜单 0，改 Gateway Key 用菜单 6，两者不能互相修复。

菜单 6 会让旧静态 Key 和已发放 OAuth 凭据失效，所有客户端更新 Key，OAuth 客户端重新授权。上游返回 401 时，改 Serein 的 Gateway Key 通常无效，应检查对应模型站点的密钥。

## 模型列表为空或出现 400 404 429

先核对 Base URL、上游协议、真实模型号和设置是否保存。客户端连接 `你的入口/v1`，拉取列表后用显示的模型名；上游模型地址和客户端访问 Serein 的地址不要混填。

Embedding／Reranker 被排除在聊天列表之外是正常行为。提供方不支持拉取模型时可以手动添加；拉取成功仍需要保存。更改模型列表后刷新客户端列表，不要使用已移除的名字。

400／404 看上游具体错误：模型名、协议、工具调用格式或该模型能力可能不匹配。先用一个普通文本请求验证，不要直接批量重试迁移任务。429 先查余额、配额和限流；反复点击重试不会增加配额。

需要手动核对 Serein 模型列表时，在安装目录运行下面只读查询；输出模型名，不输出 Key。地址换成实际入口，不要把真实密钥写进命令历史：

```python
from pathlib import Path
from urllib.request import Request, urlopen

key = Path('deploy/secrets/api-token').read_text(encoding='utf-8').strip()
request = Request('http://127.0.0.1:18217/v1/models',
                  headers={'Authorization': 'Bearer ' + key})
with urlopen(request, timeout=15) as response:
    print(response.read().decode('utf-8'))
```

## 聊天能用但没有召回记忆

先确认聊天真的经过 Serein 的 `/v1` 网关，或宿主已经实现 Hook 注入。只连接 MCP 并不自动完成每轮召回。

再检查自动记忆开关、Embedding／Reranker 选择、语义路由和索引准备。菜单 12 与菜单 4 的本地检索校验不调用模型；报告未就绪时，先处理具体阶段，不要直接调低召回阈值。

在网页召回观察中区分候选、准入、冷却和实际交付。没有匹配内容可以不召回；重排失败也不会把未经评分的候选直接注入。看到向量候选不代表模型已收到记忆。

同一窗口固定 `X-Serein-Window-ID`，新窗口换值。所有客户端都不填时会共用 `main` 的冷却与提醒轮次。编辑正文、换 Embedding 后按页面提示补齐索引；不要像旧 Markdown 桶那样在 Obsidian 或同步软件中直接改 Serein 主库。

## 更新被拒绝或没有出现菜单 12

菜单 1 更新已有实例，成功后退出旧菜单，重新输入 `se`。仍没有新选项时，先确认当前目录正确，再看 `deploy/update-state.json` 记录的安装提交；不要只看解压文件夹的名字。

非常旧的脚本需要按 [一次性引导说明](interactive-install.md#自动拉取上游更新)接入 Git 更新。更新被手工源码改动阻挡时，查看提示和 `git status --short`（仅 Git 安装目录适用），保存自己的改动后处理冲突；不要照搬旧版硬重置命令。

源码备份为 `deploy/backups/source-*.zip`；选择数据备份后还会有完整实例 tar.gz。构建失败保留备份和停止状态，修复后重试。需要回退时先停服、保留现状、核对旧源码与数据备份，没有自动回退按钮。菜单 9 清理的是受识别的旧数据升级备份，不自动删除所有手动备份或源码 ZIP。

## 迁移失败或历史数据没导入

先确认来源含需要的数据。只有 `buckets` 时缺梦境／暗房是来源不完整，不是需要新建 `diary.db`。菜单 10 可从完整旧库补漏，菜单 8 补旧边，菜单 11 补 cues；不要为此重导整个库。

打标失败先看网页具体错误，修复模型连接后重试 1 条，验证通过再重试剩余失败项。已有成功进度保留；格式校验失败、超时也可能计费。失败后换成不同备份或重建身份配置，可能破坏原批次续跑条件。

Serein 正文主库是 SQLite，旧 Ombre 的 Markdown 桶与 Syncthing 加密接收排错只适用于旧来源。不要把运行中的 Serein 主库放进双向同步，也不要对它执行旧版 `backfill_embeddings.py` 或桶去重删除脚本。

## 提供这些信息再求助

复制并填写下面模板即可，不需要上传整份数据库：

```text
系统和部署方式：例如 Windows Docker／Ubuntu Docker／Termux 直跑
首次安装还是更新：
出错步骤或菜单编号：
具体操作：
期望结果与实际结果：
菜单 12 中的 FAIL／WARN：
本机 /ready 的状态码：
网页能否登录：
聊天能否回复：
已尝试的处理：
相关日志中的错误代码或一句摘要（已遮掉私人内容）：
```

可以附错误截图，遮掉密钥、账号、真实地址和私人内容。不要分享 `connection-guide.txt`、`api-token`、数据库、完整日志或备份包。菜单 12 只显示固定错误类别；菜单 4 的原始日志可能含私人信息，发布前需要自己检查。
