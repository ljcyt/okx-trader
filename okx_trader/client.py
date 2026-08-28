# -*- coding: utf-8 -*-
"""OKX 模拟盘客户端封装（第二步交付物）

对 python-okx 官方库做一层薄封装，提供交易循环需要的最小接口集：
    账户：get_equity / get_positions / get_account_mode
    行情：get_ticker / get_funding_rate / get_candles / get_instrument
    交易：place_maker_limit / cancel_order / get_order / get_pending_orders
    止损：place_stop_loss / get_pending_stop_losses / cancel_stop_loss

设计要点：
    1. 模拟盘：所有请求自动带 x-simulated-trading: 1（python-okx 在 flag="1" 时自动加），
       REST 域名与实盘相同（www.okx.com）。
    2. 统一错误处理：所有响应 code != "0" 抛 OKXAPIError（带 code/msg）；
       网络错误和限频自动重试。
    3. 统一日志：每次调用记录到控制台 + logs/okx_client.log。
    4. SWAP 合约的 sz 单位是「张」（1 张 = ctVal 个币），本层提供
       base→张 的换算和价格/数量按交易所精度取整的工具。
"""
import importlib.util
import json
import logging
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from logging.handlers import RotatingFileHandler

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))

# 模拟盘固定 flag（"1"=Demo）。想连实盘就改 okx_config.py 里的 OKX_FLAG。
DEMO_FLAG = "1"

# 重试策略：网络错误/限频最多重试次数
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 1.0  # 每次重试翻倍：1s, 2s, 4s

# 请求超时（秒）。python-okx 未暴露 timeout 参数，这里在实例上直接覆盖 httpx 默认值。
REQUEST_TIMEOUT_SEC = 15.0


class OKXAPIError(Exception):
    """OKX 返回的业务错误（code != 0）。code/msg 来自交易所响应。"""

    def __init__(self, code, msg, data=None):
        self.code = code
        self.msg = msg
        self.data = data
        super().__init__(f"OKX API 错误 code={code} msg={msg} data={data}")


def get_logger(name="okx_trader", level="INFO"):
    """统一日志：控制台 + logs/okx_client.log（滚动，5MB×3 份）。"""
    logger = logging.getLogger(name)
    if logger.handlers:  # 已初始化过，避免重复 handler
        return logger

    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_dir = os.path.join(HERE, "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "okx_client.log"),
        maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


def load_config():
    """加载 okx_config.py（用户本地填写，不入库）。未填凭证直接抛错。"""
    path = os.path.join(HERE, "okx_config.py")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"找不到配置文件 {path}\n"
            f"请把模板复制过去：cp okx_trader/okx_config_template.py okx_trader/okx_config.py"
        )
    spec = importlib.util.spec_from_file_location("okx_config", path)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)

    for field in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
        if "在此填入" in str(getattr(cfg, field, "")):
            raise RuntimeError(
                f"okx_config.py 中的 {field} 还没填。"
                f"请填入【模拟盘专属】API Key（OKX 网页 → 交易 → 模拟交易 → API 创建）"
            )
    if str(getattr(cfg, "OKX_FLAG", "1")) != DEMO_FLAG:
        # 目前整套系统按模拟盘设计，防呆：改 flag 需要同时改这里
        raise RuntimeError("当前版本仅支持模拟盘（OKX_FLAG 必须为 \"1\"）")
    return cfg


# 周期 → 秒数（用于识别未收盘的当前 K 线）
BAR_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "2H": 7200, "4H": 14400, "6H": 21600, "12H": 43200, "1D": 86400,
}


