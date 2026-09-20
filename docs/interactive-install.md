# 交互安装：Linux、Windows 与安卓 Termux

将发行包解压到持久目录，不要放在 `/tmp` 等重启可能清理的位置。
小内存设备请在运行安装脚本前，先用旧版的管理方式停止同机旧服务，为镜像构建留出资源。
从备份迁移不会自动停止旧服务；目录输入的停服检查发生在构建之后，不能代替这一步准备。
Linux / Termux / Git Bash 执行：

```sh
bash scripts/one_click.sh
```

Windows PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\one_click.ps1
```

也可以直接执行 `python3 scripts/manage.py`（Windows 用 `python`）。菜单沿用旧版分层引导的方式，
保留本版 0–3 编号，增加 4 服务与日志；`q` 退出，子菜单用 `r` 返回；选项输错可重填。
普通确认支持 `y/yes` 和 `n/no`，回车默认取消；操作后按 Enter 返回菜单。
部署会显示当前步骤，结束时给出前端与聊天客户端的使用指引。
“设置 → 外观”的两个头像与封面背景图在选图后自动保存，状态显示“已保存”才完成。第二次上传替换同一位置的旧图，不影响另两个位置；失败保留原图。图片随实例数据库备份，不依赖浏览器缓存，HTTP 与 HTTPS 均可使用。原图最多 20 MiB，较大的静态图自动缩小压缩后保存；GIF 动图最多 2 MiB。早期临时预览没有保存，升级后需重新选图。
管理菜单需要宿主机 Python 3.9+；直跑需要 Python 3.11+。Docker 路线的服务运行时由镜像提供。
页面账号可以先设置，执行部署等操作时才选择并检查运行环境。

## 自动拉取上游更新

进入当前实例的安装目录运行 `se`，选择 **1 · 拉取上游代码并重建**。Bash、PowerShell 和 Python 入口共用这个菜单，支持 Docker 与 Python＋Node 直跑。首次安装时，菜单 1 提供全新安装与旧库迁移引导；已有实例直接进入上游更新，不再单列本地源码重部署入口。

菜单 1 直接跟踪 `Yinglianchun/Serein` 的 `main` 分支，不需要发布 Release、更新 rc 版本号或准备发行 ZIP。脚本显示已安装提交和上游提交，选择“先备份再更新”（默认）或“跳过数据备份直接更新”，也可以返回取消。宿主机需要安装 Git。

选择更新后，用 Git 浅拉取刚才确定的提交，准备好代码才停止当前实例、按选择备份、替换源码、重建并启动服务。即使拉取期间 main 又有新提交，这一轮也只安装刚才显示的提交；下一次菜单 1 再更新。相同提交已成功安装且源码未手工改动时，不重复构建；中断或失败可重试。Git 缺失、网络失败或源码不完整时不会停服。

只替换 `release-files.json` 清单中的源码，清理上一版清单中已移除的文件。`deploy/runtime`、`deploy/secrets`、`deploy/config.toml`、`deploy/.env`、`deploy/installation.json` 和用户自建文件保留。新文件与自建文件重名、目标是链接、Git 有未提交改动或上次更新后的源码被手工改过时停止，先处理本地修改再更新。

源码始终备份到 `deploy/backups/source-*.zip`；选择数据备份时，还会在服务停止后生成现有的完整实例备份。这里的 ZIP 只是本地恢复备份，不是上游发行包。源码替换遇到错误会尝试恢复旧文件；构建失败会保留停止状态和备份，启动失败会尝试停止新服务。修复错误后可重试菜单 1，或使用更新前源码和对应数据备份恢复。

更新成功后在 `deploy/update-state.json` 记录实际安装的 Git 提交并退出旧菜单，重新输入 `se` 使用新版菜单。Git 拉取在临时目录中完成，因此原安装目录可以是解压版，也可以保留旧 Git 历史；不需要重新克隆安装目录、不重做数据转换。安装目录已有的 Git 分支不移动；开发请使用独立工作区，不要把安装目录的旧历史重新推回上游。

**旧脚本首次接入：** rc65 及更早版本的菜单仍是下载发行包（或没有菜单 11）。在原来的 Serein 安装目录运行下面的一次性引导，读取 main 上的新更新脚本，用 Git 拉取代码并原地重建。以后直接使用菜单 1；无须新发一个 Release 来接入。

Linux / Termux（`curl` 失败时不会执行后续脚本）：

```sh
curl -fL https://raw.githubusercontent.com/Yinglianchun/Serein/main/scripts/upstream_update.py -o /tmp/serein-upstream-update.py && python3 /tmp/serein-upstream-update.py --root "$PWD"
```

Termux 若没有 `/tmp` 写权限，将上面的两个 `/tmp/` 都替换为 `$TMPDIR/`。Windows PowerShell：

```powershell
$updater = Join-Path $env:TEMP 'serein-upstream-update.py'
Invoke-WebRequest https://raw.githubusercontent.com/Yinglianchun/Serein/main/scripts/upstream_update.py -OutFile $updater -ErrorAction Stop
python $updater --root (Get-Location).Path
```

**Git 历史清理与现有数据：** 清理公开提交、标签不会触碰本机或服务器的数据库，也不会重建或删除正在运行的容器。更新到新版代码才需要重新构建容器，由上述脚本完成；原数据库、模型配置、Key 和向量继续沿用，不重新导入旧库。不要删除原安装目录或执行清空数据的命令来“配合”历史清理。

开发者若需要重新克隆，建议放在另一个开发目录，现有实例继续在原路径运行。不要直接在空的新克隆中重新部署来代替旧实例：它缺少 `deploy/runtime`、`secrets`、`config.toml`、`.env` 和 `installation.json`，会使用另一份状态。后者还保存 Compose 项目名；仅带数据库去新目录不等于保留完整实例。安装用户优先使用原位置引导升级，无须迁移这些文件。

## 升级后的检索校验

rc52 修复了早期升级把“有后台任务配置”误当成“必须启用 Live Policy”的问题。只有身份、Scene Linker 的 `scene-linker-background.yaml` 不需要补 `gateway` 配置。没有正式发布记录时，普通聊天直接使用已经准备好的 `model-indexes/.../routes.json`；不重新调用 embedding，不改正文数据库，也不凭向量顺序伪造新版 source/index。网页保存的 domain 选择继续生效。

已手工修好的正式发布物会继续优先使用。未配置发布目录时，默认在当前数据库同级的 `semantic_recall_routes/` 与 `domain_recall_policies/` 查找；显式 `publish_dir` 仍优先，相对路径以数据库目录为基准。只补目录、尚无 `active.json` 时仍使用已准备的路由；显式指定 `routes_path`／`index_path` 则按指定 seed 校验。普通聊天不要求生成初始发布物。

若已有 `active.json` 却丢失 source/index、source 哈希不匹配或向量配置不兼容，会报告未就绪，保留原发布文件，不静默换成默认策略。正式发布物和准备阶段的 `routes.json` 是两种格式，不能互相改名代替。

一键更新完成启动后，以及菜单 **4 → 2「查看状态」**，会使用已安装的运行时做只读检索校验：实际可加载的 route source、source/index 一致性、embedding profile／维度、全部路由和 boundary 向量、domain policy。有效的 prepared 路由没有发布目录／active.json 属于正常状态。接口 `GET /v1/settings` 的 `memory_status` 同样提供就绪状态、来源和失败阶段；`memory_ready` 保留布尔值供网页使用。检查不会调用 embedding、reranker 或聊天模型，也不会自动修复或发布数据。它不测试上游可用性，HTTP 429 等上游问题需另行排查。

## 旧备份清理

更新已有实例时会先询问是否备份，回车默认“先备份再更新”，也可选“跳过备份直接更新”或返回取消。选择备份时，服务停止后会在当前实例 `deploy/backups/` 保存完整备份；选择跳过则不生成本次快照，已有备份仍保留。此选项适用于 Docker 和 Python＋Node 直跑更新；全新安装没有旧数据时不询问，旧库迁移前的备份仍保留。

主菜单 **9 · 旧备份清理** 默认保留最近 **3 份**，可调整保留数量，但至少保留最新 **1 份**。

菜单先列出待删文件和可释放空间，确认后才删除。只清理当前实例由一键脚本生成的完整备份；未完成的 `.pending`、手动命名的备份、旧边补救目录和其他实例不在清理范围内。当前数据库、密钥和运行目录不删除。预览后文件变化会停止并要求重新预览。

清理不停止服务，也不依赖 Docker 或 Node 能否正常运行；备份数量未超过保留数时直接提示无须清理。升级本身不会自动删除旧备份。删除后不能恢复，确认前请保留仍需要回退的版本。

## 三条部署路线

| 路线 | 前置环境 | 运行方式 |
| --- | --- | --- |
| Linux / VPS | Python 3.9+、Docker Engine、Compose 插件 | 两个 Linux 容器 |
| Windows Docker Desktop | Python 3.9+、已启动的 Docker Desktop，使用 Linux containers | 同一套 Compose，PowerShell 原生菜单，无需 Git Bash |
| 安卓 Termux / Windows / Linux 直跑 | Python 3.11+、Node.js 22.12+、npm | 独立 Python 虚拟环境 + Node 网关，后台运行 |

Windows Docker 环境设置见 [Docker 官方安装说明](https://docs.docker.com/desktop/setup/install/windows-install/)。
Docker 未启动、使用 Windows containers 或依赖版本不合适时，菜单会报错并允许修复后重试。
选择的方式保存在 `deploy/installation.json`；重新部署沿用原方式，不把容器路径直接改成直跑路径。
新 Docker 安装按目录生成独立 Compose 项目名，已有安装继续使用原项目名，避免两份发行目录互相重启服务。
已有旧版 `deploy/config.toml` 的安装沿用 Docker。需要另一种部署方式时使用新的发行目录，另选端口；不要手改安装方式后共用数据库。

Termux 先准备依赖：

```sh
pkg update
pkg install python nodejs-lts git clang make rust pkg-config
```

发行目录放在 Termux 的 HOME 内，避免共享存储的执行权限限制。进入发行目录后执行 Bash 入口，选择第 3 条路线。
直跑安装会创建 `deploy/venv`，安装核心依赖，执行 `npm ci` 与 `npm run build`，再启动 Python 核心和带鉴权的 Node 网关。
Termux 上部分 Python 依赖可能需要本地编译；安装失败时保留错误输出，解决依赖后从相同目录重试。
不自动替换系统 Python、升级系统 pip 或修改手机系统设置。

手机运行期间需允许 Termux 后台运行；可使用 `termux-wake-lock`，结束后用 `termux-wake-unlock` 释放。
Android 仍可能回收子进程，见 [Termux 官方执行环境说明](https://github.com/termux/termux-packages/wiki/Termux-execution-environment)。
脚本后台运行不等于开机自启，手机／电脑重启后从菜单 4 启动服务。

0 设置前端用户名／密码；1 部署（旧库迁移或全新安装／重新部署）；2 分别重启
gateway 或记忆库；3 补齐向量、重建当前模型向量或清理孤立的派生向量。
4 启动／停止两个服务、查看状态、查看最近日志。Docker 实例的 7 可授权网页迁移读取一个旧库目录，作为只读挂载；直跑实例直接填写本机可读路径。
8 补救早期未正确转换的旧边；9 清理旧升级备份；10 补漏早期迁移遗漏的梦境与暗房历史。已迁移的用户升级后按需选择 10，并提供含 `state` 的完整旧备份。
既有数据和密钥不会因重新部署被删除。实例文件位于 `deploy/runtime` 与
`deploy/secrets`，不进发行包。密码只保存带随机盐的 scrypt 哈希。

Docker 两个 Compose 服务为 `gateway`、`memory`；直跑保持相同的服务职责。
安装脚本依次构建 memory、gateway，降低小内存 VPS 冷安装时的并行资源占用。
Python 依赖先按 `pyproject.toml` 单独安装，之后才复制并安装 Serein 源码。只修改源码时复用依赖层；首次构建、依赖配置或基础镜像变化、Docker 构建缓存被清理时才重新安装依赖。pip 下载缓存也保留在 BuildKit 中，依赖层需要重建时可以复用已下载的包。
网关提供需要页面鉴权的前端与代理；
`/v1/models`、`/v1/chat/completions` 与远程 `/serein/mcp` 都由 Gateway Key 保护，与页面密码分开。MCP 传输选择 Streamable HTTP，无需额外启动进程。支持 OAuth 的客户端在 `https://你的域名/serein/mcp` 选择 OAuth，随后在 Serein 授权页手动输入 Gateway Key 确认；不支持 OAuth、但可自定义请求头的客户端继续使用 `Authorization: Bearer <Gateway Key>`。OAuth 授权要求 HTTPS 域名（同机 localhost 例外）；公网 IP 的 HTTP 入口只能使用静态 Key。已有反向代理须转发全部路径，不能只放行 `/serein/mcp`，因为 OAuth 还会访问根路径下的 `/.well-known/oauth-*`、`/register`、`/authorize`、`/token`。旧 `/mcp` 和 `/mcp/` 保留兼容。交互菜单配置 HTTPS 时会写入 `SEREIN_PUBLIC_ORIGIN`；手动 Compose/直跑部署也必须把它设为无路径的 HTTPS origin（例如 `https://memory.example.com`）。网关不信任来访请求自行声明的 `X-Forwarded-Proto`。

