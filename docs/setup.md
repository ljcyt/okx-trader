# 安装与 OKX 模拟盘配置

## 1. 安装

    pip install -e .
    cp okx_trader/okx_config_template.py okx_trader/okx_config.py

## 2. 创建模拟盘 API Key（实盘 Key 无效！）

1. OKX 网页 → 顶部「交易」→「模拟交易」
2. 右上角头像 → API → 创建 API Key
3. 权限勾选：**读取 + 交易**（不需要提现权限）
4. 把 API Key / Secret / Passphrase 填入 `okx_trader/okx_config.py`

⚠ 密钥安全：`okx_config.py` 已被 .gitignore 忽略；建议额外收紧文件权限
（Windows：右键属性→安全→仅保留自己；Linux/macOS：`chmod 600`）。
程序内密钥以 SecretStr 掩码，traceback 不会泄露明文。

## 3. 环境自检

    python -m okx_trader check-env

## 4. 启动

    # 纸面（不需要 Key）
    python -m okx_trader run-loop --serve

    # 模拟盘真实下单（okx_config.py: TRADING_ENV="demo"）
    python -m okx_trader run-loop --serve

面板：http://127.0.0.1:8787 （WEB_PASSWORD）

## 5. 网络

国内直连 okx.com 常不可达：在 okx_config.py 配 `OKX_PROXY = "http://127.0.0.1:7890"`
（按你的代理工具实际端口）。代理节点间歇不稳时，本轮因子失败会被记为
`data_unavailable`（事件页可见），下一轮自动恢复。

## 6. 实盘

刻意设置双重门槛：`TRADING_ENV="live"` **且** 手打 `ALLOW_LIVE_TRADING=True`
（该字段不在模板里）。本版本明确建议只跑模拟盘。

## 7. 常驻运行

- Windows：`schtasks /create /tn okx-trader /sc onstart /ru <user>`
  或用 [nssm](https://nssm.cc/) 把 `python -m okx_trader run-loop --serve` 注册成服务
- Linux：systemd unit（`Restart=always`），示例见仓库 issues 模板

循环进程自带单例锁（127.0.0.1:8777）：第二个实例会直接退出，
杜绝两个循环同时操作一个账户。
