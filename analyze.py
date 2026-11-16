#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
한미 증시 데일리 브리핑 자동 생성기
- yfinance로 한국/미국 지수·섹터·VIX·환율·금 수집
- 등락률, 변동성, 이동평균, RSI 계산
- Gemini로 흐름·여론·변동성·차수재시실 채점 생성
- docs/data.json 저장 + (선택) 알림

환경변수:
  GEMINI_API_KEY   (필수) Google AI Studio 무료 키
  GEMINI_MODEL     (선택) 우선 사용할 모델명. 비우면 자동 선택
  TELEGRAM_TOKEN / TELEGRAM_CHAT_ID  (선택)
  SMTP_USER / SMTP_PASS / EMAIL_TO   (선택)
"""

import os
import json
import time
import datetime
import math
import urllib.parse
import xml.etree.ElementTree as ET

import yfinance as yf
import pandas as pd
import requests

import screener

# ── 추적 대상 ──────────────────────────────────────────────
TICKERS = {
    "KOSPI":        "^KS11",
    "KOSDAQ":       "^KQ11",
    "삼성전자":      "005930.KS",
    "SK하이닉스":    "000660.KS",
    "S&P500":       "^GSPC",
    "나스닥":        "^IXIC",
    "다우":          "^DJI",
    "반도체(SOXX)":  "SOXX",
    "엔비디아":      "NVDA",
    "VIX(공포지수)": "^VIX",
    "미10년물금리":  "^TNX",
    "원/달러":       "KRW=X",
    "달러인덱스":    "DX-Y.NYB",
    "금(선물)":      "GC=F",
}

ETF_TICKERS = {
    "KODEX200":           "069500.KS",
    "TIGER미국S&P500":    "360750.KS",
    "KODEX반도체":        "091160.KS",
    "QQQ(나스닥100)":     "QQQ",
    "SMH(미반도체)":      "SMH",
    "ARKK(혁신)":         "ARKK",
}

# ════════════════════════════════════════════════════════════
#  ★ 내 보유 종목 — 여기만 고치면 됩니다 ★
#  • 추가: 아래에 "이름": "티커", 한 줄 넣기
#  • 삭제: 해당 줄 지우기
#  • 국내 종목은 코드 뒤에 .KS  (예: 삼성전자 = "005930.KS")
#  • 미국 종목은 티커 그대로       (예: 애플 = "AAPL")
#  수정 후 → Commit → Actions에서 Run workflow 하면 반영됩니다.
# ════════════════════════════════════════════════════════════
HOLDINGS = {
    # ── 국내 ──
    "삼성전자":      "005930.KS",
    "SK하이닉스":    "000660.KS",
    "현대차":        "005380.KS",
    "현대모비스":    "012330.KS",
    "LG이노텍":      "011070.KS",
    # ── 해외 ──
    "애플":          "AAPL",
    "엔비디아":      "NVDA",
    "알파벳":        "GOOGL",
    "아마존":        "AMZN",
    "TSMC":          "TSM",
    "마이크로소프트": "MSFT",
    "테슬라":        "TSLA",
    "스페이스X":     "SPCX",
    "팔란티어":      "PLTR",
    "인텔":          "INTC",
    "넷플릭스":      "NFLX",
    "엑슨모빌":      "XOM",
    "스타벅스":      "SBUX",
}

# ════════════════════════════════════════════════════════════
#  ★ 관심 종목 — 아직 안 샀지만 점검 중인 종목 ★
#  실제로 매수하면 위 HOLDINGS로 옮기고 여기서는 지우세요.
# ════════════════════════════════════════════════════════════
WATCHLIST_TICKERS = {
    # ── 개별주 ──
    "일라이릴리(LLY)":   "LLY",
    "JP모건(JPM)":       "JPM",
    "P&G(PG)":           "PG",
    # ── ETF ──
    "SCHD(미배당)":      "SCHD",
    "SPY(S&P500)":       "SPY",
    "VOO(S&P500)":       "VOO",
    "SOXX(미반도체)":    "SOXX",
    "ITA(미방산)":       "ITA",
}

NEWS_QUERIES = {
    "한국증시": "코스피 OR 코스닥 증시",
    "미국증시": "미국 증시 OR S&P500 OR 나스닥",
    "반도체":   "반도체 OR 엔비디아 OR SK하이닉스",
    "환율·금리": "원달러 환율 OR 미국 금리 OR 연준 OR 국채금리",
    "지정학·유가": "유가 OR 중동 OR 호르무즈 OR 이란 OR 지정학 증시",
}

# 우선 시도할 모델. 종료된 모델이면 자동으로 건너뛰고, 사용 가능한 최신 flash 모델을 찾아 씁니다.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_FALLBACKS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"

OUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "data.json")


# ── 안전 변환 (NaN/inf → None) ─────────────────────────────
def _safe(v, ndigits=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):   # NaN/inf 차단
        return None
    return round(f, ndigits)


def _clean(o):
    """저장 직전 모든 NaN/inf를 None으로 정리 (JSON 호환 보장)."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(x) for x in o]
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    return o


