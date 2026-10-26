# Windows 本地部署与 Agent 交接

本项目统一名称为 `xianyu-intelligent-cs`，初始来源见 `SOURCE-REVISION.txt`，当前版本以 Git 提交为准。保留智能客服、材质知识库、商品资料、扫码账号连接和续期状态展示。这里部署全新的 Windows 数据环境，Mac 原环境和数据保持不变。

## 交付内容与不包含的内容

- 源码、Dockerfile、客服专用 Compose、测试、GPL 许可证和原有文档。
- 用户自行准备的本地 `deployment-assets/材质信息表.xlsx`，不随公开仓库分发。
- 用户自行准备的本地 `deployment-assets/customer-faqs.json`；真实店铺问答不随公开仓库分发。
- `scripts/windows-initialize.py`，首个本地管理员初始化和空知识库导入工具，仅使用 Python 标准库。
- 不含 Mac 的 `.local`、`.venv`、`node_modules`、Cookie、API Key、加密密钥、管理员密码、数据库、商品快照、聊天记录、Docker 数据卷和机器专用 DNS。
- 离线 ZIP 不含 `.git` 历史。项目仓库为 `ParseC-ctrl/xianyu-intelligent-cs`；Git 克隆不含 `deployment-assets/`，需从本地迁移包复制业务资料。不要用上游仓库替换源码。
- 共享项目目录是后续开发主目录；WSL 是运行副本，应从主目录同一版本部署，避免两份源码分别修改。`MANIFEST.sha256` 仅适用于离线 ZIP，Git 克隆不执行此文件校验。

## 1. 在 WSL2 内安装原生 Docker Engine

本机 Windows 较旧，采用 **WSL2 + Linux 内原生 Docker Engine + Compose 插件**，不依赖 Docker Desktop。以下命令以 Ubuntu 为例；其他发行版按对应官方安装页执行。Docker CLI、daemon、Compose 和项目命令都在同一个 WSL 发行版内运行，Windows PowerShell 不必安装 Docker CLI。

### 1.1 确认 WSL2 与发行版

PowerShell 中执行：

```powershell
winver
wsl --list --verbose
wsl --version
```

旧版 WSL 可能不支持 `--version`，记录该结果并继续检查；不能因此断言没有 WSL。必须确认目标发行版的 VERSION 为 **2**。已安装 WSL 不代表已使用 WSL2。若为 1，先备份该发行版并检查 Windows 构建与虚拟化支持，再执行 `wsl --set-version <实际发行版名> 2`；转换失败须根据错误处理，不能在 WSL1 强行部署。

进入目标发行版（`Ubuntu` 替换为实际名称）：

```powershell
wsl -d Ubuntu
```

在 WSL 中执行：

```bash
uname -r
uname -m
cat /etc/os-release
ps -p 1 -o comm=
command -v docker || true
command -v python3 || true
command -v unzip || true
```

优先使用 Docker 官方仍支持的 Ubuntu LTS，例如 22.04 / 24.04。不要把已经停止支持的发行版硬套到其他 Ubuntu 版本的软件源；需要新发行版时保留旧发行版及数据，另行安装受支持版本。旧 Windows 的实际兼容性以目标机器检查为准。

### 1.2 安装 Engine 和 Compose

先检查是否已有 Linux Docker Engine、Docker Desktop 注入的客户端、已有 context 和 `/var/run/docker.sock`。已有可用 Engine 则复用。若需要从其他 Docker 发行包迁移，先盘点容器和卷并备份，按官方“卸载冲突包”说明逐项处理；不要盲目卸载或删除 `/var/lib/docker`。

全新 Ubuntu 环境使用 Docker 官方 apt 源：

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl python3 unzip
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

检查是否已有 Docker 源；仅在没有配置时创建下列文件，已有配置则核对并复用，避免重复源或覆盖用户配置：

