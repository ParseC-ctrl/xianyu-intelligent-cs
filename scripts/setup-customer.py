#!/usr/bin/env python3
"""Create private local configuration for the customer-service-only container."""
import base64
import os
from pathlib import Path

root = Path(__file__).resolve().parents[1]
config = root / "config" / "customer.env"
config.parent.mkdir(parents=True, exist_ok=True)
if config.exists():
    print("现有 config/customer.env 已保留，不覆盖配置或密钥。")
else:
    # O_EXCL avoids replacing configuration created by another setup process.
    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("SAAS_AI_MASTER_KEY=" + base64.b64encode(os.urandom(32)).decode("ascii") + "\n")
        stream.write("SAAS_CUSTOMER_WEB_PORT=4173\n")
    print("已生成 config/customer.env。文件含本地加密密钥，请勿提交或分享。")
print("启动：bash scripts/customer.sh start")
print("工作台：http://127.0.0.1:4173/xianyu-intelligent-cs/")