# ── 지표 계산 ──────────────────────────────────────────────
def rsi(series: pd.Series, period: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    val = (100 - (100 / (1 + rs))).dropna()
    return _safe(val.iloc[-1], 1) if len(val) else None


def analyze_ticker(name: str, symbol: str, group: str = "지표") -> dict:
    try:
        df = yf.Ticker(symbol).history(period="3mo", interval="1d")
        close = df["Close"].dropna() if not df.empty else pd.Series(dtype=float)
        if len(close) < 5:
            return {"name": name, "symbol": symbol, "group": group, "error": "no data"}

        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        chg_pct = _safe((last - prev) / prev * 100) if prev else None

        rets = close.pct_change().dropna()
        vol_annual = _safe(float(rets.tail(20).std()) * math.sqrt(252) * 100, 1) if len(rets) else None

        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean()) if len(close) >= 60 else None
        trend = "상승" if (ma60 and ma20 > ma60) else "하락/횡보"

        recent = close.tail(20)
        range_pct = _safe((recent.max() - recent.min()) / recent.min() * 100, 1) if recent.min() else None

        return {
            "name": name,
            "symbol": symbol,
            "group": group,
            "last": _safe(last),
            "chg_pct": chg_pct,
            "vol_annual": vol_annual,
            "range20_pct": range_pct,
            "rsi": rsi(close),
            "ma20": _safe(ma20),
            "trend": trend,
        }
    except Exception as e:
        return {"name": name, "symbol": symbol, "group": group, "error": str(e)}


def fetch_earnings(symbol: str) -> dict:
    """다가오는 실적 발표일(있으면) + 최근 실적 요약. 실패해도 키트는 정상 동작."""
    out = {}
    try:
        tk = yf.Ticker(symbol)
        # 다가오는 발표일
        try:
            cal = tk.calendar
            ed = None
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
            elif cal is not None and hasattr(cal, "loc") and "Earnings Date" in getattr(cal, "index", []):
                ed = cal.loc["Earnings Date"].values
            if ed is not None:
                d = ed[0] if isinstance(ed, (list, tuple)) or hasattr(ed, "__len__") else ed
                ds = str(d)[:10]
                today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date()
                try:
                    edate = datetime.date.fromisoformat(ds)
                    dleft = (edate - today).days
                    if -1 <= dleft <= 60:
                        out["next_earnings"] = ds
                        out["d_left"] = dleft
                except ValueError:
                    pass
        except Exception:
            pass
        # 최근 실적 (분기 손익)
        try:
            fin = tk.quarterly_financials
            if fin is not None and not fin.empty and "Total Revenue" in fin.index:
                rev = fin.loc["Total Revenue"].dropna()
                if len(rev) >= 2:
                    cur, prev = float(rev.iloc[0]), float(rev.iloc[1])
                    if prev:
                        out["rev_qoq_pct"] = round((cur - prev) / abs(prev) * 100, 1)
        except Exception:
            pass
    except Exception:
        pass
    return out


# ── 여론(뉴스) 수집 ────────────────────────────────────────
def fetch_weekly(symbol: str, name: str) -> dict:
    """최근 5거래일 종가 + 일별 등락률. 주간 흐름 차트용."""
    try:
        df = yf.Ticker(symbol).history(period="12d", interval="1d")
        close = df["Close"].dropna()
        if len(close) < 2:
            return None
        recent = close.tail(6)              # 등락률 계산 위해 6일(직전 1일 포함)
        dates, vals, chgs = [], [], []
        prev = None
        for ts, v in recent.items():
            v = float(v)
            if prev is not None:
                dates.append(f"{ts.month}/{ts.day}")
                vals.append(round(v, 2))
                chgs.append(round((v - prev) / prev * 100, 2))
            prev = v
        # 최근 5일만
        return {"name": name, "dates": dates[-5:], "close": vals[-5:], "chg": chgs[-5:]}
    except Exception:
        return None


import re as _re
import html as _html

# 본문 요약을 함께 주는 언론사 직접 RSS. 주소가 바뀌거나 막히면 그 피드만 조용히 건너뜁니다.
EXTRA_FEEDS = [
    ("연합뉴스", "https://www.yna.co.kr/rss/market.xml"),
    ("연합뉴스", "https://www.yna.co.kr/rss/economy.xml"),
    ("한국경제", "https://www.hankyung.com/feed/finance"),
]
# 증시와 직접 관련된 기사만 남기는 키워드 (많이 걸릴수록 우선)
_MARKET_KW = _re.compile(
    r"코스피|코스닥|증시|외국인|외인|기관|순매수|순매도|반도체|삼성전자|하이닉스|엔비디아|나스닥|S&P|뉴욕증시|"
    r"환율|금리|국채|연준|FOMC|유가|실적|영업이익|목표가|사이드카|서킷브레이커|공매도|상장|ETF|수출")


def _is_korean_text(s: str, min_ratio: float = 0.3) -> bool:
    """깨진 바이트·이미지·영문 안내문이 본문으로 들어가는 것을 막는 검사."""
    s = (s or "").strip()
    if len(s) < 40:
        return False
    return len(_re.findall(r"[가-힣]", s)) / len(s) >= min_ratio


def _strip_tags(s: str) -> str:
    s = _html.unescape(_html.unescape(s or ""))
    s = _re.sub(r"(?s)<[^>]+>", " ", s)
    return _re.sub(r"\s+", " ", s).strip()


def fetch_feed_briefs(keep: int = 10, limit: int = 400) -> list:
    """언론사 RSS에서 증시 관련 기사만 골라 제목과 요약문을 반환.
    수급 금액·연속 일수 같은 수치를 LLM에 공급하기 위한 재료."""
    cands, seen = [], set()
    for src, url in EXTRA_FEEDS:
        try:
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                print(f"feed 건너뜀 {src} {url}: HTTP {r.status_code}")
                continue
            root = ET.fromstring(r.content)
            n = 0
            for order, it in enumerate(root.findall(".//item")[:40]):
                title = _strip_tags(it.findtext("title", ""))
                desc = _strip_tags(it.findtext("description", ""))
                if not title or title in seen:
                    continue
                seen.add(title)
                score = len(set(_MARKET_KW.findall(title + " " + desc)))
                if score == 0:
                    continue
                item = {"title": f"{title} - {src}", "source": src}
                has_body = _is_korean_text(desc) and desc != title
                if has_body:
                    item["body"] = desc[:limit]
                cands.append((score + (2 if has_body else 0), -order, item))
                n += 1
            print(f"feed {src} {url.rsplit('/', 1)[-1]}: 관련 기사 {n}건")
        except Exception as e:
            print(f"feed 건너뜀 {src} {url}: {type(e).__name__}")
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [c[2] for c in cands[:keep]]