```bash
. /etc/os-release
sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Compose 插件是项目必需组件；只安装 `docker` 客户端不足以运行。无需通过 Windows Docker Desktop 安装。上面命令须按 [Docker 官方 Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/)核对适用发行版和冲突包。

### 1.3 启动 Docker，兼容没有 systemd 的旧 WSL

**PID 1 已为 systemd 时：**

```bash
sudo systemctl enable --now docker
sudo systemctl status docker --no-pager
```

**PID 1 不是 systemd 时：**优先检查 WSL 是否支持 systemd。Microsoft 要求 WSL 0.67.6 或更新版本；Windows 过旧可能无法升级，不能把启用 systemd 作为唯一可行路线。

支持时，保留 `/etc/wsl.conf` 原有配置，将 `systemd=true` 合并到已有或新增的 `[boot]` 节，不重复创建同名节。关闭该发行版的任务后，从 PowerShell 执行 `wsl --terminate Ubuntu` 并重新进入，再核对 PID 1，使用上述 systemctl 命令。不要无提示执行影响全部发行版的 `wsl --shutdown`。

无法启用 systemd 时，在 WSL 内手动启动：

```bash
sudo service docker start
sudo docker info
```

若发行包没有可用 init 脚本，先检查是否已有运行中的 daemon；确认没有后才可手动启动一个：

```bash
sudo -v
sudo sh -c 'nohup dockerd > /var/log/docker-wsl.log 2>&1 < /dev/null &'
sudo docker info
```

启动异步，未就绪时短暂等待后检查日志；不要反复启动多个 daemon。若仍失败，读取 `/var/log/docker-wsl.log`，检查 WSL2 内核、cgroup、存储及网络支持。不要以 `--privileged`、关闭防火墙或暴露 Docker TCP 端口绕过。systemd 与非 systemd 路线都必须实际完成 `docker info` 和容器检查，不能只根据安装成功认定可运行。

日常部署脚本默认以普通用户运行。需要使用本地 Docker socket 时，可将当前管理员用户加入 docker 组，再退出并重新进入 WSL：

```bash
sudo usermod -aG docker "$USER"
exit
```

docker 组等同于赋予该用户管理主机的能力，仅用于受信任的本机管理员。重新进入后验证：

```bash
docker context show
docker version
docker compose version
docker buildx version
docker info
docker run --rm hello-world
```

应连接当前 WSL 内的本地 Unix socket；若仍指向 `desktop-linux` 或旧 `DOCKER_HOST`，先记录配置，选用本地 default context，并在当前 shell 清除失效覆盖。不覆盖远程或其他项目的 context。`hello-world` 会访问 Docker 镜像源，不涉及闲鱼或模型接口。

### 1.4 启动和常驻边界

`enable docker` 只控制发行版启动后的 Docker 服务，不代表 Windows 开机后 WSL 必然启动；非 systemd 路线在 WSL 停止或机器重启后需要重新启动 daemon。Compose 的 `restart: unless-stopped` 只有在 Engine 运行时生效。Windows 睡眠、休眠或关闭 WSL 会中断客服。

首次先手动完成部署和重启恢复检查。需要 Windows 登录后自动启动时，再配置当前用户的任务计划程序调用 `wsl.exe -d <发行版> ...`，按所选服务管理方式启动 Docker 并运行客服启动命令，记录实际工作目录；任务不能包含密码或模型 Key。不要未经检查配置为 SYSTEM 用户，因为其 WSL 发行版可能与当前用户不同。用户明确停止的客服不能被自启动任务擅自恢复。

官方依据：[WSL systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd)、[WSL 网络与 localhost 转发](https://learn.microsoft.com/en-us/windows/wsl/networking)。本节命令尚未在用户 Windows 实机执行，安装及恢复结果由 Windows Agent 实测记录。

## 2. 将代码放到 WSL 的 Linux 文件系统

不要直接在 SMB 网络盘或 `/mnt/c/...` 中构建和运行。共享目录用于源码开发、版本管理和传输，运行副本位于 WSL Linux 文件系统。交付目录旁另有同名 ZIP，可先用 Windows 的 PowerShell 验证 ZIP SHA256：

```powershell
Get-FileHash -Algorithm SHA256 .\xianyu-intelligent-cs.zip
```

结果应与同目录 `.zip.sha256` 一致。也可用 `MANIFEST.sha256` 检查解压后的单个文件。

Windows Agent 从自己的工作目录确定 ZIP 的 Windows 绝对路径，不要硬编码 Mac 的 `/Volumes/work`。在 PowerShell 转为 WSL 路径：

```powershell
$BundlePath = (Resolve-Path .\xianyu-intelligent-cs.zip).Path
wsl -d Ubuntu -- wslpath -a $BundlePath
```

在 WSL 中使用上一条输出的实际路径：

```bash
mkdir -p ~/apps
# 将下面占位路径替换为 wslpath 的实际输出。
unzip -q '/mnt/c/实际路径/xianyu-intelligent-cs.zip' -d ~/apps
cd ~/apps/xianyu-intelligent-cs
sha256sum -c MANIFEST.sha256
```

如果目标目录已存在，先检查来源及已有配置；不要直接解压覆盖。可选择一个新的工作目录。已有同名 Docker Compose 项目/数据卷也必须先识别，不把旧卷当作全新部署。

## 3. 生成本机配置并启动

在 WSL 项目根目录执行：

```bash
python3 scripts/setup-customer.py
bash scripts/customer.sh start
bash scripts/customer.sh status
```

使用的是 `compose.customer.yml`，不是上游通用 `docker-compose.yml`。脚本会构建本机架构的 Linux 镜像，因此 Windows x86_64 会自行构建，不要加载 Mac ARM64 镜像。

首次生成 `config/customer.env`，包含新的随机加密主密钥。它必须和 Windows 数据卷一起备份，之后不要重新生成或用 Mac 文件覆盖。默认仅监听本机 `127.0.0.1:4173`。

访问地址：`http://127.0.0.1:4173/xianyu-intelligent-cs/`。若端口占用，先检查占用进程；可修改 `config/customer.env` 的 `SAAS_CUSTOMER_WEB_PORT` 后重新运行启动脚本，初始化工具同时传对应 `--origin`。

