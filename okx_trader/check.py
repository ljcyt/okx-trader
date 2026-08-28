# -*- coding: utf-8 -*-
"""check-env：环境自检（唯一碰网络的命令，永不进 CI）。

检查项：Python 版本 → python-okx 库 → 配置 → 网络连通（公开行情）→ 凭证（env 需要
真实凭证时才查）。全程只读，不下单。
"""
import sys


def run_checks():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from . import __version__
    from .config import get_logger, load_config
    from .env import resolve_env

    print(f"okx-trader v{__version__} 环境自检")
    ok = True

    v = sys.version_info
    print(f"[1/5] Python {v.major}.{v.minor}.{v.micro}",
          "通过" if v >= (3, 10) else "失败（需要 >= 3.10）")
    ok &= v >= (3, 10)

    try:
        import okx  # noqa: F401
        print("[2/5] python-okx 已安装：通过")
    except ImportError:
        print("[2/5] python-okx 未安装：失败 → pip install -e .[okx-trader]")
        return 1

    cfg = load_config()
    env = resolve_env(cfg)
    print(f"[3/5] 配置：通过（TRADING_ENV={env.name}，executing={env.executing}）")
    if env.needs_creds:
        have = (cfg.has_credential("OKX_API_KEY")
                and cfg.has_credential("OKX_SECRET_KEY")
                and cfg.has_credential("OKX_PASSPHRASE"))
        print(f"[3.5] 凭证已填：{'通过' if have else '未填——demo 环境无法启动'}")
        ok &= have

    print("[4/5] OKX 网络连通性（公开行情，无需凭证）")
    proxy = (cfg.OKX_PROXY or "") or None
    try:
        from okx import MarketData
        md = MarketData.MarketAPI(flag="1", proxy=proxy, debug=False)
        resp = md.get_ticker("BTC-USDT-SWAP")
        if resp.get("code") == "0":
            print(f"       通过：BTC-USDT-SWAP 最新价 {resp['data'][0]['last']}"
                  + (f"（代理 {proxy}）" if proxy else ""))
        else:
            print(f"       失败：{resp.get('msg')}")
            ok = False
    except Exception as e:  # noqa: BLE001
        print(f"       失败：{type(e).__name__}: {e}")
        print("       提示：直连超时就在 okx_config.py 配 OKX_PROXY")
        ok = False

    print("[5/5] 凭证有效性（只读调用，不下单）")
    if env.needs_creds and ok:
        try:
            from okx import Account
            client = __import__("okx_trader.client", fromlist=["OKXClient"]).OKXClient(
                cfg, logger=get_logger(level="ERROR"), flag=env.okx_flag)
            resp = client.account.get_account_config()
            if resp.get("code") == "0":
                d = resp["data"][0]
                print(f"       通过：UID={d.get('uid')} acctLv={d.get('acctLv')} "
                      f"posMode={d.get('posMode')}")
            else:
                print(f"       失败：{resp.get('msg')}")
                hints = {
                    "50111": "签名不正确——检查 Key/Secret/Passphrase",
                    "50113": "请求时间戳过期——同步系统时间",
                    "50102": "该 Key 不能用于当前环境——确认是模拟盘专属 Key",
                }
                if resp.get("code") in hints:
                    print(f"       可能原因：{hints[resp['code']]}")
                ok = False
        except Exception as e:  # noqa: BLE001
            print(f"       失败：{type(e).__name__}: {e}")
            ok = False
    else:
        print("       跳过（paper/replay 环境不需要凭证）")

    print("\n结论：" + ("全部通过 ✔" if ok else "存在未通过项 ✘"))
    return 0 if ok else 1
