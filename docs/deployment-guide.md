# Serein 部署指南与常用命令

更新日期：2026-10-03。适用于 [Yinglianchun/Serein](https://github.com/Yinglianchun/Serein) 当前一键脚本，覆盖 Linux／VPS、Windows 和安卓 Termux。

这份指南从拿到代码、第一次安装写到客户端连接和日常维护。已经装好但遇到问题，请直接看 [Serein 排错指南](troubleshooting.md)。更详细的迁移规则与安装实现见 [交互安装说明](interactive-install.md)。

## 先看这六步

1. 准备运行环境，下载或克隆 Serein，进入安装目录。
2. Windows 运行 `scripts/one_click.ps1`；Linux／Termux 运行 `bash scripts/one_click.sh`。
3. 主菜单选 **1**，选部署环境，再选全新安装或从旧 Ombre 迁移。
4. 保存页面账号和脚本生成的 Gateway Key，等待安装与启动完成。
5. 打开网页，在 **设置 → 模型／配置** 接入模型，建立检索索引，再开启需要的功能。
6. 聊天客户端填写脚本给出的地址加 `/v1` 和 Gateway Key；MCP 使用同一入口加 `/serein/mcp`。

**安装成功不等于模型已经配置好；网页能打开不等于自动召回已经就绪。** 一键脚本处理安装和维护，你仍需要填写自己的模型服务与客户端配置。

## 1 选择部署方式

| 使用场景 | 部署方式 | 提前准备 |
| --- | --- | --- |
| Linux／VPS | Docker Compose | Python 3.9+、Docker Engine、Compose 插件；更新需要 Git |
| Windows 电脑 | Docker Desktop | Python 3.9+、Docker Desktop 已启动并使用 Linux containers；更新需要 Git |
| 无 Docker 的 Linux／Windows | Python + Node 直跑 | Python 3.11+、Node.js 22.12+、npm；更新需要 Git |
| 安卓 Termux | Python + Node 直跑 | Python、满足版本要求的 Node.js、npm 和编译工具 |

Docker 路线的 Python 和 Node 服务运行在镜像里，宿主机不需要额外安装 Node。直跑路线会建立独立 Python 虚拟环境并构建网页。

Linux 的 Docker 安装按实际发行版使用 [Docker Engine 官方说明](https://docs.docker.com/engine/install/)。Windows 使用 [Docker Desktop 官方说明](https://docs.docker.com/desktop/setup/install/windows-install/)；直跑需要的 Node 可从 [官方网站](https://nodejs.org/en/download)安装。

Windows Docker Desktop 冷安装与 Termux 真机长期运行兼容性尚未确认，不能保证所有设备都可用。Termux 长期运行还受后台限制和电池优化影响。

## 2 准备安装目录

代码放在不会被系统清理的目录，例如 Linux 的 `~/Serein`、VPS 的 `/opt/Serein` 或 Windows 的 `D:\Serein`。不要放在 `/tmp`。下面命令里的路径是示例，可以换成自己的。

已经下载并解压发行包的用户，直接进入解压后的目录，不必再次克隆。安装目录里应有 `release-files.json`、`scripts`、`src`、`web` 和 `deploy`。

### Linux 或 VPS

准备好 Git、Python 和所选运行环境后执行：

```sh
git clone https://github.com/Yinglianchun/Serein.git ~/Serein
cd ~/Serein
bash scripts/one_click.sh
```

VPS 上如使用 `/opt/Serein`，需要当前账号对该目录有写入权限。云服务器 SSH 连不上时，先看 [SSH 排错](troubleshooting.md#ssh-连接超时或被拒绝)。

### Windows PowerShell

先安装 Git、Python，以及 Docker Desktop 或直跑用的 Node。打开新 PowerShell，执行：

```powershell
git clone https://github.com/Yinglianchun/Serein.git D:\Serein
cd D:\Serein
powershell -ExecutionPolicy Bypass -File .\scripts\one_click.ps1
```

使用 PowerShell 入口即可，无需另找 Bash。若已解压发行目录，只运行最后两行，并把路径换成实际目录。

### 安卓 Termux

目录放在 Termux 自己的 HOME 内，避免把运行目录放在共享存储中：

```sh
pkg update
pkg install python nodejs-lts git clang make rust pkg-config
git clone https://github.com/Yinglianchun/Serein.git ~/Serein
cd ~/Serein
bash scripts/one_click.sh
```

环境选择 **3 · Python + Node 直跑**。安装前可用 `python --version`、`node --version`、`npm --version` 确认版本。允许 Termux 后台运行；需要时执行 `termux-wake-lock`，结束后用 `termux-wake-unlock`。手机重启后重新进入目录，用菜单 4 启动服务。

## 3 第一次运行一键脚本

主菜单选 **1 · 安装 Serein**，按实际环境选择：

- **1**：Linux／VPS Docker Compose。
- **2**：Windows Docker Desktop。
- **3**：Python + Node 直跑。

随后选择 **1 · 全新安装／重新部署**，或 **0 · 从旧 Ombre 记忆库迁移**。

全新安装时脚本会依次处理页面账号、Gateway Key、监听地址和端口、依赖与构建、服务启动和就绪检查。密码至少 10 个字符，输入时不回显。Gateway Key 自动生成，安装输出和 `deploy/connection-guide.txt` 中都有；请复制保存。

监听地址这样选：

| 访问方式 | 监听地址 | 客户端填写 |
| --- | --- | --- |
| 仅安装机器自己使用 | `127.0.0.1` | 本机地址 |
| 手机连电脑／其他设备直连 | `0.0.0.0` | 安装机器可访问的 IP 或域名 |
| nginx HTTPS 反代 | 网关通常保持 `127.0.0.1` | HTTPS 域名 |

`0.0.0.0` 是监听设置，不是客户端地址。手机里的 `127.0.0.1` 指手机自己，不是运行 Serein 的电脑。

首次构建需要下载镜像或依赖，可能耗时较长。小内存机器先停止同机旧服务，给构建留出资源。脚本只管理当前实例，不会替你在构建前停止所有旧服务。

### 从旧 Ombre 迁移

先保存完整旧库备份，再选择迁移。提供停止写入的旧库目录、`buckets` 目录或 tar／tar.gz 备份；填旧记忆中的用户名字、AI 名字和别名，查看预览并确认模型费用。

只带 `buckets` 的备份无法补齐梦境、暗房等历史数据；完整备份应保留 `state` 和旧配置。网页上传旧库 tar／tar.gz 上限 64 MiB，较大的来源可使用运行机器上的路径。Docker 网页路径导入先用菜单 7 授权只读来源。

迁移会打标并准备必要向量，可能调用模型；打标时是否生成 cues 可选。成功进度保存，中断后使用同一来源与身份配置继续。不要向新旧库同时写入，也不要因为某条失败重导整个库。

已迁移但漏了历史数据、旧边或 cues，使用菜单 10、8、11 单独补救。具体规则见 [旧库迁移与补漏说明](interactive-install.md)。

## 4 安装之后接入模型

1. 打开脚本显示的网页地址，用安装时设置的页面账号登录。
2. **设置 → 模型**：添加上游，填模型服务的 API Base URL、上游密钥、协议和模型。可拉取列表，也可手动填写真实模型号；完成后保存。
3. **设置 → 配置**：选择需要的打标、Embedding、Reranker 等模型。Embedding 负责向量，Reranker 负责比较候选记忆与当前问题；普通聊天模型不能随意当这两种模型使用。
4. 按页面要求准备语义路由，点击 **建立／补齐检索索引**，等准备完成。用菜单 4 的状态检查或菜单 12 查看检索配置是否就绪。
5. 在 **功能** 中开启自动记忆及需要的可选功能。模型费用由你使用的提供方收取；未用的可选功能无需全部开启。

客户端聊天模型从上游配置中的聊天模型列表选择，不需要在网页再指定唯一聊天模型。被选为 Embedding 或 Reranker 的模型不会出现在聊天列表中。详情见 [模型与客户端配置](model-settings.md)。

## 5 客户端地址与三种凭据

下表假设没有修改默认网关端口 **18217**。改过端口时，以脚本输出和 `deploy/connection-guide.txt` 为准。网页、聊天和 MCP 共用网关入口。

| 用途 | 同机示例 | 填写方式 |
| --- | --- | --- |
| 网页 | `http://127.0.0.1:18217` | 页面用户名和密码 |
| OpenAI 兼容聊天 | `http://127.0.0.1:18217/v1` | API Key 填完整 Gateway Key，不加 `Bearer` |
| MCP | `http://127.0.0.1:18217/serein/mcp` | Streamable HTTP；静态鉴权请求头为 `Authorization: Bearer <Gateway Key>` |
| HTTPS MCP OAuth | `https://memory.example.com/serein/mcp` | 选择 OAuth，Serein 授权页输入 Gateway Key |

**页面密码、Gateway Key、模型提供方 API Key 是三种不同凭据。** 模型提供方密钥填网页“模型”设置；不要把它填成聊天客户端访问 Serein 的 Key。

聊天客户端拉取模型列表后选择实际显示的“上游名／模型别名”。通常 Base URL 填到 `/v1`，具体接口地址由客户端追加；不要误把 `/serein/mcp` 填进聊天地址。不要照抄旧 Ombre 的 `/v1/messages` 接法；当前 Serein 网关的聊天入口是 OpenAI 兼容 `/v1/chat/completions`。

同一聊天会话保持稳定的 `X-Serein-Window-ID`，例如 `chat-001`；新窗口换成新值。不填时使用默认会话 `main`，共用提醒轮次和召回冷却，不能自动区分窗口。

聊天经过 Serein 网关才由网关自动带入记忆。只连接 MCP 时，客户端模型需要主动调用工具；两种接入并不等价。开启开窗续接后，经过网关的聊天可发送 `/resume`，也可用 MCP 的 `resume`；按需要选一种接续方式。

## 6 手机访问与 HTTPS 入口

### 手机连接同一网络的电脑

进入电脑上的安装目录，运行 `se`，选 **5 → 1 · 局域网／公网 IP 和端口**，填电脑的局域网 IP，例如 `192.168.1.23`。确认应用，允许网关端口通过电脑防火墙。

手机聊天 Base URL 示例为 `http://192.168.1.23:18217/v1`，页面为 `http://192.168.1.23:18217`。地址中的 IP、端口都换成实际值。电脑关机、休眠或服务停止后，手机会连不上。

### VPS 与域名

IP 直连使用菜单 5 的 IP 入口，并允许实际网关端口通过云安全组和系统防火墙。公网 HTTP 支持使用，但不加密凭据和记忆内容；长期使用请配置 HTTPS 或加密隧道。

菜单 **5 → 2 · HTTPS 域名** 是接入现有 nginx 和证书的功能：需要 Linux root、已安装 nginx、域名解析到本机和已有证书。它不负责买域名、签发证书或自动开放云安全组。入口有冲突或首页已经在用时可能拒绝覆盖，先按提示处理。

HTTPS 完成后，网页用 `https://memory.example.com`，聊天用该地址加 `/v1`，MCP 加 `/serein/mcp`。OAuth 使用 HTTPS 域名或本机 localhost；公网 IP 的 HTTP 入口使用支持静态请求头鉴权的 MCP 客户端。

## 7 日常维护菜单

首次运行启动脚本会尝试注册短命令 `se`。以后先进入对应 Serein 安装目录，再输入 `se`；快捷命令按当前目录选择实例。找不到 `se` 时重开终端，或继续使用原启动脚本。

| 菜单 | 用途 | 注意 |
| --- | --- | --- |
| 0 | 设置页面用户名／密码 | 与 Gateway Key 分开 |
| 1 | 首装；已有实例拉取 main 并重建 | 更新默认先备份 |
| 2 | 重启网关或记忆服务 | 会短暂中断对应服务 |
| 3 | 补齐／重建／清理派生向量 | 补齐和重建会调用嵌入模型 |
| 4 | 启动／停止／状态／最近日志 | 状态同时做本地检索校验 |
| 5 | 本机／IP／HTTPS 访问入口 | 保留数据和鉴权 |
| 6 | 更换 Gateway Key | 旧 Key 和已发放 OAuth 凭据失效，客户端需更新 |
| 7 | 网页旧库来源只读授权 | Docker 挂载用途 |
| 8 | 旧边转换补救 | 程序规则，不重导正文 |
| 9 | 清理旧升级备份 | 先预览确认，默认保留 3 份 |
| 10 | 历史数据补漏 | 提供完整旧库来源，不调用模型 |
| 11 | 旧 Scene 补 cues | 显式模型调用，可能计费 |
| 12 | 故障排查 | 只读，不调用模型 |

子菜单 `r` 返回，主菜单 `q` 退出。普通确认回车默认取消。退出菜单不会主动停止已启动的服务。

### 更新已有实例

```sh
cd ~/Serein
se
```

选择菜单 **1**，核对当前和上游提交，默认选择先备份再更新。脚本跟踪 GitHub `main`，无需等待新 Release。源码先准备好，之后停服、备份、替换发行清单中的文件、构建并启动；Git 拉取失败不会停服。

更新成功后旧菜单退出，重新输入 `se`。构建失败时服务可能保持停止，请先排错后重试；目前没有自动切回旧版本。安装目录源码有手工改动时可能拒绝更新，不要直接 `git reset --hard` 或覆盖整个目录。

很旧的安装菜单仍下载发行包或没有新选项时，按 [旧脚本首次接入](interactive-install.md#自动拉取上游更新)运行一次引导，再使用菜单 1。

## 8 数据在哪里与如何备份

| 路径 | 内容 |
| --- | --- |
| `deploy/runtime/serein.db` | 主数据库，包含记忆和实例设置等 |
| `deploy/runtime/` | 图片、派生索引、迁移报告、直跑日志等实例数据 |
| `deploy/secrets/api-token` | Gateway Key |
| `deploy/secrets/web-auth.json` | 页面账号与密码哈希 |
| `deploy/config.toml`、`.env`、`installation.json` | 基础配置、监听设置与部署记录 |
| `deploy/connection-guide.txt` | 含 Gateway Key 的连接说明 |
| `deploy/backups/` | 升级数据备份和源码备份 |

网页下载的数据库备份经过 SQLite 一致性检查，但不包含图片、派生索引或安装账号文件。完整实例升级备份还包括 runtime、secrets 和部署配置。**数据库也可能含模型密钥；这两类备份都不能公开分享。**

当前菜单没有独立的“一键备份”选项。正常升级时默认创建完整实例数据备份；单独备份可在菜单 4 停止全部服务后复制上述数据和配置，并保存对应源码版本，再启动服务。不要在服务写入时直接复制 SQLite 主库，也不要把运行中的整个 `deploy/runtime` 放进双向同步软件。

恢复前停止目标实例，保留现有数据副本，核对备份和匹配源码，再恢复数据和配置；不要把不同版本、不同实例的数据库和 secrets 混在一起。恢复不是把一份数据库丢进源码根目录。

## 9 最短命令卡

所有命令先在对应安装目录执行：

```sh
# 打开菜单；选 1 更新，4 服务与日志，12 排错
se

# Linux / Termux 没有 se 时
bash scripts/one_click.sh

# 独立只读排错；Docker 不可用时也能运行
python3 scripts/doctor.py

# 本机网关就绪检查；端口改过就替换 18217
curl --max-time 8 -i http://127.0.0.1:18217/ready
```

Windows 对应入口：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\one_click.ps1
python .\scripts\doctor.py
curl.exe --max-time 8 -i http://127.0.0.1:18217/ready
```

出了问题先运行排错，按提示查服务和入口。还需帮助时，按 [求助模板](troubleshooting.md#提供这些信息再求助)整理信息，无需贴整份配置或私人记忆。
