# Russia Crossborder ERP

俄罗斯跨境 ERP 的公开源码快照，包含产品库、OZON/WB 订单与上架、POD 处理、供应商集成、任务中心，以及 Windows 桌面执行客户端。

本快照基于 2026-10-20 的代码，包含手工 POD 订单和订单同步范围功能。公开副本不包含生产数据库、账号凭据、会话、真实订单、运行缓存、历史排查报告、一次性生产运维脚本或私有 Git 历史。生产服务地址与存储桶名称已替换为示例值；公开仓库不会自动同步生产环境。

## 开发环境

- Python 3.12 或更新版本；Windows 桌面客户端需要 Tk。
- Node.js 与 npm（依赖锁定在 frontend/package-lock.json）。
- 本地可用 SQLite；部署配置支持 PostgreSQL。

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
cd frontend
npm ci
npm run build
cd ..
.\.venv\Scripts\python.exe -m erp.migrate
.\.venv\Scripts\python.exe -m uvicorn erp.main:app --host 127.0.0.1 --port 8788
```

根据 `.env.example` 设置独立的开发环境、密钥和认证；示例值不能访问生产系统。平台业务功能需要自行配置相应平台的账号和权限。本地数据默认写入 data/，不要将其提交到 Git。

```powershell
.\.venv\Scripts\python.exe -m pytest
cd frontend
npm run build
```

## 桌面客户端与部署

`client/desktop/` 提供客户端源码，`scripts/build_desktop_client.py` 构建安装包。超分功能所需的第三方 Real-ESRGAN 程序、运行库及模型未放入公开源码，请按 `tools/realesrgan-ncnn-vulkan/README.md` 准备后构建。不要直接在客户端源码目录运行 install.cmd；安装需要构建产生的 manifest.json。

`deploy/compose.yaml`、`deploy/Dockerfile` 与示例环境配置供自行部署使用。先构建前端，再配置自己的数据库凭据和应用密钥。服务器协调任务并保存共享结果；个人电脑业务任务由发起人的电脑执行。

## 本次订单功能

- 手工订单：选择 OZON/WB 店铺、填写订单号、从产品库选择规格和数量，进入待采购列表。
- 同步订单：1/3/7/15/30 天或自定义范围，默认3天；支持按平台单号，图片同步可选。
- 时间筛选基于订单创建时间；OZON使用发货单号，WB在选定范围内查找数字组装任务ID。
- 新同步范围需要支持相应协议的桌面客户端；此快照包含更新后的源码。

公开代码并不包含平台账号或生产访问权限，也不表示所有平台接口已在新部署中完成真实验证。