以网页入口 `https://你的域名` 为例，在客户端填写：

| 用途 | 地址 / 字段 | Key 怎么填 |
| --- | --- | --- |
| 聊天 API（OpenAI 兼容） | Base URL：`https://你的域名/v1` | API Key 输入框填完整 Gateway Key，不加 `Bearer` 前缀 |
| 模型列表 / 手动聊天请求 | `GET https://你的域名/v1/models` / `POST https://你的域名/v1/chat/completions` | 请求头 `Authorization: Bearer <Gateway Key>` |
| MCP（Streamable HTTP + OAuth） | 服务器 URL：`https://你的域名/serein/mcp` | 身份验证选 OAuth；授权页输入 Gateway Key 并确认 |
| MCP（Streamable HTTP + 静态 Key） | 同一服务器 URL | 请求头 `Authorization: Bearer <Gateway Key>`；专门的 Bearer Token 输入框只填 Key |

上表静态方式中的 `<Gateway Key>` 要连同尖括号一起替换成实际 Key，`Bearer` 后保留一个空格。OAuth 时不要把 Key 写进 URL 或客户端配置，只在域名相同的 Serein 授权页输入。直接访问 IP 时可将地址换成 `http://公网IP:网关端口`，但该方式不能完成 OAuth。网页登录密码和模型厂商 API Key 都不代替 Gateway Key。公网明文 HTTP 的传输风险见下文 [HTTP 兼容与访问风险](#http-兼容与访问风险)。

升级已有安装后，需要重新部署网关和记忆服务才能使用新 `/serein/mcp` 路由；仅替换管理脚本不会给旧服务增加路由。旧 `/mcp` 地址继续可用。

聊天通过此网关、且设置中开启“开窗续接”后，可直接发送 `/resume 我们接着聊昨天那本书吧`。网关加载选中的接续内容并保留指令后面的话，不经过客户端工具返回通道。它不会替直连其他模型平台的聊天识别指令；仅连接 Serein MCP 不会处理该指令；需要聊天经过此网关。
核心不直接映射公网端口。访问地址与端口在安装时选择；公网访问推荐配置 HTTPS 或使用加密隧道，HTTP 入口仍保留兼容。

同机访问默认监听 `127.0.0.1`；手机连电脑或其他设备访问时选择 `0.0.0.0`，填写服务器的局域网 IP／公网域名。
手机与电脑在同一 Wi-Fi 时，手机不能用 `127.0.0.1` 指向电脑；还需要允许网关端口通过电脑防火墙。
安装结束显示页面、聊天 Base URL、MCP 地址与自动生成的 Gateway Key，提示复制保存；同时写入仅当前用户可读的 `deploy/connection-guide.txt`。文件包含 API 密钥，不要公开。主菜单 6 可更换 Key，聊天和 MCP 客户端都需要同步更新。
查看现有 Key 可读取该连接说明，或读取 `deploy/secrets/api-token` 的完整内容，无需使用菜单 6。网页 **使用说明 → 接入** 提供公网 IP 和域名的连接地址模板及字段填写方法，但不会展示实际密钥。

手动 Docker 部署若没有 `deploy/.env`，更换 Key 并成功重启后仍会显示新 Key。若已保存公网入口，连接说明沿用该入口；否则提示沿用原来的访问地址，不猜测端口或创建 `.env`。连接说明文件无法保存时也会先显示 Key，再单独提示保存失败。重启失败仍恢复旧 Key。
在客户端添加请求头 `X-Serein-Window-ID`，值为当前会话 ID：同一会话保持不变，新窗口换值；支持会话 ID 变量的客户端可使用变量。不填时回退到 `main`，仍可聊天及使用记忆，但共用提醒轮次和召回冷却，不能识别新窗口。全局固定值也不能自动分窗；开窗续接请在经过此网关的聊天中发送 `/resume`。
直跑另外选择记忆核心、页面内部服务的两个端口；只绑定 loopback，不需要向外放行。

直跑日志在 `deploy/runtime/logs/{memory,gateway}.log`。管理器使用实例内的独占锁与带随机凭据的本机控制端口，
只终止自己持有的服务子进程，不按磁盘中的旧 PID 杀进程。端口占用、进程提前退出或就绪失败会报错并给出日志位置。
关闭菜单保留服务运行；重新部署直跑时会先停止本实例服务，再更新依赖与构建，数据与密钥保留。

## 快捷命令 se

首次运行 `scripts/one_click.sh` 或 `scripts/one_click.ps1` 会自动注册 `se`。之后先进入要管理的 Serein 发行目录，再输入：

```sh
se
```

命令读取当前目录，不绑定第一次安装的位置；多实例分别进入各自目录即可。错误目录只显示提示，不会寻找或启动其他实例。更新旧版本后，再运行一次启动脚本即可注册。也可以单独执行 `python scripts/install_shortcut.py`。

Linux root 安装到 `/usr/local/bin/se`，普通用户使用 `~/.local/bin/se`；必要时为用户 shell 配置追加 PATH。Termux 使用自己的 `$PREFIX/bin/se`。Windows 使用 `%LOCALAPPDATA%\Serein\bin\se.cmd` 并保存到用户 PATH；新开终端后生效。注册重复运行不追加相同配置，已有其他同名命令不覆盖；无法注册时仍可使用原启动脚本。

## 配置局域网／公网入口

部署完成后选择主菜单 **5 · 访问入口**，已有安装也可以直接使用，不需要重新构建或迁移。

- **仅本机**：网关监听 `127.0.0.1`。
- **IP 和端口**：输入其他设备能访问的 IP／域名，脚本将网关改为 `0.0.0.0` 监听，重新创建或重启本实例网关，并显示页面及聊天 API 地址。按实际访问范围放行服务器防火墙、云安全组的网关端口，尽量限制来源。该入口使用 HTTP，公网 IP 方式仍兼容；放行前请阅读下方 [HTTP 兼容与访问风险](#http-兼容与访问风险)。
- **HTTPS 域名**：Linux root 环境中，使用已安装的 nginx 和已有证书。域名须先解析到本机；新站点使用 `/etc/letsencrypt/live/<域名>/` 证书。脚本可接入现有 HTTPS 站点的 404／410 首页，或创建独立 nginx 配置；已有其他首页内容会拒绝覆盖。原站点其他路径保持原样，需自行确认它们不与应用路径冲突。

HTTPS 方式保留网关 loopback 监听，nginx 转发页面和 API，保留 Host、鉴权头及流式响应。修改前备份 nginx 文件到本实例 `deploy/runtime/nginx-backups`；检查或重载失败会恢复之前配置。已配置 HTTPS 的实例如要改回 IP／仅本机，应先移除对应 nginx 入口，避免仍可通过代理访问。

切换入口保留数据库、页面密码和 API token；新的页面地址和聊天 Base URL 写入 `deploy/connection-guide.txt`。脚本不会修改云安全组、DNS 或自动申请证书。

### HTTP 兼容与访问风险

通过 HTTP 地址（包括局域网 IP 和公网 IP 加端口）访问网页时，模型页也支持添加上游、手动添加模型和导入同名模型。旧版若点击“添加上游”无反应，升级并刷新页面后重试；无需为使用这个按钮先配置 HTTPS。以下只说明部署风险，不禁用 HTTP、不增加协议检测或强制跳转，也不改变聊天 API 与静态 Key MCP 的现有接入；OAuth 的 HTTPS 要求保持不变。

**HTTP 不提供传输加密。** Basic 登录凭据采用可逆的 Base64 编码，登录密码、请求中的 Key 与记忆内容可能被链路上的第三方读取，内容也可能被篡改；强密码和服务器保存的密码哈希不能解决传输泄露。公网明文 HTTP 不建议用于长期传输私人记忆，推荐配置 HTTPS 或通过加密隧道访问。局域网 HTTP 也不会自动获得加密，仍需评估网络是否可信。参见 [MDN：Basic 认证的安全性](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Authentication#security_of_basic_authentication)。

**监听地址与公网可达性要分别判断。** `0.0.0.0` 表示监听所有 IPv4 接口，本身不等于已暴露到公网；实际可达性还取决于公网路由、端口映射／Docker 端口发布、云安全组和防火墙规则。直接有公网地址的服务器可能无需路由器端口映射就可被访问。反向代理或隧道也能把本机监听的服务发布出去，不能只看监听地址判断谁能访问。Docker 部署请核对实际端口发布与访问规则，见 [Docker：端口发布与映射](https://docs.docker.com/engine/network/port-publishing/)。

**求助时不要公开真实实例地址、账号密码或 Gateway Key。** 截图与日志请遮挡这些信息，地址示例可改成 `http://示例IP:端口`，不要直接转发含密钥的 `deploy/connection-guide.txt`。地址不等于密码，但已开放的公网入口即使没有被分享，也可能被扫描或反复尝试登录；不公开地址不能代替鉴权和访问限制。HTTPS 保护传输，不能代替登录与访问控制；使用加密隧道时也要确认访问权限，避免把入口无意间公开。

## 旧库迁移

支持旧库根目录、buckets 目录、tar/tar.gz 备份。只扫描 buckets 下认可的
dynamic、permanent、archive、archived、feel、whisper 目录；不把删除备份、
迁移预览、日印象当作记忆。备份解压在新实例目录，拒绝越界路径和符号链接。
扫描错误、重复 ID 须先处理。删除指定分节后变空的记录跳过并列入报告，原文件保留。

输入旧库路径后填写用户、AI 名字及可选旧称，用于新名字配置和 cues 禁名校验。
导入前查看并增删改七个默认主域。只从旧配置导入模型连接；模型密钥不打印。
未能导入的连接可在向导补配。归档状态保留，其他旧标签／评分不进入新 metadata。
完整源文件仍在原库；新库保留来源标识和内容哈希以供核查。

模型导入也识别备份内 `.env` 的旧 `OMBRE_API_KEY`、`OMBRE_DEHYDRATION_*`、
`OMBRE_EMBEDDING_*`、`OMBRE_RERANKER_*`、`OMBRE_PERSONA_*` 覆盖值和上游
`api_key_envs` 列表（使用第一个可解析的密钥）。不读取安装主机上的同名变量。
只有运行配置、没有原 `.env` 的备份可能缺少密钥，需要在向导中补填。

清理规则：reflection、affect_anchor 及和弦情绪分节连同其内容删除，其他三级
标题删除而内容保留。代码块内的 # 不作为分节识别。feel/whisper 转日记。
网页和一键脚本均可选择“打标时生成召回线索 cues”，默认开启，开启时 cues、主域和实体由同一次打标调用生成；cues 不允许用户和 AI 的名字／别名。模型仍返回含这些名字的 cue 时，程序直接丢弃该条并保留其他合格 cues，不为禁名 cue 再次调用模型。
关闭后只提取主域和实体，不要求或校验模型返回的 cues，也不改写已有 cues。正文与必要的向量准备仍会进行，没有 cues 仍可按正文检索；关闭此项并不免除其他模型费用。
校验失败后停止自动重试。暂停或失败后可调整 cues 开关并续跑，只影响尚未成功打标的条目，已完成条目不重复调用模型。命令行 options JSON 可设置 `"generate_cues": false`，未填写的旧配置保持开启。

已经迁入当前实例、但因早期版本或中断而缺少 cues 时，可在一键维护菜单选择“旧 Scene 补 cues”。它先预览数量，确认后自动备份，只处理活动的 Ombre 旧导入 Scene；已有 cues、正文和已完成的主域／实体不会覆盖。失败后停止自动重试并保存进度，重跑会继续；空 cues 是有效结果并记为完成。索引同步由写入路径、后台任务和维护脚本负责，不再向聊天模型提供“索引重试”工具。
每条最多尝试三次，仍失败则保留进度并暂停，错误记录不包含上游原始响应或密钥。
旧边由程序转换，不调用模型：updates 转 continues 并交换两端；同名且方向明确的现有关系保留。其余不确定项只保留原记录与原因，不生成 related_to（相关）边。旧关联不标成正文证据；已取消、无效端点或不可用的边跳过。支持 state/memory_edges.jsonl 和所选旧库根目录／state 中 SQLite 的 scene_edges、scene_relations、memory_edges 表；扫描预览列出实际读取的文件和数量。
缺端点、日记端点、归档／不浮现／排除主域不生成新边；无关系是合法结果。

旧 embeddings 表没有保存嵌入原文与哈希时不能确认复用。只有保存原文、哈希、
模型、维度且与当前选择的旧模型配置一致的分段向量才尝试复用。
长文分段是可选项；在“设置 → 配置”开启后，首次导入时按设定门槛（默认超过 500 字）保存 passage 分段，复用已有分段并只补缺失向量。

迁移前预览并确认可能的模型调用，按当前挂载识别和确认停止旧容器；备份文件
输入不需要停服务。若旧服务由 systemd 等管理，按提示先自行停止写入。
Windows Docker Desktop 的目录迁移和直跑目录迁移也要求明确确认旧服务已停止写入；不根据 Desktop 转换后的挂载路径猜测旧进程。
跨平台迁移优先使用独立备份文件，可避免停止当前旧库服务。
先构建并创建两服务，转换期间保持新 memory 后台停止，完成后一起启动。
续跑也不会先启动写入后台，避免和迁移争用任务。
停止记录在 `deploy/runtime/stopped-legacy-services.json`。

每条处理写入独立 ledger，已有正文不覆盖，失败后同源重跑可继续。Linux / Termux / Windows 独占锁
防止同实例的迁移／向量维护双开。备份、ID 映射、阶段报告在 `runtime/migrations`。
中断不会删除原库，也不会自动删除旧容器；恢复旧服务前先停止新写入口。
续跑须保留原始快照、名字、主域与模型连接；可更新密钥。来源变化时拒绝继续，
防止将整库当成新批次重复导入。归档内容保留，不自动打标或建边。

## 梦境与暗房补漏

主要来源是 [Ombre-Brain 公开版](https://github.com/Yinglianchun/Ombre-Brain/tree/fc763dcc9332e45ed0f67066b801bf6e3d38d22f)：这个版本没有正式日记数据库，**不需要 diary.db**。暗房读取 `state/darkroom/entries.jsonl`，并保留 `state.json`、`releases.jsonl` 原文件；梦境读取 `state/dreams/dream_*.md` 与 `state/dreams/logs/events.jsonl`。配置过的 `state_dir` 或 `dream.data_dir` 只在所选完整备份内解析，不会读取备份外的旧绝对路径。只带 buckets 的备份不包含这两项，需要原库的 state 目录。

新的一键迁移和网页迁移会自动导入这些历史数据。早期已完成记忆迁移的用户：

1. 用新版发行文件完成重新部署，刷新网页；打开 `se` 的 **菜单 10「历史数据补漏」**。升级本身不会自动执行补漏。
2. 提供停止写入后、**包含 `state` 的完整旧库目录或 tar/tar.gz 备份**，先查看梦境与暗房数量再确认。只带 `buckets` 的备份不能补齐这两项；不需要另找或创建 `diary.db`。
3. 脚本短暂停止当前实例，备份数据库后只补历史数据，再恢复服务；不调用模型、不重新导入 Scene、不打标、不重建向量。
4. 查看报告与 ID 对应关系。备份和报告位于 `deploy/runtime/migrations/history-repair/`，完整原文快照也保存在数据库的导入记录中。它们含私人内容，请勿公开。

同一暗房按原 JSONL 顺序取最后一条为当前正文，前面各条保留为修订。保留原时间、旧 ID 对应关系及 `locked_until`；归档和撤回的房间不出现在当前暗房列表。删除备份目录不会被扫描，release 日志只保留，不重新执行释放动作。同一备份可安全重跑；导入后在新实例删除或编辑的内容不会被补漏恢复。预览后来源变化会停止执行。

如备份另含扩展版本的 `diary.db` 或 `window_shadows.sqlite`，可选导入器也会保留日记、评论、历史、暗房会话和窗影。数字 ID 冲突时分配新 ID，并同步评论、修订与会话归属；已有正式暗房记录优先于旧 JSONL，不重复恢复旧版本。

## 旧边转换补救

**这是对早期版本旧边未正确转换的补救。** 早期转换曾把多数旧类型统一保存成 `related_to`，未完成旧类型到五种关系的映射；当时没有读到边文件的记录也可能漏掉。正文已导入的用户不需要重新迁移整库。

1. 用新版发行文件重新部署当前实例（保留原数据和密钥），再在一键菜单选择 **8 · 旧边转换补救（早期版本未转换成功）**。
2. 输入原始旧库根目录、buckets 目录或含有 `state` 边文件的 tar/tar.gz 备份。留空可以恢复早期导入边中保存的原记录；如果当时根本没读到边文件，必须提供原始备份。
3. 查看扫描来源、旧边数量及问题提示，确认执行。脚本短暂停止当前实例，先生成 SQLite 一致性备份，再单独转换旧边，最后启动服务。
4. 查看输出中的 `report` 路径：`done` 为本次转换成功，`held` 为保留待核对，`skipped` 为端点或状态不允许转换，`repeated` 为已处理。报告逐条保留原记录及原因；数据库内也保存转换收据。报告和备份在 `deploy/runtime/migrations/edge-repair/`，含原始内容，请勿公开。

补救只使用程序规则，不调用关系模型、不重新导入或改写正文、不重新打标、不重建向量。旧 ID 与导入来源必须唯一匹配；补上边文件不会重新计算已经导入的 Scene ID。无法核对来源、正文已编辑、归档、排除域或已取消的边不自动恢复。匹配到的早期泛关联退出活动关系，保留历史与原记录。已有审核关系不覆盖，重复执行不重复建边，也不会恢复用户后来取消的边。异常中断后，可从同一入口继续。

关系体系保持 `continues / echoes / resolves / contrasts_with / evidenced_by` 五种。程序规则如下：

| 旧类型 | 处理 |
| --- | --- |
| `updates` | 转 `continues`，交换两端：旧分类器的“新 → 旧”改为“前情 → 后续”；不进一步猜测是否为 resolves |
| `echoes`、`contrasts_with` | 方向为空或 symmetric 时，保留同名对称关系 |
| `continues`、`resolves`、`evidenced_by` | 明确标记 directed 才保留同名方向；旧 JSONL evidenced_by 没有方向标记，且可能只是引用来源，保留待核对 |
| `triggers`、`causes`、`precedes`、`context_of`、`same_event`、`next_context`、`previous_context` | 仅旧类型无法确定具体经历延续，保留并报告 |
| `reflects_on`、`contradicts`、`supports`、`promises`、`blocks`、`belongs_to`、`emotional_echo`、`relates_to` | 不能仅靠改名证明回响、反差、履约或佐证，保留并报告 |
| 未知类型或冲突方向 | 保留并报告，不另造第六种边 |

程序转换只继承旧图关系，置信度记为 0，不伪造两端原文证据。正文版本检查继续有效；有关系只提供召回候选，不直接注入。

## Persona 与照顾备忘迁移

迁移向导额外读取所选备份内的 `state/persona_state.db`（也兼容 `buckets/persona_state.db`）和 `state/reminders.sqlite`。支持旧配置的 `persona.db_path`、`reminder_db_path` 和 `state_dir` 指向备份内的文件；绝不沿用指向备份外的旧部署路径或本机环境变量。标准位置与配置都没有文件时，在扫描中报告缺失；存在多个候选库时要求在备份配置中明确相对路径。

保留 Persona 的关系状态、会话心绪、内心文字、事件历史和评估记录。所选 `persona.profile_id` 映射到公开实例的 `default`，未配置且只有一个档案时自动选择；多个档案没有选择则停止。旧五维人格字段只作为历史兼容字段保留，不恢复已移除的运行机制。备忘保留 ID、标题、正文、活动／完成／归档状态、起止时间、下次提醒、重复规则、通道／会话约束、累计／每日次数和上限。

旧配置声明的默认会话映射到 `main`；其他会话 ID 原样保留。未声明默认会话时按 `main` 处理，扫描提示其他 ID 的延续方式。只从 `buckets/gateway_state.db` 汇总成功请求轮次，不搬整个网关聊天／诊断日志。缺少轮次记录时，以最近提醒轮次作为下限继续计数，并在报告注明，避免把提醒进度重置为零。

源表只读，目标同一事务写入状态与迁移收据。扫描与写入间源数据变化会阻断；目标已有 Persona 状态、会话轮次或冲突备忘 ID 时拒绝覆盖。相同来源重试不重复历史、不覆盖迁入后编辑。旧内存迁移 fingerprint 和 Scene ID 不变，已跑过的记忆迁移可以补跑此阶段。原始状态快照与映射保存在迁移目录的 `companion-source.json` 和阶段报告中，不进入发行包。Persona 模型连接按旧模型导入规则接入，功能开关仍保持新实例原设置，用户按需开启。

## 模型接入与运行限制

模型统一按上游管理。客户端 `/v1/models` 显示“上游名称/模型别名”，别名留空使用真实上游模型号；内部编号不用于显示，旧编号仍可路由到原模型。被分配为 Embedding 或 Reranker 的模型不列入客户端聊天列表和其他生成任务的选择项，仍保留在上游配置及向量／重排选择项中。更改分配后，保存并刷新客户端模型列表即可更新。相同上游内的显示名不能重复，可填写不同别名。

旧版单独模型以所属连接归入上游编辑器，保存时转为上游配置，保留任务绑定、密钥、缓存和向量参数。地址相同但密钥或连接选项不同的旧连接不会自动混用凭据；部署者可确认后手动合并。

“地下室 → 召回观察”默认展示 Gateway 当前请求的全部已加载记录。通过本站 `/v1/chat/completions` 的合法请求会保存当前原句、窗口 ID、自动召回开关、路由与准备材料、上游完成或失败状态；不保存完整提示词、密钥或记忆正文。工具续轮单独标记，不伪造新的用户原句。关闭召回的聊天也显示，未完成和失败不算已注入。“Hook 注入”页签展示 Hook 完成记录，按实际结果区分已注入、未命中与跳过；模型请求成功或宿主回执不证明客户端已显示回复。训练标注导出仍可从页头按钮操作，页面不再常驻显示导出统计面板。

迁入的旧记录继续保留并标为历史记录；升级前没有保存的原句无法补回。直接连接其他上游或只调用 MCP 的请求不会冒充本站聊天网关请求。自动召回开关、召回算法和五次成功响应冷却保持原设置；查看观察记录不会推进冷却或整理原话。

聊天网关的召回时间预算为 30 秒，适用于路由、候选检索与重排评分；预算耗尽时跳过召回，继续上游回复。观察记录保留候选数量、预算不足、重排认证失败／限流／超时等原因。重排服务失败仍不把未经评分的候选当作命中；请在设置中为所选重排模型配置有效密钥。

重新部署时，Docker 路线会先停止本实例的网关和记忆服务，再依次构建 memory、gateway，成功后启动并等待健康检查。构建期间入口暂停；停止或构建失败时不继续启动，可修复后重新部署。此步骤只停止当前 Compose 项目，同机其他旧实例仍需事先手动停止。小内存设备优先在其他机器构建前端再部署经过校验的产物。

Windows Docker Desktop 冷安装与安卓 Termux 的真机运行兼容性尚未确认。Termux 的依赖安装、双服务启动和后台保活取决于设备环境，不保证所有机型可用。

## 网页旧库迁移与备份

使用说明位于侧栏圆圈问号。设置页把“对话导入”和“旧库迁移”分成两个标签。旧库迁移可上传最多 64 MiB 的 tar/tar.gz，也可输入**运行 Serein 的机器**上的旧库根目录、buckets 目录或 tar 路径。直跑实例直接读取；Docker 实例先在安装菜单 7 授权旧库根目录，脚本将其只读挂载并短暂重建当前实例两个容器，随后在网页输入该目录或子路径。未授权的任意宿主机路径不可由容器读取。路径预览后可在浏览器一键导出所选来源的 ZIP（最多 5 万文件、4 GiB）；原库停止写入后再确认迁移。预览后确认名字、别名和模型费用才启动后台任务。关闭页面不停止任务；暂停在当前条目或阶段结束后生效，服务中断后可用相同配置续跑。原库若在预览后变化，需要重新预览；模型和主域沿用当前实例的配置，网页不自动覆盖上游密钥。

旧库里被标为“自我锚点”的正文直接丢弃，不恢复独立“自我”分类或作为梦境固定身份锚点；扫描预览会报告丢弃数量。持续更新的窗影承担流动的自我。心绪 Persona 状态与备忘是另两项可选功能，仍按下述规则迁入。

下载当前数据库备份会先做 SQLite 一致性快照并检查完整性，下载接口须登录。数据库包含模型密钥，需要妥善保存；它不包含图片、向量索引或安装账号文件，不是整站恢复包。

普通重部署停止服务后，会将已有 runtime、secrets 与部署配置保存到 deploy/backups。失败时保留停止状态及备份，不自动用旧数据库覆盖运行数据；回退请使用保留的旧发行目录，确认服务停止后恢复对应备份。Docker 网关健康检查同时验证记忆服务和前端预览服务，重启后也等待健康检查。


### 打标失败与旧边缺失

打标每条只尝试一次，失败后保留原文和成功进度，等待用户处理。先检查页面具体错误，再用“先重试 1 条”验证；“重试全部失败”会重新调用对应数量的模型请求，已成功的条目不重做。模型已生成但格式校验失败、请求超时等情况也可能计费，旧版笼统错误不能还原当时的具体原因。

所选备份没有识别到旧边时，先核对旧配置 `state_dir` 和旧 Docker 挂载。仅备份代码目录与 `buckets` 可能漏掉外置状态目录；完整备份中应保留 `state/memory_edges.jsonl` 或存有关系表的 SQLite 库。不应为凑齐数量创建新边。缺少旧边不阻止正文导入；网页按钮不可用时会提示缺少名字、未确认费用或扫描错误。

旧原文库 `state/raw_events.sqlite`（或备份内 `raw_events.db_path` 指定的库）会原样归档，供原文搜索和手动绑定。保留原文字、角色、时间、会话与来源；相同消息不重复插入，身份冲突保留现有数据并报告。仅归档，不调用模型，不自动重新生成 Event，不凭相似度恢复 Scene 绑定。

菜单 10 同时为已迁入的 Scene 补齐缺失日期：有效 `date` 优先，否则使用 `created` / `created_at`；已有日期与正文保持不变。没有可信旧日期则继续留空，不伪造迁移当天为记忆日期。此补漏不改变原批次指纹和 Scene ID。

### 补回记忆注脚／旧年轮

旧版 `comment_bucket` 和 Dashboard 写入的年轮存在各记忆 Markdown 顶部的 `comments` 数组中，不在日记评论表。rc46 起新迁移直接保留为记忆注脚；已迁移用户升级后选菜单 10，提供首次迁移的同一份完整旧备份即可补入。无需重新打标，不调用模型。

保留作者、内容及原时间（优先 `original_feel_created`，再取 `created`／`created_at`）。原 `kind`、来源和其他字段另存原始记录，注脚不变成正文或原文证据。原作者角色不明确时显示为“旧记录”，不擅自归给用户或 AI。已有注脚及删除记录优先，重复执行不会重复插入或复活删除项；无法匹配原 Scene、重复旧评论 ID 或格式异常会保留原记录并在报告中列出。