class OKXDemoClient:
    """OKX 模拟盘客户端。tdMode 固定用 cross（全仓），简单可靠。"""

    def __init__(self, cfg=None, logger=None, api_key=None, api_secret_key=None,
                 passphrase=None, proxy=None):
        from okx import Account, MarketData, PublicData, Trade  # 延迟导入，方便单元测试

        self.cfg = cfg or load_config()
        self.log = logger or get_logger(level=getattr(self.cfg, "LOG_LEVEL", "INFO"))
        # 显式传入的凭证优先于配置文件（PaperClient 传 "-1" 表示不签名，且不改写 cfg）
        api_key = api_key if api_key is not None else self.cfg.OKX_API_KEY
        api_secret_key = (api_secret_key if api_secret_key is not None
                          else self.cfg.OKX_SECRET_KEY)
        passphrase = passphrase if passphrase is not None else self.cfg.OKX_PASSPHRASE
        proxy = proxy if proxy is not None else (getattr(self.cfg, "OKX_PROXY", "") or None)

        common = dict(flag=str(self.cfg.OKX_FLAG), proxy=proxy, debug=False)
        self.account = Account.AccountAPI(
            api_key=api_key, api_secret_key=api_secret_key, passphrase=passphrase,
            **common,
        )
        self.trade = Trade.TradeAPI(
            api_key=api_key, api_secret_key=api_secret_key, passphrase=passphrase,
            **common,
        )
        # 公开行情不需要签名，但传 flag 保证行为一致
        self.market = MarketData.MarketAPI(**common)
        self.public = PublicData.PublicAPI(**common)
        # 衍生品数据（OI/多空比/主动买卖量），部分端点模拟盘可能受限，失败会优雅降级
        try:
            from okx import TradingData
            self.trading_data = TradingData.TradingDataAPI(**common)
        except Exception:  # noqa: BLE001
            self.trading_data = None

        # 放宽 httpx 默认 5s 超时（AccountAPI 等本身就是 httpx.Client 子类）
        for api in (self.account, self.trade, self.market, self.public):
            try:
                api.timeout = httpx.Timeout(REQUEST_TIMEOUT_SEC)
            except Exception:
                pass

        self._instruments = {}   # instId -> 合约规格缓存（ctVal/lotSz/tickSz/minSz）
        self._acct_mode = None   # 账户模式缓存（net / long_short）

    # ────────────────────────── 内部通用请求 ──────────────────────────

    def _call(self, api_name, fn, *args, _order_resp=False, **kwargs):
        """统一请求入口：重试 + 日志 + 错误归一化。

        _order_resp=True 时（下单/撤单类接口），OKX 把业务错误放在 data[0].sCode 里，
        code 仍可能是 "0"（"1" 表示部分失败），需要进一步检查。
        """
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                t0 = time.time()
                resp = fn(*args, **kwargs)
                cost = (time.time() - t0) * 1000
                code = resp.get("code")
                msg = resp.get("msg", "")

                # 下单类接口：data[0].sCode 才是真正的业务结果
                if _order_resp and code in ("0", "1") and resp.get("data"):
                    item = resp["data"][0]
                    s_code = item.get("sCode", "0")
                    if s_code != "0":
                        self.log.error(
                            "%s 失败 sCode=%s sMsg=%s", api_name, s_code, item.get("sMsg")
                        )
                        raise OKXAPIError(s_code, item.get("sMsg", ""), item)

                if code != "0":
                    # 限频可重试
                    if code == "50011" and attempt < MAX_RETRIES:
                        delay = RETRY_BACKOFF_SEC * (2 ** attempt)
                        self.log.warning("%s 限频，%.1fs 后重试", api_name, delay)
                        time.sleep(delay)
                        continue
                    raise OKXAPIError(code, msg, resp.get("data"))

                self.log.debug("%s 成功 %.0fms", api_name, cost)
                return resp

            except (httpx.HTTPError, httpx.TransportError, TimeoutError) as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF_SEC * (2 ** attempt)
                    self.log.warning(
                        "%s 网络异常（%s），%.1fs 后重试（第 %d/%d 次）",
                        api_name, type(e).__name__, delay, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(delay)
                else:
                    self.log.error("%s 网络异常重试耗尽：%s", api_name, e)
                    raise
        raise last_exc  # pragma: no cover

    def _post(self, api_obj, method_name, api_name, payload, order_resp=False):
        """POST 类接口的统一包装（打 INFO 日志，方便复盘每次下单）。"""
        resp = self._call(
            api_name,
            getattr(api_obj, method_name),
            _order_resp=order_resp,
            **payload,
        )
        self.log.info("%s %s", api_name, json.dumps(payload, ensure_ascii=False))
        return resp

    # ────────────────────────── 合约规格 / 取整 ──────────────────────────

    def get_instrument(self, inst_id, refresh=False):
        """获取合约规格（带缓存）：ctVal 每张面值、lotSz/minSz 数量步长、tickSz 价格步长。"""
        if inst_id not in self._instruments or refresh:
            resp = self._call("get_instruments", self.public.get_instruments,
                              instType="SWAP", instId=inst_id)
            d = resp["data"][0]
            self._instruments[inst_id] = {
                "instId": d["instId"],
                "ctVal": float(d["ctVal"]),        # 1 张合约对应的币数（如 BTC 0.01）
                "ctValCcy": d["ctValCcy"],         # 面值币种（BTC）
                "settleCcy": d["settleCcy"],       # 结算币种（USDT）
                "lotSz": float(d["lotSz"]),        # 数量步长（张）
                "minSz": float(d["minSz"]),        # 最小下单量（张）
                "tickSz": float(d["tickSz"]),      # 价格步长
                "maxLmtSz": float(d.get("maxLmtSz", "0") or 0),
            }
            self.log.debug("加载合约规格 %s：%s", inst_id, self._instruments[inst_id])
        return self._instruments[inst_id]

    @staticmethod
    def round_price(px, tick_sz):
        """价格按 tickSz 取整（四舍五入）。用 Decimal 避免浮点尾巴。"""
        d_px = Decimal(str(px))
        d_tick = Decimal(str(tick_sz))
        ticks = (d_px / d_tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return float(ticks * d_tick)

    @staticmethod
    def round_size(sz, lot_sz, min_sz):
        """数量向下取整到 lotSz，并保证 >= minSz；不足最小量返回 0。"""
        d_sz = Decimal(str(sz))
        d_lot = Decimal(str(lot_sz))
        d_min = Decimal(str(min_sz))
        lots = int((d_sz / d_lot).to_integral_value(rounding=ROUND_DOWN))
        out = lots * d_lot
        return float(out) if out >= d_min else 0.0

    # ────────────────────────── 账户 ──────────────────────────

    def get_account_mode(self, refresh=False):
        """读取账户模式（带缓存）：posMode 决定下单时 posSide 怎么填。"""
        if self._acct_mode is None or refresh:
            resp = self._call("get_account_config", self.account.get_account_config)
            d = resp["data"][0]
            self._acct_mode = {
                "uid": d.get("uid"),
                "acctLv": d.get("acctLv"),        # 3杠杆 4多币种 5组合；<3 不能交易 SWAP
                "posMode": d.get("posMode"),      # net_mode / long_short_mode
            }
            self.log.info("账户模式：%s", self._acct_mode)
        return self._acct_mode

    def _pos_side(self, side):
        """根据账户持仓模式把【开仓方向】side 映射为 posSide 参数。

        net_mode（净持仓）：posSide 必须留空；
        long_short_mode（双向）：开多 posSide=long，开空 posSide=short。
        """
        mode = self.get_account_mode()["posMode"]
        if mode == "long_short_mode":
            return "long" if side == "buy" else "short"
        return ""

    def _pos_side_for_close(self, side):
        """【平仓方向】的 posSide：OKX 的 posSide 标识的是被操作的仓位本身。
        双向模式下，平多单（side=sell）的 posSide 是 long，平空单（side=buy）是 short。
        净持仓模式留空。"""
        mode = self.get_account_mode()["posMode"]
        if mode == "long_short_mode":
            return "long" if side == "sell" else "short"
        return ""

    def get_equity(self):
        """账户权益（统一折算美元）。返回 dict：
            total_eq   —— 总权益（USD，交易所折算）
            usdt_eq    —— USDT 账户权益
            usdt_avail —— USDT 可用保证金
        """
        resp = self._call("get_account_balance", self.account.get_account_balance)
        d = resp["data"][0]
        usdt_detail = next(
            (x for x in d.get("details", []) if x.get("ccy") == "USDT"), {}
        )
        return {
            "total_eq": float(d.get("totalEq") or 0),
            "usdt_eq": float(usdt_detail.get("eq") or 0),
            "usdt_avail": float(usdt_detail.get("availEq") or 0),
            "raw": d,
        }

    def get_positions(self, inst_id=""):
        """当前持仓（归一化）。net 模式用 pos 正负判断方向；双向模式用 posSide。"""
        resp = self._call("get_positions",
                          self.account.get_positions, instType="SWAP", instId=inst_id)
        positions = []
        for p in resp.get("data", []):
            pos = float(p.get("pos") or 0)
            if pos == 0:
                continue
            pos_side = p.get("posSide", "")
            if pos_side in ("", "net"):  # 净持仓模式
                direction = "long" if pos > 0 else "short"
            else:
                direction = pos_side  # long / short
            positions.append({
                "instId": p["instId"],
                "direction": direction,
                "contracts": abs(pos),                     # 张
                "avg_px": float(p.get("avgPx") or 0),      # 开仓均价
                "mark_px": float(p.get("markPx") or 0),    # 标记价格
                "upl": float(p.get("upl") or 0),           # 未实现盈亏（USD）
                "lever": float(p.get("lever") or 0),
                "liq_px": float(p.get("liqPx") or 0),      # 预估强平价
                "mgnMode": p.get("mgnMode", ""),
                "raw": p,
            })
        return positions

    def set_leverage(self, inst_id, lever):
        """设置杠杆（cross 模式按 instId 设置）。注意：set_leverage 在 AccountAPI 上。"""
        return self._call("set_leverage", self.account.set_leverage,
                          str(lever), "cross", instId=inst_id)

    # ────────────────────────── 行情 ──────────────────────────

    def get_ticker(self, inst_id):
        """最新行情。返回 ask1/bid1/last 等（float 化，去掉原 raw 冗余）。"""
        resp = self._call("get_ticker", self.market.get_ticker, inst_id)
        d = resp["data"][0]
        return {
            "instId": inst_id,
            "last": float(d["last"]),
            "ask": float(d["askPx"]) if d.get("askPx") else None,
            "bid": float(d["bidPx"]) if d.get("bidPx") else None,
            "ask_sz": float(d["askSz"]) if d.get("askSz") else None,
            "bid_sz": float(d["bidSz"]) if d.get("bidSz") else None,
            "vol24h_quote": float(d.get("volCcy24h") or 0),  # 24h 成交额（计价币）
            "ts": int(d.get("ts") or 0),
        }

    def get_funding_rate(self, inst_id):
        """当前资金费率。"""
        resp = self._call("get_funding_rate", self.public.get_funding_rate, inst_id)
        d = resp["data"][0]
        return {
            "instId": inst_id,
            "funding_rate": float(d["fundingRate"]),
            "next_rate": float(d.get("nextFundingRate") or 0),
            "next_time": int(d.get("fundingTime") or 0),
        }

    def get_candles(self, inst_id, bar="1H", limit=100):
        """K 线（时间升序返回，不含未收盘的当前 K 线），用于计算 ATR/波动率/因子。
        bar 可选：1m/5m/15m/1H/4H/1D 等。
        """
        resp = self._call("get_candles", self.market.get_candlesticks,
                          inst_id, bar=bar, limit=str(limit))
        rows = list(reversed(resp.get("data", [])))  # OKX 倒序 → 升序
        rows, dropped = self._clean_candle_rows(rows, bar)
        if dropped:
            self.log.debug("get_candles %s %s：丢弃 %d 根未收盘K线", inst_id, bar, dropped)
        return [{
            "ts": int(r[0]),
            "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]),
            "vol": float(r[5]),
        } for r in rows]

    @staticmethod
    def _clean_candle_rows(rows, bar, now_ms=None):
        """过滤未收盘 K 线（rows 为时间升序的原始响应行）。

        主判据：OKX 行下标 8 是 confirm（"1" 已收盘 / "0" 进行中）；
        响应缺 confirm 列时退回时间戳启发式（时钟偏移下可能误判，仅兜底）。
        未收盘的半根 K 线混进 RSI/ATR/量比/形态会污染所有基于收盘数据的因子。
        返回 (cleaned_rows, dropped_count)。
        """
        if now_ms is None:
            now_ms = time.time() * 1000
        cleaned, dropped = [], 0
        if rows and len(rows[0]) > 8:  # 有 confirm 列
            for r in rows:
                if str(r[8]) == "1":
                    cleaned.append(r)
                else:
                    dropped += 1
        else:  # 无 confirm 列：时间戳启发式兜底
            sec = BAR_SECONDS.get(bar)
            cleaned = list(rows)
            if sec:
                while cleaned and now_ms - int(cleaned[-1][0]) < sec * 1000:
                    cleaned.pop()
                    dropped += 1
        return cleaned, dropped

    def compute_atr(self, inst_id, period=14, bar="1H"):
        """简单 ATR（周期 TR 均值）：用在波动率目标仓位计算。
        请求 period+2 根：丢掉未收盘那根后仍剩 period+1 根 → 正好 period 个 TR。"""
        candles = self.get_candles(inst_id, bar=bar, limit=period + 2)
        if len(candles) < 2:
            raise OKXAPIError("DATA", f"{inst_id} K 线不足，无法计算 ATR")
        trs = []
        for prev, cur in zip(candles, candles[1:]):
            tr = max(
                cur["high"] - cur["low"],
                abs(cur["high"] - prev["close"]),
                abs(cur["low"] - prev["close"]),
            )
            trs.append(tr)
        return sum(trs) / len(trs)  # 简单平均即可满足"粗略估算"

    # ── 衍生品/微观数据（委员会因子用；端点失败返回 None，不抛异常）──────────

    def get_orderbook(self, inst_id, depth=20):
        """订单簿前 depth 档。"""
        try:
            resp = self._call("get_orderbook", self.market.get_orderbook,
                              inst_id, sz=str(depth))
            d = resp["data"][0]
            return {"bids": [(float(b[0]), float(b[1])) for b in d["bids"]],
                    "asks": [(float(a[0]), float(a[1])) for a in d["asks"]]}
        except Exception as e:  # noqa: BLE001
            self.log.debug("orderbook 获取失败：%s", e)
            return None

    def get_oi_history(self, inst_id, period="1H", limit=2):
        """未平仓合约量历史（升序），用于计算 OI 变化。"""
        if self.trading_data is None:
            return None
        try:
            resp = self._call("get_oi_history", self.trading_data.get_open_interest_history,
                              inst_id, period=period, limit=str(limit))
            rows = list(reversed(resp.get("data", [])))  # 转升序
            return [{"ts": int(r[0]), "oi": float(r[1]),
                     "oi_usd": float(r[2]) if len(r) > 2 else None} for r in rows]
        except Exception as e:  # noqa: BLE001
            self.log.debug("OI 历史获取失败：%s", e)
            return None

    def get_long_short_ratio(self, ccy, period="1H"):
        """全市场多空人数比（如 1.8 表示多头人多）。ccy 传 "BTC" 这种币种名。"""
        if self.trading_data is None:
            return None
        try:
            resp = self._call("get_long_short_ratio",
                              self.trading_data.get_long_short_ratio,
                              ccy, period=period)
            rows = resp.get("data", [])
            if not rows:
                return None
            return float(rows[0][1])  # 最新一条的比例
        except Exception as e:  # noqa: BLE001
            self.log.debug("多空比获取失败：%s", e)
            return None

    def get_taker_volume_ratio(self, ccy, inst_type="SWAP", period="1H"):
        """主动买卖量比：>1 表示主动买盘占优。"""
        if self.trading_data is None:
            return None
        try:
            resp = self._call("get_taker_volume", self.trading_data.get_taker_volume,
                              ccy, inst_type, period=period)
            rows = resp.get("data", [])
            if not rows:
                return None
            r = rows[0]
            # 字段：[ts, buyVol, sellVol]（数值为字符串）
            buy, sell = float(r[1]), float(r[2])
            return buy / sell if sell > 0 else None
        except Exception as e:  # noqa: BLE001
            self.log.debug("主动买卖量比获取失败：%s", e)
            return None

    # ────────────────────────── 交易 ──────────────────────────

    def maker_price(self, inst_id, side, px=None, price_offset_ratio=None):
        """计算 Maker 限价单的委托价（下单价与纸面模拟共用）。
        不传 px 时：买单挂在买一下方、卖单挂在卖一上方，保证只做 Maker。"""
        if px is None:
            ratio = price_offset_ratio
            if ratio is None:
                ratio = getattr(self.cfg, "MAKER_PRICE_OFFSET", 0.0005)
            ticker = self.get_ticker(inst_id)
            ref = ticker["bid"] if side == "buy" else ticker["ask"]
            if not ref:
                raise OKXAPIError("NOPX", f"{inst_id} 盘口无 {side} 方向价格")
            px = ref * (1 - ratio) if side == "buy" else ref * (1 + ratio)
        inst = self.get_instrument(inst_id)
        return self.round_price(px, inst["tickSz"])

    def place_maker_limit(self, inst_id, side, contracts, px=None,
                          price_offset_ratio=None, cl_ord_id="", reduce_only=False):
        """挂 Maker 限价单（默认主力方式）。

        side: "buy"（开多/平空）或 "sell"（开空/平多）
        contracts: 张数
        px: 委托价；不传则按盘口对侧价偏移 price_offset_ratio 自动定价：
            买：bid * (1 - offset)；卖：ask * (1 + offset)——保证挂在盘口后面，只做 Maker。
        返回订单 ID（ordId）。
        """
        inst = self.get_instrument(inst_id)
        contracts = self.round_size(contracts, inst["lotSz"], inst["minSz"])
        if contracts <= 0:
            raise OKXAPIError("SIZE", f"下单张数不足最小单位（minSz={inst['minSz']}）")

        px = self.maker_price(inst_id, side, px=px, price_offset_ratio=price_offset_ratio)

        payload = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": side,
            # post_only：若挂单瞬间会吃单，交易所直接拒单 —— 由交易所保证只做 Maker，
            # 而不是只靠本地算价时留偏移（行情在取盘口与撮合之间可能反向跳动）。
            "ordType": "post_only",
            "sz": self._fmt(contracts),
            "px": self._fmt(px),
            # 平仓单（reduce_only）的 posSide 标识被平的仓位，与开仓方向相反
            "posSide": (self._pos_side_for_close(side) if reduce_only
                        else self._pos_side(side)),
            "reduceOnly": "true" if reduce_only else "",
        }
        if cl_ord_id:
            payload["clOrdId"] = cl_ord_id
        try:
            resp = self._post(self.trade, "place_order",
                              f"place_maker_limit({inst_id},{side},{contracts}张@{px})",
                              payload, order_resp=True)
        except OKXAPIError as e:
            # post_only 被拒 = 挂单瞬间会吃单（竞争失败），属正常现象，返回 None 让上层按未成交处理
            self.log.info("post_only 挂单被拒（%s %s），按未成交处理", e.code, e.msg)
            return None
        return resp["data"][0]["ordId"]

    def close_position_market(self, inst_id, direction=""):
        """市价平掉指定标的持仓（安全兜底：挂止损失败时立即平仓，不留裸仓位）。

        direction: "long"/"short"，双向持仓模式必须传；净持仓模式留空。
        """
        payload = {"instId": inst_id, "mgnMode": "cross", "autoCxl": "true"}
        if self.get_account_mode()["posMode"] == "long_short_mode":
            if not direction:
                raise OKXAPIError("PARAM", "双向持仓模式下平仓必须指定 direction")
            payload["posSide"] = direction
        resp = self._post(self.trade, "close_positions",
                          f"close_position_market({inst_id})", payload, order_resp=True)
        return resp

    def cancel_order(self, inst_id, ord_id):
        """撤销普通订单。"""
        return self._post(self.trade, "cancel_order", f"cancel_order({ord_id[:8]}…)",
                          {"instId": inst_id, "ordId": str(ord_id)}, order_resp=True)

    def get_order(self, inst_id, ord_id):
        """查询单个订单状态。state: live/partially_filled/filled/canceled。"""
        resp = self._call("get_order", self.trade.get_order, inst_id, ordId=str(ord_id))
        d = resp["data"][0]
        return {
            "instId": d["instId"],
            "ordId": d["ordId"],
            "state": d["state"],
            "side": d["side"],
            "px": float(d["px"]) if d.get("px") else None,
            "sz": float(d["sz"]),
            "acc_fill_sz": float(d.get("accFillSz") or 0),
            "avg_px": float(d["avgPx"]) if d.get("avgPx") else None,
            "raw": d,
        }

    def get_pending_orders(self, inst_id=""):
        """当前挂着的普通订单。"""
        resp = self._call("get_pending_orders", self.trade.get_order_list,
                          instType="SWAP", instId=inst_id)
        return [{
            "instId": d["instId"], "ordId": d["ordId"], "state": d["state"],
            "side": d["side"], "px": d.get("px"), "sz": d.get("sz"),
        } for d in resp.get("data", [])]

    # ────────────────────────── 交易所止损 ──────────────────────────

    def place_stop_loss(self, inst_id, direction, contracts, stop_px, tp_px=None,
                        cl_ord_id=""):
        """挂交易所侧保护单：有 tp_px 时挂 OCO（止盈+止损），否则挂 conditional（纯止损）。

        direction: 持仓方向 "long"/"short"；保护单方向取反（long→sell, short→buy）。
        stop_px:   止损触发价（long 持仓应低于开仓价，short 反之）。
        tp_px:     止盈触发价，可选（来自风控计算的结构位目标）。
        slOrdPx/tpOrdPx="-1": 触发后市价平仓，保证一定出得来。
        cxlOnClosePos: 仓位平掉后自动撤残留保护单，防止变成裸单。
            OKX 官方文档（docs-v5）定义该字段为 Boolean："true" = 仓位平掉时保护单自动撤销。
            （曾经网传的 "cancile" 枚举在现行文档中不存在，勿用。）
            个别网关若仍拒收该字段，则去掉后重试（防御性降级）。
        """
        inst = self.get_instrument(inst_id)
        contracts = self.round_size(contracts, inst["lotSz"], inst["minSz"])
        if contracts <= 0:
            raise OKXAPIError("SIZE", f"保护单张数不足最小单位（minSz={inst['minSz']}）")
        stop_px = self.round_price(stop_px, inst["tickSz"])
        close_side = "sell" if direction == "long" else "buy"
        pos_side = self._pos_side_for_close(close_side)

        payload = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": close_side,
            "ordType": "oco" if tp_px else "conditional",
            "sz": self._fmt(contracts),
            "slTriggerPx": self._fmt(stop_px),
            "slOrdPx": "-1",                 # -1 = 触发后市价平仓
            "reduceOnly": "true",
            "cxlOnClosePos": "true",
        }
        if tp_px:
            payload["tpTriggerPx"] = self._fmt(self.round_price(tp_px, inst["tickSz"]))
            payload["tpOrdPx"] = "-1"
        if pos_side:
            payload["posSide"] = pos_side    # 双向持仓模式必须带（标识被保护的仓位）
        if cl_ord_id:
            payload["algoClOrdId"] = cl_ord_id

        try:
            resp = self._post(self.trade, "place_algo_order",
                              f"place_stop_loss({inst_id},{direction},{contracts}张@{stop_px})",
                              payload, order_resp=True)
        except OKXAPIError as e:
            if "cxlOnClosePos" in payload and e.code in ("51000", "51001"):
                payload.pop("cxlOnClosePos")  # 该字段不被网关支持时降级重试
                self.log.warning("cxlOnClosePos 被拒（%s），去掉后重试", e.code)
                resp = self._post(self.trade, "place_algo_order",
                                  f"place_stop_loss({inst_id},{direction},{contracts}张@{stop_px})",
                                  payload, order_resp=True)
            else:
                raise
        return resp["data"][0]["algoId"]

    def get_pending_stop_losses(self, inst_id=""):
        """挂着的保护单：conditional（纯止损）与 oco（止盈+止损）都要查并合并——
        python-okx 的 order_algos_list 一次只接受一个 ordType，所以是两次调用。
        漏查 oco 会让带止盈的仓位在巡检眼里永远"裸仓"，每轮叠加一张止损单。"""
        out = []
        for ord_type in ("conditional", "oco"):
            resp = self._call("order_algos_list", self.trade.order_algos_list,
                              ordType=ord_type, instType="SWAP", instId=inst_id)
            for d in resp.get("data", []):
                out.append({
                    "instId": d["instId"],
                    "algoId": d["algoId"],
                    "ord_type": ord_type,
                    "state": d.get("algoState", d.get("state", "")),
                    "side": d.get("side"),
                    "sz": d.get("sz"),
                    "sl_trigger_px": (float(d["slTriggerPx"])
                                      if d.get("slTriggerPx") else None),
                    "tp_trigger_px": (float(d["tpTriggerPx"])
                                      if d.get("tpTriggerPx") else None),
                    "cxlOnClosePos": d.get("cxlOnClosePos", ""),
                })
        return out

    def get_algo_order_details(self, algo_id):
        """查询单个策略委托单的当前状态（巡检对账用：交易所查不到时先对账再补挂）。"""
        resp = self._call("get_algo_order_details", self.trade.get_algo_order_details,
                          algoId=str(algo_id))
        return resp["data"][0] if resp.get("data") else None

    def cancel_stop_loss(self, inst_id, algo_id):
        """撤销止损单。cancel_algo_order 的入参是 list[dict]（支持批量），这里单笔。"""
        self.log.info("cancel_stop_loss(%s, algoId=%s)", inst_id, algo_id)
        resp = self._call("cancel_algo_order", self.trade.cancel_algo_order,
                          orders_data=[{"instId": inst_id, "algoId": str(algo_id)}])
        if resp.get("data"):
            item = resp["data"][0]
            if item.get("sCode", "0") != "0":
                raise OKXAPIError(item.get("sCode"), item.get("sMsg", ""), item)
        return resp

    @staticmethod
    def _fmt(x):
        """去浮点尾巴：12.000000000001 → "12"；返回交易所要的字符串。"""
        s = f"{float(x):.10f}".rstrip("0").rstrip(".")
        return s if s else "0"
