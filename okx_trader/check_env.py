# -*- coding: utf-8 -*-
"""OKX 模拟盘环境自检脚本（第一步交付物）

用法（在仓库根目录执行）：
    python okx_trader/check_env.py

检查项（由浅入深，任一失败即停并给出修复提示）：
    1. Python 版本（需要 >= 3.10）
    2. python-okx 库是否可用
    3. okx_config.py 是否存在且已填写凭证
    4. 公网连通性：调用 OKX 公开行情接口（不需要 API Key）
    5. 凭证有效性：调用模拟盘账户接口（只读，不会下单/不会改动任何状态）

全程只做只读查询，放心运行。
"""
import os
import sys
import time

# 保证 Windows 控制台能打印中文（GBK 控制台兜底转 UTF-8）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASS, FAIL, SKIP = "[通过]", "[失败]", "[跳过]"
WARN = "[警告]"

_installed_ok = {"python_okx": False, "config": False}


def _step_ok(idx, total, name):
    print(f"\n[{idx}/{total}] {name}")


def check_python(total):
    v = sys.version_info
    version_str = f"Python {v.major}.{v.minor}.{v.micro}"
    if v >= (3, 10):
        print(f"  {PASS} {version_str}")
        return True
    print(f"  {FAIL} {version_str} — 本项目需要 Python >= 3.10")
    return False


def check_python_okx(total):
    try:
        from okx import Account, MarketData, PublicData, Trade  # noqa: F401
        import okx as _okx
        print(f"  {PASS} python-okx 已安装")
        _installed_ok["python_okx"] = True
        return True
    except ImportError:
        print(f"  {FAIL} python-okx 未安装")
        print("         修复：python -m pip install python-okx")
        return False


def load_config(total):
    """加载 okx_config.py，并判断凭证是否已填写。"""
    cfg_path = os.path.join(HERE, "okx_config.py")
    tpl_path = os.path.join(HERE, "okx_config_template.py")

    if not os.path.exists(cfg_path):
        print(f"  {FAIL} 找不到 {cfg_path}")
        print(f"         修复：把模板复制过去并填写凭证")
        print(f"         cp {tpl_path} {cfg_path}")
        return None, False

    import importlib.util
    spec = importlib.util.spec_from_file_location("okx_config", cfg_path)
    cfg = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(cfg)
    except Exception as e:
        print(f"  {FAIL} okx_config.py 语法错误：{e}")
        return None, False

    filled = (
        isinstance(getattr(cfg, "OKX_API_KEY", ""), str)
        and "在此填入" not in getattr(cfg, "OKX_API_KEY", "")
        and "在此填入" not in getattr(cfg, "OKX_SECRET_KEY", "")
        and "在此填入" not in getattr(cfg, "OKX_PASSPHRASE", "")
    )
    if filled:
        flag_desc = "模拟盘" if str(getattr(cfg, "OKX_FLAG", "1")) == "1" else "实盘（危险！）"
        print(f"  {PASS} 凭证已填写（环境：{flag_desc}）")
        if str(getattr(cfg, "OKX_FLAG", "1")) != "1":
            print(f"  {WARN} OKX_FLAG != \"1\"，后续操作会用【真实资金】！确认无误再继续。")
    else:
        print(f"  {SKIP} 凭证尚未填写（{cfg_path}）——第 5 项将跳过")
        print("         打开该文件，填入模拟盘 API Key / Secret / Passphrase")
    return cfg, filled


def check_network(cfg, total):
    """调用公开行情接口（无需凭证），验证到 OKX 的网络连通性。"""
    from okx import MarketData

    proxy = (getattr(cfg, "OKX_PROXY", "") or None) if cfg else None
    if proxy:
        print(f"  使用代理：{proxy}")
    try:
        t0 = time.time()
        md = MarketData.MarketAPI(flag="1", proxy=proxy, debug=False)
        resp = md.get_ticker("BTC-USDT-SWAP")
        cost = time.time() - t0
        code = resp.get("code")
        if code == "0":
            last = resp["data"][0]["last"]
            print(f"  {PASS} OKX 公开行情连通（{cost:.1f}s）BTC-USDT-SWAP 最新价：{last}")
            return True
        print(f"  {FAIL} 行情接口返回异常：code={code} msg={resp.get('msg')}")
        return False
    except Exception as e:
        print(f"  {FAIL} 无法连接 OKX：{type(e).__name__}: {e}")
        print("         提示：如直连超时，在 okx_config.py 里配置 OKX_PROXY（如 http://127.0.0.1:7890）")
        return False


