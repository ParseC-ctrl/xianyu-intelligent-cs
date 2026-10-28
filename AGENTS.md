# xianyu-intelligent-cs 开发与部署入口

用户反馈 Windows WSL 已完成首次部署；后续在 Mac 共享主目录开发并推送，Windows WSL 固定运行副本拉取、重新构建部署，保留当前定制客服功能与运行数据。先读 `WINDOWS-DEPLOYMENT.md`、`SOURCE-REVISION.txt`、`docs/CUSTOMER-KNOWLEDGE.md`，再检查当前 Windows/WSL/Docker 状态并执行部署。

- 用户 Windows 较旧，部署采用 WSL2 内原生 Docker Engine + Compose 插件，不要求或安装 Docker Desktop。先确认目标发行版 VERSION=2、发行版受支持，再按 WINDOWS-DEPLOYMENT.md 安装及检查本地 daemon。所有 docker 命令在目标 WSL 内执行。
- 按 PID 1 选择 systemd 或手动启动路线；旧 WSL 无 systemd 时不能只因 systemctl 不可用而停止。检查实际 daemon 错误，不并发启动多个 daemon，不暴露 Docker TCP 端口。WSL/Windows 重启和休眠会影响运行，必须说明恢复方式。
- 当前目录可能是共享盘或 Windows NTFS。用于运行的副本放在 WSL2 Linux 文件系统，例如 `~/apps/xianyu-intelligent-cs`。用 `compose.customer.yml` 和 `scripts/customer.sh`，不要误启上游通用 `docker-compose.yml`。
- 当前共享目录是后续开发和升级的主目录，统一名称 `xianyu-intelligent-cs`；远程为公开源码仓库 `ParseC-ctrl/xianyu-intelligent-cs`，通过 HTTPS 管理。保留 GPL-3.0-only 及上游署名。不要用上游源码覆盖定制功能。
- 在主目录修改并提交源码，WSL 运行副本从同一版本部署，不维护另一套分叉代码。离线包不带 Git 历史；需要 Git 管理时使用本项目仓库。业务 Excel、真实 FAQ、凭据和运行数据禁止提交远程。
- 检查是否已有同名 Compose 项目及数据卷，保留已有数据。不要 `down -v`，不要删数据库、重新生成已有主密钥或擅自重置管理员密码。
- 本包没有 Mac 的 Cookie、API Key、管理员密码、加密主密钥、Colima DNS 覆盖、数据库或聊天记录。真实 Excel 和店铺 FAQ 只存在使用者本地目录，禁止进入公开仓库、历史提交、Release、Actions 附件或截图。部署生成 Windows 自己的配置和管理员凭据。
- 新部署先执行 `python3 scripts/setup-customer.py`，再启动容器；使用 `python3 scripts/windows-initialize.py --seed-knowledge` 创建首个管理员并只向空知识库导入资料。凭据保存在 `.local/windows-admin.json`，不把明文密码或模型 Key 放入日志、Git、对话报告。
- 如果已有管理员而凭据不匹配，保留文件与卷并说明需要现有管理员凭据，不绕过登录或重置数据。
- 先完成本地健康检查、知识查询、账号页和配置读取验收。不要自动开始闲鱼客服、扫码、同步商品或发送买家消息。需要真实账号验证时由用户扫码。
- 用户在 Windows 配置 DeepSeek Key。客服试答明确“仅试答，不发送闲鱼”后才可按用户授权运行模型测试。首次只使用测试账号；同一个账号不要在 Mac/Windows 同时运行客服。
- 正常续期已有调度。账号页轮询只能读本地 `/api/bot/status`，不得用额外闲鱼请求做保活或探测。
- 常规可逆的安装、初始化、检查可持续执行。需要用户真实扫码、模型 Key、Windows 管理员权限或重启时，说明具体缺失项，并继续能独立完成的步骤。不要把“容器启动成功”当作账号与自动回复都已验收。
- 收尾报告 WSL 发行版、项目实际路径、Compose 状态、访问地址、凭据文件位置、已完成/未完成检查和必须由用户处理的事项。不要声称已在真实 Windows 上通过了本包尚未执行的检查。

- 客服面向买家的转接提示统一使用“我看看哈”，不出现人工客服、转人工、待处理队列等系统术语；后台接管和状态功能保留，商品材质名称不得因此误改。语气轻快亲切，区分致谢、确认与“那好吧”等接受情绪，避免冷淡收尾、重复规则和催单。

- 公开源码只使用合成测试数据。修改后运行 tests/public-data-contract.py；不要推送私有归档分支、使用 git push --all/--mirror，或公开本地业务迁移包。

- 已配置 `.local/faq-sync.json` 的部署通过 `scripts/customer.sh start` 自动同步私有 FAQ；源文件按 ID 管理对应条目，其他已有问答必须保留。源文件不可用时不得静默跳过或把同步失败报告为部署成功；不得自动恢复接管会话或将真实政策移入公开仓库。

- 客服专用模式仅手动同步商品资料，历史定时配置不得继续提交任务。账号授权与客服启停分开：扫码成功不自动启动客服，最近授权时间不冒充令牌续期时间。

## 实验功能发布限制

未读会话同步和读取平台聊天历史属于本地实验，未经用户另行明确授权，不得进入正式分支或 Windows 更新。发布时从远程正式版本移植已验收的修复，检查最终 diff 及新增提交历史，禁止整段合入包含实验代码的本地开发分支。真实会话测试数据仅私有保存。