def fetch_news(per_query: int = 5) -> dict:
    """카테고리별 헤드라인(Google News) + 언론사 RSS 요약."""
    out = {}
    for label, q in NEWS_QUERIES.items():
        try:
            url = ("https://news.google.com/rss/search?q="
                   + urllib.parse.quote(q) + "&hl=ko&gl=KR&ceid=KR:ko")
            xml = requests.get(url, timeout=15,
                               headers={"User-Agent": "Mozilla/5.0"}).text
            root = ET.fromstring(xml)
            heads = []
            for it in root.findall(".//item")[:per_query]:
                title = it.findtext("title", "").strip()
                src = title.rsplit(" - ", 1)[-1] if " - " in title else ""
                heads.append({"title": title, "source": src})
            out[label] = heads
        except Exception as e:
            out[label] = [{"title": f"수집 실패: {e}", "source": ""}]
    briefs = fetch_feed_briefs()
    if briefs:
        out["증권 속보"] = briefs
    return out


# ── 규칙 기반 채점 (같은 데이터면 항상 같은 결과) ──────────
GRADE_RULE = 2          # 등급 계산 방식 버전. 바꾸면 '등급 변화' 알림을 한 번 쉰다.
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "docs", "history.json")
GRADE_NAMES = {          # (상, 중, 하)
    "보유": ("코어유지", "관심·단기", "비중조절 검토"),
    "관심": ("진입 검토", "관심 지속", "관망"),
}


def _region(symbol: str) -> str:
    return "KR" if (symbol or "").endswith((".KS", ".KQ")) else "US"


def market_gate(metrics: dict) -> dict:
    """시(시황)를 지수 위치·추세·VIX로 판정. 지역별(한국/미국)로 따로 본다."""
    vix = (metrics.get("VIX(공포지수)") or {}).get("last")
    out = {}
    for reg, key in (("KR", "KOSPI"), ("US", "S&P500")):
        m = metrics.get(key) or {}
        last, ma20, chg = m.get("last"), m.get("ma20"), m.get("chg_pct")
        if last is None or ma20 is None:
            out[reg] = {"sym": "❓", "why": f"{key} 데이터 없음"}
            continue
        above = last > ma20
        up = m.get("trend") == "상승"
        calm = vix is not None and vix < 20
        pts = int(above) + int(up) + int(calm)
        sym = "✅" if pts == 3 else "⚠️" if pts == 2 else "❌"
        shock = chg is not None and chg <= -3
        if shock:
            sym = "❌"
        why = (f"{key} {'20일선 위' if above else '20일선 아래'}, "
               f"{'상승 추세' if up else '추세 약함'}, "
               f"VIX {vix if vix is not None else '—'}{' (20 미만)' if calm else ' (20 이상)'}"
               + (f", 당일 {chg}% 급락" if shock else ""))
        out[reg] = {"sym": sym, "why": why}
    return out


def chart_score(d: dict) -> str:
    """차(차트)를 20일선 위치·추세·RSI 과열로 판정."""
    if not d or d.get("error") or d.get("last") is None or d.get("ma20") is None:
        return "❓"
    pts = int(d["last"] > d["ma20"]) + int(d.get("trend") == "상승")
    sym = "✅" if pts == 2 else "⚠️" if pts == 1 else "❌"
    if sym == "✅" and (d.get("rsi") or 0) >= 80:
        sym = "⚠️"                                           # 과열
    return sym


def rule_grade(scores: dict, kind: str) -> str:
    """차수재시실 → 등급.

    관심(미보유): 진입 판단이므로 시황 우선 원칙을 그대로 적용한다.
      시 ✅이고 ✅ 4개 이상 → 상 / ✅ 3개 → 중 / ✅ 2개 이하 → 하 / 시 ❌이면 최대 중
    보유: 이미 들고 있는 종목은 시장이 나쁘다는 이유만으로 '하'가 되지 않게 종목 자체 4항목(차·수·재·실)으로 본다.
      ✅ 2점, ⚠️·❓ 1점, ❌ 0점 합계(8점 만점): 6점 이상 → 상 / 4~5점 → 중 / 3점 이하 → 하
      시가 ✅가 아니면 최대 중
    공통: 재료나 수급이 ❌면 한 단계 내린다(교집합 원칙).
    """
    top, mid, low = GRADE_NAMES[kind]
    gate = scores.get("시")
    if kind == "보유":
        pts = sum({"✅": 2, "❌": 0}.get(scores.get(k), 1) for k in ("차", "수", "재", "실"))
        tier = 0 if pts >= 6 else 1 if pts >= 4 else 2
        if gate != "✅":
            tier = max(tier, 1)
    else:
        n_ok = sum(1 for k in ("차", "수", "재", "시", "실") if scores.get(k) == "✅")
        tier = 0 if (n_ok >= 4 and gate == "✅") else 1 if n_ok >= 3 else 2
        if gate == "❌":
            tier = max(tier, 1)
    if scores.get("재") == "❌" or scores.get("수") == "❌":
        tier = min(tier + 1, 2)
    return (top, mid, low)[tier]


