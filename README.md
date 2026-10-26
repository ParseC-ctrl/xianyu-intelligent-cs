# xianyu-intelligent-cs

闲鱼 AI 智能客服，结合商品描述／图片货号、Excel 材质知识库和店铺常见问答生成回复。支持连续消息合并、上下文对话、回复风格控制、商品同步进度、账号连接与续期状态展示。

## 部署与开发

Windows 本地部署请先阅读 [WINDOWS-DEPLOYMENT.md](WINDOWS-DEPLOYMENT.md)，Agent 开始前读取 [AGENTS.md](AGENTS.md)。当前 Windows 使用 WSL2 内原生 Docker Engine + Compose 插件，无需 Docker Desktop，使用 `compose.customer.yml`，不使用通用 Compose 启动客服版本。

```bash
python3 scripts/setup-customer.py
bash scripts/customer.sh start
```

新部署访问 `http://127.0.0.1:4173/xianyu-intelligent-cs/`。初始化管理员见 Windows 文档，无固定初始密码。

本目录是后续开发与升级的主目录。仓库公开发布脱敏源码，通过 HTTPS 管理：`https://github.com/ParseC-ctrl/xianyu-intelligent-cs.git`。

Excel 和真实店铺 FAQ 由使用者自行保存在本地 `deployment-assets/`，不提交远程，也不放入公开 Release、Actions 附件或演示截图。Git 克隆后需自行复制该目录再运行知识库导入。Cookie、API Key、管理员凭据、数据库和加密主密钥均不得提交。

账号运行依赖第三方平台，无法保证不触发平台限制；默认减少额外调用，续期展示读取本地状态。首次部署不自动连接闲鱼、同步商品或给买家发消息。同一账号切换机器前先停止旧机器客服。

通用部署入口保留在 `deploy/docker-install.sh`，其说明见 `docs/DEPLOYMENT.md`；当前客服专用版本按本文及 Windows 文档操作。

## 来源与许可证

本项目从 [tswawa/xianyu-saas](https://github.com/tswawa/xianyu-saas) 的定制版本迁移，初始来源见 [SOURCE-REVISION.txt](SOURCE-REVISION.txt)。上游项目与第三方许可证见 LICENSING.md。

保留 **GPL-3.0-only** 许可证，见 [LICENSE](LICENSE) 和 [LICENSING.md](LICENSING.md)。Worker 来源及版权见 [worker/NOTICE.md](worker/NOTICE.md)。公开发布继续遵循继承的许可证。

从早期私有版本迁移到公开历史的部署用户，请先阅读 Windows 文档第 10 节，保留数据和配置，避免强制合并旧历史。

Windows 日常启动可使用根目录 `Start-XianyuIntelligentCs.ps1`，支持右键运行和 PowerShell 命令两种入口，详见 Windows 部署文档第 12 节。
