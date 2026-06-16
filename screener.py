#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
특징주 자동 추출 + 종목선정 9기준 정량 계산 (pykrx 기반)
KRX 접근 실패 시 빈 결과 반환 → 상위 로직에서 뉴스 기반으로 폴백.

정량화 매핑:
  2 거래대금   → 당일 거래대금, 20일 평균 대비 배수
  3 하방경직성 → 음봉 평균낙폭 / 변동성 (작을수록 경직)
  6 갭상승의 힘 → 최근 갭업 횟수·당일 갭
  7 음봉 소화력 → 음봉 다음날 반등 비율
  9 과거력     → 최근 60거래일 일간 +15% 이상 급등 횟수
  1·4·5·8      → LLM 정성 판단(뉴스 활용)
"""

import datetime


def _ohlcv_all(day: str):
    from pykrx import stock
    return stock.get_market_ohlcv(day, market="ALL")


def _recent_trading_day(max_back: int = 7):
    """최근 거래일(YYYYMMDD)과 전종목 OHLCV 반환."""
    for back in range(max_back):
        d = (datetime.date.today() - datetime.timedelta(days=back)).strftime("%Y%m%d")
        try:
            df = _ohlcv_all(d)
        except Exception:
            continue
        if df is not None and not df.empty and df["거래대금"].sum() > 0:
            return d, df
    return None, None


SKIP = ("스팩", "리츠")


def _is_common(name: str) -> bool:
    if any(s in name for s in SKIP):
        return False
    if name.endswith("우") or name.endswith("우B") or "우(전환)" in name:
        return False
    return True


def score_nine(ticker: str, name: str, end: str) -> dict:
    """단일 종목 9기준 정량 지표."""
    from pykrx import stock
    start = (datetime.datetime.strptime(end, "%Y%m%d")
             - datetime.timedelta(days=120)).strftime("%Y%m%d")
    df = stock.get_market_ohlcv(start, end, ticker)
    if df is None or df.empty or len(df) < 10:
        return {"ticker": ticker, "name": name, "error": "no data"}

    o, c = df["시가"], df["종가"]
    val, chg = df["거래대금"], df["등락률"]

    value_last = float(val.iloc[-1])
    value_ratio = round(value_last / max(val.tail(20).mean(), 1), 2)
    last_chg = round(float(chg.iloc[-1]), 2)

    # 갭: 당일 시가 vs 전일 종가
    gap_today = round((o.iloc[-1] - c.iloc[-2]) / c.iloc[-2] * 100, 2)
    gap_up_cnt = int(((o.values[1:] - c.values[:-1]) / c.values[:-1] > 0.02).sum())

    # 과거 급등 횟수 (+15%↑)
    surge_cnt = int((chg >= 15).sum())

    # 음봉 소화/반등: 음봉 다음날 양봉 비율
    body = (c - o)
    down = body < 0
    nxt_up = ((down.shift(1).fillna(False)) & (body > 0)).sum()
    rebound = round(float(nxt_up) / max(int(down.sum()), 1), 2)

    # 하방경직성: 음봉 평균낙폭(절대) / 일간변동성 (작을수록 경직)
    rets = c.pct_change().dropna()
    down_avg = abs(rets[rets < 0].mean()) if (rets < 0).any() else 0
    vol = rets.std() if len(rets) else 1
    rigidity = round(float(down_avg / max(vol, 1e-9)), 2)

    return {
        "ticker": ticker,
        "name": name,
        "chg_pct": last_chg,
        "value_eok": round(value_last / 1e8, 1),      # 거래대금(억원)
        "value_ratio": value_ratio,                    # 평소 대비 배수
        "gap_today": gap_today,
        "gap_up_cnt": gap_up_cnt,
        "surge_cnt": surge_cnt,
        "rebound": rebound,
        "rigidity": rigidity,
    }


def featured_stocks(top: int = 8) -> dict:
    """거래대금 상위에서 특징주 후보 추출 + 9기준 정량."""
    try:
        from pykrx import stock  # noqa
    except Exception as e:
        return {"date": None, "items": [], "error": f"pykrx 미설치: {e}"}

    date, df = _recent_trading_day()
    if df is None:
        return {"date": None, "items": [], "error": "KRX 거래일 데이터 없음"}

    df = df[df["거래대금"] > 0].sort_values("거래대금", ascending=False)
    items = []
    for tkr, _ in df.head(top * 4).iterrows():
        try:
            from pykrx import stock
            name = stock.get_market_ticker_name(tkr)
            if not _is_common(name):
                continue
            s = score_nine(tkr, name, date)
            if "error" not in s:
                items.append(s)
        except Exception:
            continue
        if len(items) >= top:
            break
    return {"date": date, "items": items}


if __name__ == "__main__":
    import json
    print(json.dumps(featured_stocks(5), ensure_ascii=False, indent=2))