def apply_rules(brief: dict, holdings: dict, watchlist: dict, gate: dict):
    """LLM이 준 수·재·실은 살리고, 차·시·등급은 코드 값으로 확정한다."""
    for sec, src, kind in (("holdings_scores", holdings, "보유"),
                           ("watchlist_scores", watchlist, "관심")):
        for it in brief.get(sec) or []:
            d = src.get(it.get("name")) or {}
            sc = dict(it.get("scores") or {})
            for k in ("수", "재", "실"):
                if sc.get(k) not in ("✅", "⚠️", "❌", "❓"):
                    sc[k] = "❓"
            sc["차"] = chart_score(d)
            sc["시"] = gate.get(_region(d.get("symbol", "")), {}).get("sym", "❓")
            it["scores"] = sc
            it["grade"] = rule_grade(sc, kind)
    brief["gate"] = gate
    brief["grade_rule"] = GRADE_RULE


def _sig(g):
    if not g:
        return None
    if ("비중확대" in g) or ("코어유지" in g) or ("진입 검토" in g):
        return "🟢"
    if ("관심" in g) or ("단기" in g):
        return "🟡"
    return "🔴"


def update_history(brief: dict, holdings: dict, watchlist: dict, date_str: str) -> dict:
    """그날의 신호등과 종가를 기록하고, 과거 신호의 이후 수익률을 집계한다."""
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        hist = {"rule": GRADE_RULE, "days": []}
    if hist.get("rule") != GRADE_RULE:                       # 채점 방식이 바뀌면 새로 쌓는다
        hist = {"rule": GRADE_RULE, "days": []}

    items = {}
    for sec, src in (("holdings_scores", holdings), ("watchlist_scores", watchlist)):
        for it in brief.get(sec) or []:
            p = (src.get(it.get("name")) or {}).get("last")
            s = _sig(it.get("grade"))
            if p and s:
                items[it["name"]] = {"s": s, "p": p}
    if items and not brief.get("stale_parts"):
        hist["days"] = [d for d in hist["days"] if d.get("date") != date_str]
        hist["days"].append({"date": date_str, "items": items})
        hist["days"].sort(key=lambda d: d["date"])
        hist["days"] = hist["days"][-400:]
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))

    days = hist["days"]
    track = {"start": days[0]["date"] if days else None, "n_days": len(days), "horizons": {}}
    parse = datetime.date.fromisoformat
    for label, span in (("1주", 7), ("4주", 28)):
        agg = {s: [] for s in ("🟢", "🟡", "🔴")}
        for i, d0 in enumerate(days):
            target = parse(d0["date"]) + datetime.timedelta(days=span)
            d1 = next((d for d in days[i + 1:] if parse(d["date"]) >= target), None)
            if not d1:
                continue
            for name, a in d0["items"].items():
                b = d1["items"].get(name)
                if b and a.get("p") and b.get("p"):
                    agg[a["s"]].append((b["p"] / a["p"] - 1) * 100)
        res = {}
        for s, v in agg.items():
            if v:
                res[s] = {"n": len(v), "avg": round(sum(v) / len(v), 2),
                          "win": round(sum(1 for x in v if x > 0) / len(v) * 100)}
        track["horizons"][label] = res
    return track