WSL 中检查页面：

```bash
python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:4173/xianyu-intelligent-cs/', timeout=15).status)"
```

Windows 浏览器也应能打开该地址。如果只有 WSL 内可访问，先确认 WSL 内页面正常、容器端口绑定为 127.0.0.1、WSL2 的 localhost 转发（`%UserProfile%\.wslconfig` 中 `[wsl2]` 的 `localhostForwarding=true`）、VPN/代理和防火墙；修改配置前保留原值，重启发行版前保存任务。旧系统的转发能力以实测为准，不要直接改为公网监听。

## 4. 首次管理员及知识库初始化

在 WSL 根目录执行：

```bash
python3 scripts/windows-initialize.py --seed-knowledge
```

工具会生成新的 `customer-admin` 密码，保存在 **WSL 运行副本**的 `.local/windows-admin.json`，不打印密码。没有固定的出厂密码，Mac 密码不适用于这个新环境。用户可在自己的 WSL 终端查看该本地文件完成登录，Agent 不将明文写入公共日志或交付报告。

工具仅在服务器允许首个管理员注册时注册；重复执行时复用本机凭据。已有知识库/FAQ 将保留，不会覆盖。已有管理员但本机凭据不匹配时会停止，请提供原管理员凭据，不要删卷或删库来解决。

该工具只调用本地管理员、材质知识库和 FAQ API，不登录闲鱼、不启动客服、不调用模型、不同步商品。

导入数量、冲突及空值以用户自己的数据为准。核对自备 Excel 的任意已知货号与对应资料，确认查询和字段映射准确；公开仓库不提供真实货号、材质记录或业务统计。

## 5. 模型连接

用户在页面配置自己的 DeepSeek Key。不要将 Key 写入文档、源码或 Git。沿用已配置且验证可用的模型，不由 Agent 擅自猜测最新模型名；支持图片识别的模型还需实际具备图片输入能力。连接信息加密保存在 Windows 本地数据卷中。

未提供 Key 时，Agent 可完成容器启动、管理员创建、知识库导入与本地规则预览，明确标注真实模型测试尚未完成。用户提供 Key 后，再使用“仅试答”测试自然回复，禁止为模型测试向真实买家发送消息。

## 6. 闲鱼账号与人工验收

部署完成不等于已连接闲鱼。由用户进入“账号连接”扫码，新环境不复用 Mac Cookie。默认不自动启动客服；扫码及账号相关调用只在用户进行真实接入验收时执行。

