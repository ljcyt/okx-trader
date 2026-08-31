# -*- coding: utf-8 -*-
"""回测历史 K 线抓取 + 本地缓存。

缓存目录：okx_trader/backtest/cache/<inst>_<bar>.jsonl，一行一根 K 线，
按 ts 升序、去重。二次运行只补缺口，不重抓。抓取失败必须抛异常，
绝不静默返回空——本项目历史上有过静默空转的事故。
"""
import json
import os


def _cache_path(cache_dir, inst_id, bar):
    safe = inst_id.replace("/", "_").replace(":", "_")
    return os.path.join(cache_dir, f"{safe}_{bar}.jsonl")


def _load(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _save(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def fetch_history(client, inst_id, bar, start_ms, end_ms, cache_dir=None):
    """抓取 [start_ms, end_ms] 区间的 K 线，与本地缓存合并后返回升序列表。

    返回 [{"ts","open","high","low","close","vol"}, ...]（ts 升序、去重）。
    分页从 end_ms 向更旧翻，直到覆盖 start_ms 或交易所无更旧数据。
    """
    if cache_dir is None:
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "cache")
    path = _cache_path(cache_dir, inst_id, bar)
    cached = {r["ts"]: r for r in _load(path)}

    # 已缓存区间若覆盖请求区间，直接返回
    if cached:
        have_min = min(cached)
        have_max = max(cached)
        if have_min <= start_ms and have_max >= end_ms:
            return [cached[t] for t in sorted(cached)
                    if start_ms <= t <= end_ms]

    fetched = {}
    after = end_ms
    # 向更旧翻：OKX after 返回严格早于该 ts 的记录
    while True:
        rows = client.get_candles(inst_id, bar=bar, limit=300, after=after)
        if not rows:
            break
        for r in rows:
            fetched[r["ts"]] = r
        oldest = min(r["ts"] for r in rows)
        if oldest <= start_ms:
            break
        if oldest == after:  # 无进展（去重后同一批）
            break
        after = oldest

    if not fetched:
        raise RuntimeError(
            f"{inst_id} {bar} 抓取失败：{start_ms}~{end_ms} 区间无数据"
            f"（检查网络或时间范围）")

    merged = dict(cached)
    merged.update(fetched)
    _save(path, [merged[t] for t in sorted(merged)])
    return [merged[t] for t in sorted(merged)
            if start_ms <= t <= end_ms]