# ── LLM 분석 ───────────────────────────────────────────────
ANALYSIS_PROMPT = """너는 신중한 시장 애널리스트다. 아래 한미 증시 지표·뉴스·특징주 정량데이터를 바탕으로
한국어 데일리 브리핑을 작성하라. 과장·단정 금지, 확률·시나리오·근거 중심으로.

지표:
{data}

최신 뉴스 (title=제목, body=기사 본문 발췌 — 있으면 반드시 읽어라):
{news}

뉴스 body에서 '구체 수치'를 추출해 분석에 인용하라: 외인·기관·개인 순매수 금액, 연속 매수/매도 일수,
목표가, 괴리율, 투자 규모, 발동된 사이드카/서킷브레이커 등. 수치 없는 두루뭉술한 서술보다
"외인 N일 연속 순매도 후 전환" 같은 정량 서사를 우선하라. body가 없는 기사는 제목만으로 판단하되 과도한 추정은 금지.

오늘의 특징주 정량 데이터(거래대금 상위, KRX):
{featured}

내 보유 종목 지표:
{holdings}

관심 종목 지표(아직 미보유, 점검 중):
{watchlist}

[종목 채점 프레임 — 차수재시실(차·수·재·시·실)]
각 종목을 5축으로 채점한다. 기호: ✅충족 ⚠️부분 ❌미충족 ❓미확인.
- 차(차트): 가격 위치·캔들·거래대금 회복. 정량 근거 → value_ratio, gap_today, gap_up_cnt, rebound, surge_cnt
- 수(수급): 외인·기관 순매수 전환과 질. (무료 데이터 한계 시 ❓/뉴스 보조)
- 재(재료): 구조적 재료·신규 촉매 여부·Tier. 뉴스로 판단
- 시(시황): 시장 전체 환경. VIX·코스피 과열/안정으로 판단. **데이존·과열이면 ❌**
- 실(실적): 실적 모멘텀·펀더멘털

[등급 산출 규칙 — 시황 우선 원칙]
"시장이 개별 종목을 이긴다." 시(시황)가 ❌면 상단이 구조적으로 제한된다.
- 시✅ 그리고 ✅ 4~5개 → "비중확대"
- ✅ 3개 → "관심·단기"
- 시❌이면 ✅ 개수와 무관하게 최대 "관심·단기(단타·분할 한정)"로 제한
- ✅ 2개 이하 → "관망"
교집합 원칙: 재료·수급 중 하나가 ❌로 무너지면 실적이 강해도 등급을 한 단계 낮춘다.

다음 JSON 형식으로만 응답하라(설명·마크다운 금지):
{{
  "headline": "오늘의 한 줄 요약(25자 내외)",
  "us_summary": "미국 증시 흐름 2~3문장",
  "kr_summary": "한국 증시 흐름 2~3문장",
  "sentiment": "뉴스·여론 심리 1~2문장(긍정/중립/부정 톤 명시)",
  "market_gate": "오늘 시황(시) 채점과 근거 1~2문장. 전 종목 상단 제한 여부 명시",
  "key_issues": ["주요 이슈 1","주요 이슈 2","주요 이슈 3"],
  "volatility_outlook": "오늘 변동성 예측. VIX·변동성·금·환율 근거로 '높음/보통/낮음'과 이유",
  "scenarios": [
    {{"name":"상승 시나리오","prob":"확률%","desc":"조건과 근거","actions":[{{"label":"행동 옵션","type":"offense"}}]}},
    {{"name":"횡보 시나리오","prob":"확률%","desc":"조건과 근거","actions":[{{"label":"행동 옵션","type":"neutral"}}]}},
    {{"name":"하락 시나리오","prob":"확률%","desc":"조건과 근거","actions":[{{"label":"행동 옵션","type":"defense"}}]}}
  ],
  "stock_scores": [
    {{"name":"종목명","scores":{{"차":"✅","수":"⚠️","재":"✅","시":"❌","실":"❓"}},"grade":"관심·단기","comment":"채점 근거 1~2문장","caution":"리스크 1문장"}}
  ],
  "holdings_scores": [
    {{"name":"보유종목명","scores":{{"차":"✅","수":"⚠️","재":"✅","시":"❌","실":"✅"}},"grade":"코어유지/관심·단기/비중조절 검토","comment":"보유 관점 채점 근거 1~2문장","caution":"점검 포인트 1문장"}}
  ],
  "watchlist_scores": [
    {{"name":"관심종목명","scores":{{"차":"✅","수":"⚠️","재":"✅","시":"❌","실":"✅"}},"grade":"진입 검토/관심 지속/관망","comment":"미보유 관점 채점 근거 1~2문장","caution":"진입 전 점검 포인트 1문장"}}
  ],
  "watchlist": ["주목 종목·섹터와 이유 2~3개"],
  "theme_plays": [
    {{"trigger":"감지된 호재·테마(예: HBM 수요 급증)","chain":"밸류체인 흐름(예: 메모리→후공정 장비→소재→전력기기)","picks":[
      {{"rank":1,"name":"종목명","role":"밸류체인 내 위치","scores":{{"차":"✅","수":"⚠️","재":"✅","시":"❌","실":"✅"}},"reason":"이 종목이 더 유력한 비교 근거 1~2문장","caution":"리스크 1문장"}}
    ]}}
  ],
  "triggers": [
    {{"cond":"전환 확인 조건(예: 외인 코스피 순매수 N거래일 연속, VKOSPI 30 이하, 원/달러 1,500 하향 안정, 미·이란 종전 공식화 등)","status":"✅ 또는 ⚠️ 또는 ❌ 또는 ❓","note":"현재 지표값·근거"}}
  ],
  "risk_cards": [
    {{"type":"warning","title":"추격매매 위험","body":"위꼬리·장대음봉·과열 등 차트/시황 경고 1~2문장"}},
    {{"type":"contrarian","title":"역발상 사례","body":"실적이 강해도 재료·수급이 무너지면 붕괴한다는 교훈·사례 1~2문장"}},
    {{"type":"opportunity","title":"기회 자리","body":"악재 소멸 후 반등 가능 조건과 선행 신호 1~2문장"}}
  ]
}}
risk_cards는 오늘 시황·뉴스에 근거해 생성. type은 warning/contrarian/opportunity.
scenarios의 각 시나리오에 actions 배열로 '일반적 대응 옵션' 2~3개를 제시하라. 이는 추천이 아니라 사용자가 자기 계획으로 '선택'할 후보다.
- label은 8자 내외 행동 문구(예: "신규 매수 중단", "현금 비중 확대", "관망 유지", "분할 진입 검토", "레버리지 축소").
- type은 defense(방어)/neutral(중립)/offense(공격) 중 하나. 해당 시나리오에 부합하는 순서로 정렬하라.
- 특정 종목명·수량·가격을 넣지 말고 포트폴리오 수준의 행동 범주만. "추천"·"해야 한다" 같은 지시 표현 금지.

triggers는 시장 '전환 초입'을 확인하는 관찰 조건 4~6개. 반드시 다음 형식을 지켜라:
- cond는 [대상] + [정량 조건] + [기간/횟수] 구조로 구체적으로. 나쁜 예: "외인 수급 개선". 좋은 예: "외국인 코스피 현물 순매수 2거래일 연속 (직전 13거래일 연속 매도의 전환 확인)".
- note에는 현재 실측값·근거를 수치로 (예: "VIX 22.4 / 기준 20 이하", "원/달러 1,383 / 기준 1,350 하향").
- 4~6개 중 최소 1~2개는 시장 지표가 아닌 '개별 종목 단위' 트리거로: 보유·관심·테마 종목 중 오늘 뉴스에 등장한 종목의 확인 조건(예: "삼성전기 — 외인·기관 동반 순매수 + 전고점 241.7만원 거래량 동반 돌파").
- VIX·원/달러·달러인덱스·금리는 제공된 지표값으로 자동 판정. 수급·지정학은 뉴스 body 수치로 판단. status는 ✅충족 ⚠️부분 ❌미충족 ❓미확인.
stock_scores는 특징주 데이터에서 등급이 높은 순으로 최대 4개. 확률 3개 합은 100%.
holdings_scores는 '내 보유 종목' 전체를 차수재시실로 채점한다.
holdings_scores와 watchlist_scores의 comment는 5축 중 핵심 2~3개 축을 '왜 그렇게 채점했는지' 구체적 근거로 3~4문장 작성하고, 마지막에 '오늘의 포인트'를 한 가지 짚어라. 포인트는 가능하면 정량 조건으로(나쁜 예: "수급을 지켜볼 것" / 좋은 예: "외인 순매수 2일 연속 여부와 21만원 지지 확인").
watchlist_scores는 '관심 종목' 전체를 차수재시실로 채점한다.
ETF(SCHD·SPY·VOO·SOXX·ITA 등)는 개별주와 달리 재료·실적 축을 '추종 지수·섹터 추세'로 해석해 채점하고, comment에 분산상품 특성과 어떤 섹터·지수에 노출되는지 명시하라.
지표에 next_earnings(다가오는 실적 발표일)·d_left(남은 일수)·rev_qoq_pct(최근 분기 매출 증감)가 있으면 '실' 채점과 comment에 반영하라.
실적 발표가 임박(d_left 7 이하)하면 caution에 '실적 발표 D-N 대기'를 명시하라.
시황 게이트는 보유·관심 종목에도 동일 적용. 레버리지·테마성 종목은 caution에 구조적 리스크를 명시.
미10년물금리·금·달러인덱스 동향을 매크로로 해석하라: 금리 상승은 성장주(반도체·테크)에 부담, 하락은 우호.
금이 오르며 증시가 약하면 위험회피 국면이다. 이 해석을 volatility_outlook과 key_issues에 반영하라.
theme_plays는 오늘 뉴스·호재에서 포착되는 테마 1~2개에 대해, 밸류체인 수혜 종목을 rank 순위로 제시한다.
밸류체인은 대장주에서 멈추지 말고 후방까지 깊게 전개하라: 완제품·대장주 → 핵심 부품/장비 → 소재 → 전력·인프라 → 후공정/유통.
[picks 선정 강제 규칙 — 반드시 지켜라]
1) 누구나 아는 초대형 대장주(삼성전자·SK하이닉스·TSMC·엔비디아·인텔·마이크로소프트·애플 등)는 picks에 '최대 1개'만 허용한다. 0개여도 좋다.
2) ETF·지수추종 상품(KODEX·SMH·SOXX·QQQ 등)은 picks에 절대 넣지 마라. ETF는 별도 섹션에서 다룬다.
3) 아래 보유/관심 종목과 겹치는 종목은 picks에서 제외하라(발굴 가치를 위해): {holdings_names}, {watchlist_names}.
4) picks의 대부분(최소 3개 이상)은 '덜 알려졌지만 수혜가 직접적인 중소형·중견 종목'으로 채워라.
예: 'AI 반도체 호황'이면 HBM 후공정 장비(한미반도체·이오테크닉스), 기판·소재(이수페타시스·해성디에스·티엘비), 테스트(리노공업), 데이터센터 전력기기(LS ELECTRIC·HD현대일렉트릭·제룡전기), 냉각(GST) 등 구체적 중소형 수혜주를 발굴해 비교하라.
'방산 호황'이면 한화에어로스페이스·현대로템·LIG넥스원·한국항공우주, 그 후방의 부품·소재주를 발굴하라.
국내 종목을 우선하되 글로벌 중소형 수혜주도 1~2개 섞어도 된다. picks는 테마당 3~5개.
각 종목을 차수재시실로 채점하고 reason에 '밸류체인 내 위치와 왜 이 종목의 수혜 강도가 더 직접적인지'를 비교로 쓴다. role에는 밸류체인 단계(예: HBM 후공정 장비)를 명시하라.
각 pick의 reason 마지막에 "확인 트리거: ..." 한 문장을 붙여라 — 진입 검토 전 확인할 정량 조건 1개(예: "확인 트리거: 마이크론向 공급계약 공시 발생", "확인 트리거: 외인 순매수 전환 + 거래대금 평균 2배"). 조건이 확인되기 전에는 '아이디어 단계'임을 전제한다.
단, 매수 시점·진입 타이밍·'지금 사라'는 절대 쓰지 마라. 목표가도 단정하지 마라. 순위와 비교 근거까지만 제시하고 결정은 사용자 몫이다.
모든 종목 표기는 매수 권유가 아니라 채점·분석 결과임을 전제로 한다.
rev_qoq_pct는 '직전 분기 대비' 매출 증감이다. 전년 동기 대비로 서술하지 마라.

[이번 호출의 과제]
{task}"""