同一闲鱼账号不要同时在 Mac 和 Windows 上运行自动客服。若用户要测试同一账号，先在 Mac 暂停客服，再在 Windows 明确启动；Agent 不通过后台脚本替用户扫码或绕过验证。

商品描述使用完整货号；必要时用户手动执行一次受限批次同步。同步只要在卖/已发布商品，每批最多 20 件详情，保留静默合并、暂停和续传逻辑。不为部署验收自动全量同步，不开启模拟流量或额外保活请求。

账号页展示已有消息令牌续期成功时间、下次计划和失败原因。旧记录可能为空；登录失效需用户扫码，平台验证先到闲鱼 App 处理。页面轮询只读本地状态。

## 7. Windows Agent 的验收清单

1. 目标发行版为 WSL2，记录 systemd 或手动 daemon 路线；`docker info`、容器运行及 `docker compose` 可用，`bash scripts/customer.sh status` 显示客服容器运行并最终 healthy；记录 WSL 发行版和实际运行目录。
2. 实测发行版重新启动后的 Docker 与应用恢复方式；Windows 浏览器能访问工作台；创建/登录本地管理员成功，凭据保存在 WSL 私有文件。
3. 客服专用导航有效，自备材质表和店铺 FAQ 导入；已有资料不被覆盖。
4. 确认材质查询准确，本地规则试答明确标记未使用模型；有 Key 后再做仅试答的模型检查。
5. 账号页能显示续期字段；未登录/未启动时显示暂无记录或无计划，不虚构续期成功。
6. 未经用户实际扫码和启动，不验证真实账号收发；这些项目应列为“等待用户验收”。
7. 不增加闲鱼请求，不启用日常虚假访问、不启动上架/自动发货/订单操作。

本包的应用运行路径已在 Mac 的 Linux 容器验证；本次交付未在真实 Windows 机器执行安装与部署。Windows Agent 必须报告实际结果，不能把文档说明当成已通过证据。

## 8. 日常运维、备份与排障

```bash
bash scripts/customer.sh status
bash scripts/customer.sh logs
bash scripts/customer.sh stop
bash scripts/customer.sh start
```

日志可能包含业务信息，分享前脱敏。不要运行 `docker compose down -v`，不要使用 `docker volume prune` 清理这个项目。

数据在命名卷 `xianyu-intelligent-cs_customer-data`（实际名称用 Compose 检查），包括数据库、模型加密凭据、知识库和会话。有效备份必须同时保存数据卷、`config/customer.env` 与本地管理员凭据；备份放在受控位置，不提交 Git。只复制源码无法恢复运行数据。

- Docker 无法连接：在目标 WSL 内检查本地 daemon、Unix socket、docker 组权限、context 和 `DOCKER_HOST`。systemd 用 `systemctl status docker` / `journalctl -u docker`；手动模式检查启动结果及 `/var/log/docker-wsl.log`。
- `docker: command not found`：在 WSL 安装官方 docker-ce-cli；`docker compose` 不可用则检查 docker-compose-plugin。Windows PowerShell 不存在 docker 命令不代表 WSL 未安装。
- CRLF 导致 shell 报错：本包使用 LF；保留 `.gitattributes`，不要让编辑器改成 CRLF。
- 构建下载失败：检查 Windows/WSL/Docker 的正常联网和代理配置，不复制 Mac Colima 的 DNS 覆盖；不要关闭 TLS 校验或下载不明二进制。
- 权限失败：确保运行副本在 WSL Linux 文件系统，数据卷由 Compose 的 data-init 初始化；不要用 `chmod -R 777` 绕过。
- 模型 Key 解密失败：检查是否换过加密主密钥；保留原文件和卷，先排查，不清空数据。
- Cookie 失效：页面提示用户重新扫码，禁止用循环请求强行维持登录。

## 9. Mac 开发后更新 Windows

Mac 在共享开发主目录修改、检查并推送项目仓库；Windows WSL 的固定运行目录只部署版本，不同时修改应用源码。用户已反馈 Windows 首次部署成功，后续升级仍需检查实际提交和容器状态。

