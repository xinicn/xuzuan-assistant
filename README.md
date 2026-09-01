# 续赚抢单助手

续赚抢单助手是一个 Windows 桌面工具，提供账号登录、订单监听、平衡抢单、续赚 WAP 页面和可选机器人通知。

## Windows 下载

前往 [Releases](https://github.com/xinicn/xuzuan-assistant/releases/latest) 下载最新版本：

- `Xuzuan-Assistant-Setup-x64.exe`：推荐，双击安装后从开始菜单启动。
- `Xuzuan-Assistant-Portable-x64.zip`：免安装，完整解压后运行其中的 `续赚抢单助手.exe`。

支持 Windows 10/11 64 位。安装包暂未进行商业代码签名，Windows 可能显示“未知发布者”。

## 使用

1. 打开续赚抢单助手。
2. 输入续赚账号、登录密码、开放接口密钥和商品编号。
3. 按“启动”开始监听，按“停止”结束运行。

账号、登录密码、开放接口密钥和商品编号只用于当前运行，不会写入配置文件。钉钉和企业微信通知配置会保存在当前 Windows 用户的应用数据目录；内置网页的登录 Cookie 和缓存也会保存在该用户目录中。

## 风险说明

- 本工具仅用于账户本人操作和技术交流，请在使用前确认续赚平台规则及相关订单的履约要求。
- 自动接单可能触发平台限流、风控或账号限制，使用者应自行控制运行时间并承担相应风险。
- 续赚相关接口使用 HTTP/WS，部分通信并非端到端加密，请勿在不可信网络中使用。
- 请勿将账号、密码、开放接口密钥、通知 Webhook 或本地配置提交到公开仓库。

## 从源码运行

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python xuzuan_v3.py
```

## 构建 Windows 版本

推送 `xuzuan-v*` 标签后，GitHub Actions 会在 Windows 环境中构建并启动检查程序，然后将安装版和便携版发布到 Releases。也可以在 Actions 页面手动运行临时构建。