MARKET_KEYS = ["headline", "us_summary", "kr_summary", "sentiment", "market_gate", "key_issues",
               "volatility_outlook", "scenarios", "stock_scores", "watchlist", "theme_plays",
               "triggers", "risk_cards"]
STOCK_KEYS = ["holdings_scores", "watchlist_scores"]

TASK_MARKET = ("위 JSON 형식 중 다음 키만 포함해 응답하라: " + ", ".join(MARKET_KEYS) +
               ". holdings_scores와 watchlist_scores는 이번 응답에서 제외한다.")
TASK_STOCKS = ("위 JSON 형식 중 holdings_scores와 watchlist_scores 두 키만 포함해 응답하라. "
               "보유 종목과 관심 종목을 하나도 빠뜨리지 말고 전부 채점하라.\n"
               "차(차트)·시(시황)와 grade는 코드가 규칙으로 확정한다. 아래 확정값을 scores에 그대로 넣고, "
               "comment와 caution은 이 값과 모순되지 않게 써라. grade에는 빈 문자열을 넣고 comment에 등급 이름을 쓰지 마라.\n"
               "시황 판정: {gate}\n종목별 차·시 확정값: {fixed}\n"
               "네가 판단할 것은 수·재·실 세 항목이다. 직전 채점은 다음과 같다: {prev}\n"
               "오늘 지표·뉴스에 바꿀 근거가 없으면 직전 값을 유지하고, 바꿨다면 comment에 그 근거를 한 문장으로 밝혀라. "
               "외인·기관 수급 수치가 뉴스에 없으면 수는 ❓로 둔다.")


_WORKING_MODEL = None


