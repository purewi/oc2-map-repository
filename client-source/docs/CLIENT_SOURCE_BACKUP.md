# OC2MapManager 客户端源码备份

这里保存公开版与 COS 专享版客户端共用的可构建源码。两个入口分别是：

- `client_main_public.py`：公开版
- `client_main.py`：COS 专享版

## 本地构建

在 Windows PowerShell 中创建 Python 3.11+ 虚拟环境，安装
`requirements.txt` 与 `requirements-build.txt`，然后运行：

```powershell
.\build_client.ps1
```

脚本会运行测试、分别构建两个单文件 EXE，并执行冒烟测试。客户端运行时不需要
GitHub Token；COS 密钥和服务端 `.env` 从未包含在这个源码备份中。

## 备份范围

备份包含 `client/`、客户端使用的 `publisher` 数据模型/版本解析模块、图标、代理源
配置、PyInstaller spec、构建脚本和客户端测试。服务端机器人、数据库、QQ 缓存、
地图归档、`.env`、Token 与构建产物不在备份范围内。