def check_credentials(cfg, total):
    """用凭证调用账户配置接口（只读），验证模拟盘 Key 有效性。"""
    from okx import Account

    proxy = (getattr(cfg, "OKX_PROXY", "") or None)
    acct = Account.AccountAPI(
        api_key=cfg.OKX_API_KEY,
        api_secret_key=cfg.OKX_SECRET_KEY,
        passphrase=cfg.OKX_PASSPHRASE,
        flag=str(cfg.OKX_FLAG),
        proxy=proxy,
        debug=False,
    )
    try:
        resp = acct.get_account_config()
    except Exception as e:
        print(f"  {FAIL} 请求异常：{type(e).__name__}: {e}")
        return False

    code = resp.get("code")
    if code == "0":
        data = resp["data"][0]
        print(f"  {PASS} 模拟盘账户验证成功")
        print(f"         UID={data.get('uid')}  账户模式 acctLv={data.get('acctLv')}  持仓模式 posMode={data.get('posMode')}")
        # 账户模式提示：1现货 2现货+杠杆 3杠杆 4多币种 5组合（跨式）。交易 SWAP 建议 >= 3。
        try:
            acct_lv = int(data.get("acctLv", "0"))
            if acct_lv < 3:
                print(f"  {WARN} 当前账户模式可能不支持合约（acctLv={acct_lv}）。")
                print("         若下单报错，去 OKX 网页把模拟盘账户切换为「杠杆模式/多币种模式/组合模式」。")
        except ValueError:
            pass
        # 持仓模式提示：net_mode=净持仓，long_short_mode=双向持仓（下单 posSide 字段不同）
        if data.get("posMode") == "long_short_mode":
            print("  提示：账户为双向持仓模式，客户端会自动使用 posSide=long/short 下单。")
        return True

    # 常见错误码速查
    hints = {
        "50111": "签名不正确 —— API Key / Secret Key / Passphrase 填错（注意别混入空格）",
        "50113": "请求时间戳过期 —— 本机时间与标准时间偏差过大，请同步系统时间",
        "50102": "该 Key 不能用于当前环境 —— 大概率是【实盘 Key】拿来连模拟盘（或反之），请确认是在「模拟交易」页面创建的 Key",
        "51000": "参数错误或凭证无效 —— 确认 Key 是模拟盘专属且未删除",
    }
    hint = hints.get(code, "")
    print(f"  {FAIL} 账户验证失败：code={code} msg={resp.get('msg')} data={resp.get('data')}")
    if hint:
        print(f"         可能原因：{hint}")
    return False


def main():
    total = 5
    ok = True

    _step_ok(1, total, "Python 版本")
    ok &= check_python(total)

    _step_ok(2, total, "python-okx 依赖库")
    ok &= check_python_okx(total)
    if not _installed_ok["python_okx"]:
        return finish(ok)

    _step_ok(3, total, "配置文件 okx_config.py")
    cfg, filled = load_config(total)
    ok &= cfg is not None  # 配置缺失/语法错误也算未通过

    _step_ok(4, total, "OKX 网络连通性（公开行情，无需凭证）")
    ok &= check_network(cfg, total)

    _step_ok(5, total, "模拟盘凭证有效性（只读调用，不下单）")
    if not filled:
        print(f"  {SKIP} 凭证未填写，跳过。填好后重新运行本脚本即可验证。")
    else:
        ok &= check_credentials(cfg, total)

    finish(ok)


def finish(ok):
    print("\n" + "=" * 60)
    if ok:
        print("结论：环境自检全部通过 ✔ 可以进入第二步（OKX 客户端封装）")
    else:
        print("结论：存在未通过项 ✘ 请按上面的提示修复后重跑本脚本")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
