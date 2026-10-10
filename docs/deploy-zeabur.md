# Serein 的 Zeabur 手动部署参考

更新日期：2026-10-10。

**本文根据当前 Serein 源码和 Zeabur 官方文档整理，尚未在 Zeabur 实际部署验证。** 它提供手动配置参考，不是经过验收的一键模板；平台构建、文件注入、网络、流式响应和重启后的数据保留仍需要部署者检查。常规安装见 [部署指南](deployment-guide.md)。

## 1. 当前结构

完整实例包含两个服务，均从同一份 Serein 代码、同一版本构建：

| 服务 | Dockerfile（相对仓库根目录） | 用途 | 本文监听端口 |
| --- | --- | --- | --- |
| memory | `Dockerfile` | 记忆、数据库、索引、聊天 API、MCP 和后台任务 | 8080 |
| gateway | `deploy/Gateway.Dockerfile` | 网页、网页登录鉴权和请求转发 | 8080 |

```text
浏览器 / 聊天客户端 / MCP 客户端
                ↓ HTTPS
             gateway
                ↓ 项目内 HTTP
              memory
                ↓
       /data 持久化数据卷
```

两个服务各自监听 8080，不会相互占用端口。网页还在 gateway 容器内启动一个仅监听 `127.0.0.1:4173` 的 Vite preview，不需要对外开放这个端口。

与旧 Ombre-Brain 不同，gateway 通过 HTTP 调用 memory，不直接读写主库，因此不需要两个服务共享数据库卷。旧版的 `server.py`、`gateway.py`、`config.yaml`、buckets 和 `/state` 配置不适用于本指南。