在 Windows 的目标 WSL 中，进入**已有运行目录**，确认 Docker Engine 已启动，并先备份数据卷、`config/customer.env` 和管理员凭据。按原有备份方式执行，保留恢复能力；不要重新初始化密钥、管理员或知识库。

```bash
cd ~/apps/xianyu-intelligent-cs  # 使用实际已有运行目录
# 输出应为空；有本地修改先保留并分析，不强制覆盖。
git status --short
git rev-parse HEAD             # 记录升级前版本
git pull --ff-only origin main
bash scripts/customer.sh start # 内含 compose up -d --build，构建并重建有变化的容器
bash scripts/customer.sh status
```

只执行 `docker restart` / `docker compose restart` 会继续使用旧镜像，不能完成源码更新。`start` 不删除命名卷；保持原 Compose 项目名、配置、数据卷和端口，不换到另一份目录误启第二个实例。脚本可能短暂中断客服连接，选择低峰更新，不在 Mac 同时运行相同闲鱼账号。

若运行目录来自无 Git 的离线包，不能直接 `git pull`；应先识别现有卷和配置，将新源码部署到原运行位置，或建立本项目项目仓库检出并明确沿用原 Compose 项目和卷。不要在未知状态直接 clone 后启动或覆盖已有文件。

升级后检查容器 healthy、Windows 页面访问、登录与知识库记录保留、账号状态、本地试答；不自动发买家消息或全量同步。构建失败先保留旧服务并查日志。需要回退时保留当前数据与配置，在确认数据结构兼容后使用升级前版本重新构建；不要以删卷或重新生成密钥解决。

## 10. 从早期私有历史切换到公开源码

公开版仅包含脱敏源码并建立新历史。旧私有版本的 `git pull --ff-only` 因历史无关可能失败，这是保护性停止；不要强制合并两个历史、reset 丢弃本地改动或把私有历史推回公开仓库。

Windows Agent 应先记录旧运行目录、提交、Compose 项目名、数据卷、配置和已有备份；在 WSL 的另一个 Linux 路径克隆公开仓库，检查 `AGENTS.md` 与部署文档。使用当前已有的 Compose 项目和命名卷，安全复制原 `config/customer.env` 及管理员凭据到新运行副本，仅在本地保留。自定义 override 需要核对其相对路径与机器网络。不得复制旧 `.git`、上传真实知识库、重新初始化主密钥或启动第二个独立项目。

确认新配置指向同一数据卷、端口和 Compose 项目后，在维护时段从新运行目录重新构建并替换原容器。原目录和备份先保留；验证登录、知识库和账号状态后，以新运行副本作为后续更新目录。远程公开行为不会修改 Windows 已运行的容器或数据。

## 11. 部署时自动同步私有 FAQ（配置一次）

代码更新来自公开 Git；真实 FAQ 更新来自用户本地或 Windows 共享目录，两者分别管理。首次在实际 WSL 运行目录执行一次：

```bash
python3 scripts/sync-local-faqs.py --configure --source '/实际共享路径/deployment-assets/customer-faq-updates.json' --account default
```

源文件应指向 Mac 和 Windows 共同可见、后续持续维护的那份文件，而不是一次性复制到 WSL 的过期副本。使用 `wslpath` 确定 Windows 路径在 WSL 中的实际位置。默认复用 `.local/windows-admin.json`，其他凭据位置用 `--credentials`；端口默认读取本机 `config/customer.env`，也可显式传 `--origin http://127.0.0.1:实际端口`。不创建或重置管理员。多账号仅同步 `--account` 指定的账号。

配置存在 `.local/faq-sync.json`，始终被 Git 忽略。之后每次在同一 WSL 运行目录更新：

```bash
git pull --ff-only origin main
bash scripts/customer.sh start
```

启动脚本先检查共享文件、格式和凭据文件是否可用，再构建和启动容器，等待本地 API 就绪，自动按 FAQ ID 原子合并。文件列出的同 ID 条目以私有源文件为准，其余已有 FAQ 保留；从源文件删除条目不会删除库中条目。相同配置不重复写入或产生备份。发生变更时先备份旧 FAQ 到 `.local/faq-backup-*.json`，结果写入 `.local/faq-sync-status.json`，均不含于公开仓库。同步期间不要同时从页面保存整组 FAQ；受源文件管理的条目在页面修改后，可能在下一次部署时被源文件覆盖。