def _discover_models(api_key: str) -> list:
    """현재 키로 쓸 수 있는 flash 계열 모델을 최신 순으로 반환. 실패하면 빈 목록."""
    try:
        r = requests.get(f"{GEMINI_API}/models", params={"pageSize": 200},
                         headers={"x-goog-api-key": api_key}, timeout=20)
        found = []
        skip = ("lite", "image", "tts", "live", "audio", "preview", "exp", "8b", "native", "embedding")
        for m in r.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in name or any(k in name for k in skip):
                continue
            mv = _re.search(r"gemini-(\d+(?:\.\d+)?)", name)
            found.append((float(mv.group(1)) if mv else 0.0, name))
        return [n for _, n in sorted(found, reverse=True)]
    except Exception:
        return []


def _gemini_json(prompt: str, api_key: str) -> dict:
    """Gemini REST 호출 → JSON dict. 실패하면 {"error": 사유}."""
    global _WORKING_MODEL
    if _WORKING_MODEL:
        models = [_WORKING_MODEL]
    else:
        models = []
        for m in [GEMINI_MODEL] + _discover_models(api_key) + GEMINI_FALLBACKS:
            if m and m not in models:
                models.append(m)

    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}}
    last_err = "모델 없음"
    for model in models[:6]:
        for attempt in range(3):
            try:
                r = requests.post(f"{GEMINI_API}/models/{model}:generateContent",
                                  headers={"x-goog-api-key": api_key}, json=body, timeout=180)
                if r.status_code in (400, 403, 404):
                    last_err = f"{model}: HTTP {r.status_code} {r.text[:160]}"
                    print("모델 건너뜀 →", last_err)
                    break                                   # 다음 모델로
                if r.status_code in (429, 500, 503):
                    last_err = f"{model}: HTTP {r.status_code} {r.text[:160]}"
                    if r.status_code == 429 and ("limit: 0" in r.text or "PerDay" in r.text):
                        print("무료 한도 없음 또는 일일 한도 소진 →", model)
                        break                               # 기다려도 소용없음, 다음 모델로
                    if attempt == 2:
                        break
                    wait = 30 * (attempt + 1)
                    print(f"{model} 일시 오류({r.status_code}), {wait}s 대기 후 재시도")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                cand = (r.json().get("candidates") or [{}])[0]
                if cand.get("finishReason") == "MAX_TOKENS":
                    last_err = f"{model}: 응답이 길이 제한으로 잘림"
                    continue
                text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()
                if text.startswith("```"):
                    text = text.split("```")[1].lstrip("json").strip()
                out = json.loads(text)
                if not isinstance(out, dict):
                    raise ValueError("JSON 최상위가 객체가 아님")
                _WORKING_MODEL = model
                out["_model"] = model
                return out
            except Exception as e:
                last_err = f"{model}: {type(e).__name__} {str(e)[:160]}"
                time.sleep(5)
    return {"error": last_err}


def run_llm(data: dict, news: dict, featured: dict, holdings: dict, watchlist: dict,
            prev_brief: dict = None, now_str: str = "", gate: dict = None) -> dict:
    """시장 분석과 종목 채점을 두 번에 나눠 호출. 한쪽이 실패하면 직전 분석을 유지한다."""
    prev_brief = prev_brief or {}
    gate = gate or {}
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"headline": "LLM 키 없음", "error": "GEMINI_API_KEY 미설정"}

    def build(task: str) -> str:
        return ANALYSIS_PROMPT.format(
            data=json.dumps(data, ensure_ascii=False),
            news=json.dumps(news, ensure_ascii=False),
            featured=json.dumps(featured, ensure_ascii=False),
            holdings=json.dumps(holdings, ensure_ascii=False),
            watchlist=json.dumps(watchlist, ensure_ascii=False),
            holdings_names=", ".join(HOLDINGS.keys()),
            watchlist_names=", ".join(WATCHLIST_TICKERS.keys()),
            task=task)

    brief, stale, errors = {}, [], []

    def merge(res: dict, keys: list, label: str, at_key: str, ok_key: str):
        if "error" not in res and res.get(ok_key):
            for k in keys:
                if k in res:
                    brief[k] = res[k]
            brief[at_key] = now_str
            brief["model"] = res.get("_model", "")
        else:
            errors.append(f"{label}: {res.get('error', '필수 항목 누락')}")
            kept = False
            for k in keys:
                if k in prev_brief:
                    brief[k] = prev_brief[k]
                    kept = True
            if kept:
                stale.append(label)
                brief[at_key] = prev_brief.get(at_key, "")

    task_market = TASK_MARKET + (" 시(시황)는 코드가 이미 판정했다: " + json.dumps(gate, ensure_ascii=False) +
                                 " market_gate 문장은 한국과 미국의 이 판정과 근거를 풀어 쓰고, 다른 판정을 내리지 마라.")
    if not (featured or {}).get("items"):
        task_market += " 특징주 정량 데이터가 비어 있으므로 stock_scores는 빈 배열 []로 응답하라."
    market = _gemini_json(build(task_market), api_key)
    merge(market, MARKET_KEYS, "시장 분석", "market_at", "headline")

    if "error" in market:                                    # 서비스 자체가 막힌 상태면 두 번째 호출 생략
        stocks = {"error": "시장 분석 실패로 생략"}
    else:
        time.sleep(8)                                        # 분당 한도 여유
        fixed, prev = {}, {}
        for src in (holdings, watchlist):
            for name, d in src.items():
                fixed[name] = {"차": chart_score(d),
                               "시": gate.get(_region(d.get("symbol", "")), {}).get("sym", "❓")}
        for sec in STOCK_KEYS:
            for it in prev_brief.get(sec) or []:
                sc = it.get("scores") or {}
                prev[it.get("name")] = {k: sc.get(k, "❓") for k in ("수", "재", "실")}
        stocks = _gemini_json(build(TASK_STOCKS.format(
            gate=json.dumps(gate, ensure_ascii=False),
            fixed=json.dumps(fixed, ensure_ascii=False),
            prev=json.dumps(prev, ensure_ascii=False) if prev else "없음")), api_key)
    merge(stocks, STOCK_KEYS, "종목 채점", "stocks_at", "holdings_scores")

    if stale:
        brief["stale_parts"] = stale
    if errors:
        brief["error"] = " / ".join(errors)
        print("LLM 오류:", brief["error"])
    if not brief.get("headline"):
        brief["headline"] = "분석 일시 실패"
    return brief