Zeabur 当前[不直接支持 Docker Compose YAML 部署](https://zeabur.com/docs/en-US/deploy/methods/dockerfile)。仓库的 `deploy/compose.yaml` 可以用来核对配置，但需要在平台中分别创建服务。

## 2. 先准备配置与账号文件

从仓库根目录的 `config.example.toml` 复制配置，修改这两行：

```toml
[storage]
database = "/data/serein.db"
index = "/data/recall-index.sqlite"
```

保留其余默认配置，包括 `[runtime]` 的 `writable = true`。初次部署不必手工填写 embedding、reranker 或后台模型；网页启动后再按 [模型设置](model-settings.md)、[召回说明](recall.md) 配置并准备索引。可选功能仍默认关闭。

需要准备三个文件：

| 容器内路径 | 内容 | 放在哪个服务 |
| --- | --- | --- |
| `/config/config.toml` | 修改后的配置 | memory |
| `/secrets/api-token` | 随机生成的 Gateway Key，纯文本 | memory 和 gateway，内容完全相同 |
| `/secrets/web-auth.json` | 网页用户名、盐和密码哈希 | gateway |

已有自己生成的账号文件可以沿用。全新安装可在本机保存并运行下面的 Python 脚本，交互输入账号和密码。它仅生成文件，不启动服务；生成目录应留在本机，不提交到 Git。

```python
import getpass
import hashlib
import json
import secrets
from pathlib import Path

directory = Path("deploy/secrets")
directory.mkdir(parents=True, exist_ok=True)
token_file = directory / "api-token"
auth_file = directory / "web-auth.json"
if token_file.exists() or auth_file.exists():
    raise SystemExit("账号文件已存在，请沿用原文件或另选空目录。")
username = input("网页用户名: ").strip()
if not username or ":" in username:
    raise SystemExit("用户名不能为空或包含冒号。")
password = getpass.getpass("网页密码（至少 10 个字符）: ")
if len(password) < 10 or password != getpass.getpass("再次输入密码: "):
    raise SystemExit("密码过短或两次输入不同。")
salt = secrets.token_bytes(16)
record = {
    "username": username,
    "salt": salt.hex(),
    "hash": hashlib.scrypt(
        password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32
    ).hex(),
}
token_file.write_text(secrets.token_urlsafe(36), encoding="utf-8")
auth_file.write_text(json.dumps(record), encoding="utf-8")
print("文件已保存到 deploy/secrets；api-token 的内容就是 Gateway Key。")
```

网页密码和 Gateway Key 是两种凭据。`web-auth.json` 必须使用上述格式，不能把明文密码写进 `hash`。

通过 Zeabur 的[配置文件管理](https://zeabur.com/docs/en-US/operations/data/config-file-management)将文件放到表中的绝对路径，确保每次新容器启动前文件都存在。不要只在某次容器终端里写入临时文件。配置文件和凭据不放入公开仓库或镜像；如平台允许设置权限，使用仅运行用户可读的权限。

## 3. 创建 memory 服务

在同一个 Zeabur 项目中创建 Git 服务。两个服务的构建根目录都使用**仓库根目录**：Dockerfile 的 `COPY` 路径依赖这一点。

memory 的环境变量：

```text
ZBPACK_DOCKERFILE_PATH=Dockerfile
PORT=8080
SEREIN_HTTP_TOKEN_FILE=/secrets/api-token
```

`ZBPACK_DOCKERFILE_PATH` 的用法见 [Dockerfile 部署说明](https://zeabur.com/docs/en-US/deploy/methods/dockerfile)。本文固定使用 8080，与平台 Git 服务的默认端口对齐；若实际分配端口不同，启动命令和后面的内部 URL 必须同步修改。

**覆盖默认启动命令为：**

```sh
python -m serein.launch --config /config/config.toml http --live --host 0.0.0.0 --port 8080
```

仓库根 Dockerfile 默认命令是 `python -m serein ... --port 8011`，而本地 Compose 另行设置了 `serein.launch` 入口。Zeabur 不执行 Compose，因此这里需要显式使用 `serein.launch`：它读取 token 文件、建立数据目录并初始化数据库。只设置 `PORT` 不会改变 Python 命令里的端口。

为 memory 挂载一个[持久化数据卷](https://zeabur.com/docs/en-US/operations/data/volumes)，路径为 `/data`。数据库、派生索引、图片和其他以数据库目录为根的运行文件都应保留在这个卷中；不要只保存 `serein.db` 一个文件。

配置 `/health` 为健康检查路径。memory 只供项目内访问，不需要绑定公网域名。启动后，从它的网络面板复制实际的 **Private hostname**；不要仅凭服务名称猜主机名。Zeabur 的[私有网络说明](https://zeabur.com/docs/en-US/deploy/networking/private-networking)介绍了查找方法。

## 4. 创建 gateway 服务

仍使用仓库根目录作为构建根目录。环境变量如下：

```text
ZBPACK_DOCKERFILE_PATH=deploy/Gateway.Dockerfile
PORT=8080
SEREIN_GATEWAY_PORT=8080
SEREIN_GATEWAY_BIND=0.0.0.0
SEREIN_MEMORY_URL=http://<memory 的实际 Private hostname>:8080
SEREIN_MEMORY_TOKEN_FILE=/secrets/api-token
SEREIN_WEB_AUTH_FILE=/secrets/web-auth.json
SEREIN_PUBLIC_ORIGIN=https://<gateway 的公网域名>
SEREIN_ROUTE_DRAFT_FILE=/app/.runtime/semantic-route-draft.json
```

将尖括号占位内容替换成实际值。`SEREIN_PUBLIC_ORIGIN` 只填写 HTTPS origin，例如 `https://example.zeabur.app`，不加 `/v1`、查询参数或其他路径；它用于远程 MCP OAuth 的公开地址识别。gateway 代码读取的是 `SEREIN_GATEWAY_PORT`，不能只设置平台的 `PORT`。端口规则见 [Zeabur 公网说明](https://zeabur.com/docs/en-US/deploy/networking/public-networking)。

沿用镜像默认启动命令：

```sh
node server/gateway.mjs
```

为 gateway 挂载独立卷到 `/app/.runtime`，保留网页编辑中的语义路由草稿。这个卷不与 memory 共享，也不要挂载到整个 `/app`，否则会遮住镜像中的程序文件。已发布的索引与路由由 memory 管理。

向 gateway 注入两个凭据文件，绑定 HTTPS 域名，设置 `/ready` 为健康检查路径。先启动 memory，再启动 gateway；Zeabur 不会自动沿用 Compose 的 `depends_on`。`/ready` 同时检查记忆服务和容器内网页，返回 200 只证明两者可连接，不代表模型或召回已经就绪。

## 5. 客户端连接与首次设置

| 用途 | 地址与凭据 |
| --- | --- |
| 网页 | `https://<gateway 域名>/`，使用网页用户名和密码 |
| OpenAI-compatible 聊天客户端 | Base URL 为 `https://<gateway 域名>/v1`，Key 为 `api-token` 内容 |
| MCP | `https://<gateway 域名>/serein/mcp`；Bearer 客户端使用同一 Key，OAuth 客户端按 [接入说明](interactive-install.md)授权 |
| Hook | `https://<gateway 域名>/api/hook/recall`，使用同一 Key，见 [Hook 接入](hook-integration.md) |

在网页的“设置 → 模型／配置”添加模型，选择 embedding 与 reranker 并准备索引，再开启所需功能。仅连接 MCP 的客户端需要主动调用记忆工具；自动召回需要聊天经过网关或宿主接入 Hook。

网页设置的叙事卷模型使用现有上游 API 路径。Gateway 镜像不包含 Codex CLI，不应直接开启需要本地 CLI 的 `SEREIN_WRITER_ENABLED`／`SEREIN_WRITER_COMMAND` 路线。

## 6. 数据迁移、更新与备份

旧 Ombre 记忆需要走 Serein 的旧库迁移入口，见 [迁移说明](interactive-install.md)。不能把 buckets 解压到 `/data` 就当作完成迁移，也不能直接把旧数据库更名为 `serein.db`。优先使用网页上传完整旧库备份；仅含 buckets 的包缺少暗房、梦境等历史状态。网页上传上限为 64 MiB，较大的备份需另行安排只读来源路径与授权，不套用本地菜单的主机挂载步骤。

Zeabur 部署由平台管理构建和重启，不使用本机 `se` 菜单管理线上服务。更新前保存完整 `/data`、gateway 草稿卷、配置与凭据；两个服务更新到相同版本。网页下载的数据库备份不包含图片、派生索引、草稿和部署凭据，不能代替整套备份。

SQLite 实例按单个 memory 副本运行，不对同一主库开启多副本写入。完整文件备份应先停止 gateway 的请求和 memory 服务，或使用能保证 SQLite 一致性的备份方式；不要在运行中只复制主库而漏掉 WAL。备份可能含个人记忆和模型密钥，不公开分享。

数据卷可以保留重启后的文件，但不代替备份；平台说明挂卷后的重启可能短暂中断服务。模型网络、构建内存、请求超时和 MCP 连接能力取决于实际环境，本文不作资源规格或所有功能可用的承诺。

## 7. 部署者自检（尚未执行）

以下是后续实际部署时的检查项目，不是本仓库已通过的 Zeabur 验收记录：

1. memory 的 `/health` 和 gateway 的 `/ready` 返回 200；公网只绑定 gateway。
2. 网页未登录时要求鉴权，正确账号可进入；错误 Gateway Key 无法调用聊天 API。
3. `/v1/models`、实际聊天流式输出、MCP 工具连接与 OAuth（如使用）可用。
4. 用测试数据写入、读取一条记忆；准备索引后验证相关召回与无关问题的表现。
5. 上传一张测试图片、保存路由草稿，分别重启和重新部署两个服务，核对记忆、图片、配置和草稿保留。
6. 用空库或测试实例验证备份恢复，再考虑迁移正式资料。

### 启动时报 HTTP bearer credential 未设置

```text
ValueError: Set the HTTP bearer credential before starting the service.
```

当前代码直接读取的环境变量名是 **`SEREIN_HTTP_TOKEN`**，值为自己生成的随机 Gateway Key，填写纯 token，不加 `Bearer ` 前缀。`SEREIN_HTTP_BEARER`、`SEREIN_BEARER_TOKEN` 和 `SEREIN_API_KEY` 都不是这个配置项，也不是模型厂商的 API Key。

根目录 Dockerfile 默认使用 `python -m serein`，不会自行读取 `SEREIN_HTTP_TOKEN_FILE`。使用本文推荐的文件配置时，需要改成第 3 节的 `python -m serein.launch ...` 启动命令；这个入口才会将文件内容读入 `SEREIN_HTTP_TOKEN`。也可直接设置 `SEREIN_HTTP_TOKEN`，但仍建议使用 `serein.launch` 完成初始化，并保持 gateway 的 token 文件内容相同。

### 服务运行但网页 502

仅用根目录 Dockerfile 会启动 memory 后端，不会提供完整网页。先确认是否另建了 gateway，以及公网域名是否绑定到 gateway。再核对平台转发端口与进程实际监听端口：根 Dockerfile 默认监听 8011，本文显式改为 8080；仅修改 `PORT` 环境变量不会改变该默认 Python 命令。

还需核对 gateway 的 `SEREIN_MEMORY_URL`、实际 Private hostname 和 memory 日志。502 本身不足以确定原因；排查时提供两个服务的 Dockerfile 路径、启动命令、端口、数据卷挂载路径及最近启动日志，环境变量仅提供名称，隐藏 token、密码、账号哈希和模型密钥。

`/ready` 返回 503 时，先检查 memory 启动、内部主机名、端口和凭据文件；网页显示鉴权尚未配置时，检查 `/secrets/web-auth.json` 的路径和格式。构建失败先核对 Dockerfile 选择和构建根目录。更多项目侧排错见 [排错指南](troubleshooting.md)。