没有配置过自动同步时保持旧行为并明确提示跳过。已经配置后，如果共享盘未挂载、文件损坏或凭据文件缺失，预检失败会阻止本次重建；服务启动后发生登录、API 或合并失败则命令返回失败，既有数据保留，不能报告部署全部完成。排障时查看本地错误和备份，不删库、不换密钥。不自动恢复接管会话，不调用模型、不请求闲鱼、不刷新商品，也不自动同步 Excel 或替换知识库。

换运行目录时须安全迁移本机 `.local/faq-sync.json` 及管理员凭据，并核对绝对路径；不要将机器配置、输入快照、备份、真实知识库或政策文件加入 Git、Release 或 Actions 附件。

## 12. 无需 Agent 的日常启动（PowerShell）

根目录 `Start-XianyuIntelligentCs.ps1` 支持 Windows PowerShell 5.1。可复制到桌面单独使用，不必安装 Windows Docker CLI 或 Docker Desktop，也不需要把 Windows PowerShell 以管理员身份运行。

**方式一：文件启动。** 在资源管理器中右键该文件，选择“使用 PowerShell 运行”。普通双击 `.ps1` 在很多系统中会打开编辑器，不能视为启动。脚本会识别已安装的 WSL 发行版和原容器的运行目录；存在多个发行版或多个候选目录时停止并提示指定参数，不创建新部署。首次成功后把选择保存在脚本旁的 `.local/windows-start.json`，后续右键可直接复用。

**方式二：PowerShell 命令启动。** 将路径替换为 `.ps1` 文件实际位置：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\实际目录\Start-XianyuIntelligentCs.ps1" -NoPause
```

首次有多个候选时，显式传入真实参数；示例值不是机器实际配置：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\实际目录\Start-XianyuIntelligentCs.ps1" -Distro "Ubuntu" -ProjectPath "/home/你的WSL用户/apps/xianyu-intelligent-cs-public" -NoPause
```

如需使用原部署用户，增加 `-WslUser "实际用户名"`。查询信息可运行 `wsl --list --verbose` 和 `wsl --exec bash -lc 'find ~/apps -maxdepth 3 -name compose.customer.yml 2>/dev/null'`；第二条查询默认发行版，多发行版时加 `--distribution` 指定。项目路径必须是 WSL Linux 路径。脚本运行位置可与 WSL 项目位置不同。

脚本仅以 WSL root 启动本地 Docker daemon，随后以 WSL 默认用户或 `-WslUser` 指定用户运行应用脚本。Docker 已就绪时直接复用；未就绪时按 systemd、已有 init 脚本、手动 dockerd 的顺序选择适用分支，避免重复启动 daemon。无法使用 Docker socket 时明确报错，不擅自修改用户组或以 root 创建应用配置。

日常启动优先使用已有镜像，调用 `scripts/customer.sh run`，不自动拉取 Git 或构建镜像；较旧运行副本尚无 run 命令时沿用其 start 命令并提示可能构建。需要仅重建本地代码时增加 `-Build`；需要自动拉取最新源码并重建时增加 `-Update`，或使用下节的一键更新入口。两种模式都保留已配置的私有 FAQ 自动同步，并保留数据卷、主密钥、账号状态及会话接管状态。

脚本会检查本地 API 就绪、Windows 页面可访问，成功后显示并打开工作台。`-NoBrowser` 可只启动而不打开浏览器，`-NoPause` 可结束后不等待回车。若 Windows localhost 转发不可用，会明确区分“WSL 内服务已启动”和“Windows 无法访问”，不会删库重装或修改防火墙。执行策略 Bypass 仅适用于这一进程，不修改系统长期策略。

关闭启动窗口不会执行停止命令；WSL 停止、Windows 关机或休眠仍可能中断服务。脚本不设置开机任务、不批量恢复会话、不扫码、不主动同步商品或发送测试消息；已有明确启用的客服可能按原来的运行状态恢复连接。此脚本需在用户 Windows 实机进行最终验收。