# ── 알림 ───────────────────────────────────────────────────
def notify_telegram(brief: dict):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    msg = (f"📊 {brief.get('headline','')}\n\n"
           f"🇺🇸 {brief.get('us_summary','')}\n"
           f"🇰🇷 {brief.get('kr_summary','')}\n\n"
           f"⚡ 변동성: {brief.get('volatility_outlook','')}")
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=15)
    except Exception as e:
        print("telegram error:", e)


def notify_email(brief: dict):
    import smtplib
    from email.mime.text import MIMEText
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    if not (user and pw):
        return
    to = os.environ.get("EMAIL_TO", user)
    issues = "\n".join(f"• {i}" for i in brief.get("key_issues", []))
    body = (f"[오늘의 한미 증시 브리핑]\n\n"
            f"■ {brief.get('headline','')}\n\n"
            f"🇺🇸 미국: {brief.get('us_summary','')}\n\n"
            f"🇰🇷 한국: {brief.get('kr_summary','')}\n\n"
            f"🗣 여론: {brief.get('sentiment','')}\n\n"
            f"⚡ 변동성: {brief.get('volatility_outlook','')}\n\n"
            f"주요 이슈:\n{issues}\n\n"
            f"※ 정보 제공용이며 투자 권유가 아닙니다.")
    m = MIMEText(body, "plain", "utf-8")
    m["Subject"] = f"📊 {brief.get('headline','증시 브리핑')}"
    m["From"], m["To"] = user, to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw)
            s.sendmail(user, [to], m.as_string())
    except Exception as e:
        print("email error:", e)


# ── 메인 ───────────────────────────────────────────────────
def main():
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(kst)

    metrics = {name: analyze_ticker(name, sym, "지표") for name, sym in TICKERS.items()}
    metrics.update({name: analyze_ticker(name, sym, "ETF") for name, sym in ETF_TICKERS.items()})

    weekly = {
        "코스피":  fetch_weekly("^KS11", "코스피"),
        "나스닥":  fetch_weekly("^IXIC", "나스닥"),
        "S&P500": fetch_weekly("^GSPC", "S&P500"),
    }
    news = fetch_news()
    try:
        featured = screener.featured_stocks(top=8)
    except Exception as e:
        featured = {"date": None, "items": [], "error": f"screener 오류: {e}"}
    holdings = {}
    for name, sym in HOLDINGS.items():
        d = analyze_ticker(name, sym, "보유")
        d.update(fetch_earnings(sym))
        holdings[name] = d

    watchlist_stocks = {}
    for name, sym in WATCHLIST_TICKERS.items():
        d = analyze_ticker(name, sym, "관심")
        d.update(fetch_earnings(sym))
        watchlist_stocks[name] = d

    prev_data = {}
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            prev_data = json.load(f)
    except Exception:
        pass
    prev_brief = prev_data.get("brief") or {}
    if not prev_brief.get("market_at"):                      # 구버전 data.json 호환
        prev_brief.setdefault("market_at", prev_data.get("updated_at", ""))
        prev_brief.setdefault("stocks_at", prev_data.get("updated_at", ""))
    if prev_brief.get("headline") in ("분석 일시 실패", "LLM 키 없음"):
        prev_brief = {}

    gate = market_gate(metrics)
    brief = run_llm(metrics, news, featured, holdings, watchlist_stocks,
                    prev_brief=prev_brief, now_str=now.strftime("%Y-%m-%d %H:%M KST"), gate=gate)
    apply_rules(brief, holdings, watchlist_stocks, gate)
    track = update_history(brief, holdings, watchlist_stocks, now.strftime("%Y-%m-%d"))

    # A. 등급 변화 감지 (직전 data.json과 신호등 비교)
    prev_grades = {}
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            _old = json.load(f)
        for sec in ("holdings_scores", "watchlist_scores"):
            for it in (_old.get("brief") or {}).get(sec) or []:
                if it.get("name"):
                    prev_grades[it["name"]] = it.get("grade", "")
    except Exception:
        pass

    grade_changes = []
    same_rule = (prev_data.get("brief") or {}).get("grade_rule") == GRADE_RULE
    for sec in (("holdings_scores", "watchlist_scores") if same_rule else ()):
        for it in brief.get(sec) or []:
            n = it.get("name")
            og, ng = prev_grades.get(n), it.get("grade", "")
            if og and ng and _sig(og) != _sig(ng):
                grade_changes.append({"name": n, "from": _sig(og), "to": _sig(ng),
                                      "from_grade": og, "to_grade": ng})

    out = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "metrics": metrics,
        "weekly": weekly,
        "news": news,
        "featured": featured,
        "holdings": holdings,
        "watchlist_stocks": watchlist_stocks,
        "brief": brief,
        "grade_changes": grade_changes,
        "track": track,
        "disclaimer": "본 자료는 정보 제공·참고용이며 투자 권유가 아닙니다. 모든 투자 판단과 책임은 본인에게 있습니다.",
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(_clean(out), f, ensure_ascii=False, indent=2, allow_nan=False)
    print("saved:", OUT_PATH)

    if "시장 분석" not in brief.get("stale_parts", []) and brief.get("us_summary"):
        notify_telegram(brief)
        notify_email(brief)


if __name__ == "__main__":
    main()