## 13. 一键拉取、重建并启动（PowerShell）

首次将仓库中最新的 `Start-XianyuIntelligentCs.ps1` 和 `Update-XianyuIntelligentCs.ps1` 放在 Windows 同一目录（例如桌面）。更新入口复用启动入口，两份文件需同时保留；不要求安装 Windows Docker 或打开 Agent。已有旧版启动脚本时也要替换为最新文件。不要从共享盘复制 Mac 的 `.local/compose.local.yml`、密钥或凭据。

右键 `Update-XianyuIntelligentCs.ps1`，选择“使用 PowerShell 运行”，即自动拉取、构建、启动及检查。首次沿用同目录 `.local/windows-start.json` 的 WSL 发行版、Linux 运行目录和用户；未保存选择或存在多个候选时，先显式指定一次：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Desktop\Update-XianyuIntelligentCs.ps1" -Distro "Ubuntu-22.04" -ProjectPath "/home/你的WSL用户/apps/xianyu-intelligent-cs-public" -WslUser "你的WSL用户" -NoPause
```

保存成功后可简化为：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Desktop\Update-XianyuIntelligentCs.ps1" -NoPause
```

只保留单份启动脚本也可以使用同等命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Desktop\Start-XianyuIntelligentCs.ps1" -Update -NoPause
```

脚本启动 WSL2 Docker 后，在已有运行目录执行 Git 检查、获取公开仓库 `origin/main`、快进更新，然后调用最新版 `scripts/customer.sh start` 重建和启动容器。即使代码已是最新，也会执行构建启动流程。确认本地 API 和 Windows 页面可访问后才显示完成。已有私有 FAQ 自动同步配置继续生效；源文件不可用或导入失败时报告失败。没有配置 FAQ 同步时保持原来的本地问答。

遇到代码未提交、分支不是 main、本地存在额外提交、远程仓库不符、网络错误或同时运行另一更新任务时停止；不执行强制覆盖、删库、卷删除、密钥初始化或管理员重置。未跟踪及忽略的配置保留；未跟踪文件与新源码冲突时 Git 会停止合并，需要自行整理。构建失败时不会因为构建步骤主动停止原有容器；构建完成进入容器切换后若检查失败，应查看本地错误及容器状态，不能视为更新成功。

`.local/windows-update.json` 记录本次更新前和目标提交，供排错使用，不是自动回滚承诺。更新会短暂重建服务；已有运行的客服可能按其原有运行意图恢复，未启用的客服不会因该脚本自动启用。脚本不扫码、不做商品同步或模型试答。重要更新前仍建议按原部署方式备份数据卷和私有配置。Windows WSL 实机的最终部署效果需要用户验收。


### 更新时明确指定私有 FAQ 文件

`Update-XianyuIntelligentCs.ps1` 和 `Start-XianyuIntelligentCs.ps1 -Update` 支持 `-FaqSource` 指定 Windows 或 WSL 内的绝对文件路径。Windows 盘符路径会经 `wslpath` 转换；对应盘符或共享目录必须已经能在 WSL 读取，否则更新入口报告失败，不构建容器。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\项目目录\Update-XianyuIntelligentCs.ps1" -FaqSource "D:\项目目录\deployment-assets\customer-faq-updates.json" -AccountKey "default" -NoPause
```

这会在拉取代码后、构建之前配置并校验私有同步源，复用 WSL 原有 `.local/faq-sync.json` 的凭据位置、端口和账号；显式指定 `-AccountKey` 时只调整目标账号。首次没有配置时使用已有 `.local/windows-admin.json` 凭据及默认账号。不会创建或重置管理员。源文件需要存在且格式有效；导入会按 ID 合并、保留其他问答并备份变化前的 FAQ。

针对具体店铺的话术和专用更新入口只通过本地私有 `deployment-assets` 交付，不能依赖 Git 拉取获得。运行私有入口时请保留该目录与主目录脚本的相对位置，并使用当前共享目录中的最新版脚本；单独复制一份旧 FAQ 到别处不会自动获得后续更新。公开仓库、Release 和 CI 中不包含真实店铺政策。
