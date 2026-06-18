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
}

GEMINI_MODEL = "gemini-2.5-flash"   # 현역 무료 모델 (필요시 gemini-2.5-flash-lite)

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
                today = datetime.date.today()
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
def fetch_news(per_query: int = 4) -> dict:
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
    return out


# ── LLM 분석 ───────────────────────────────────────────────
ANALYSIS_PROMPT = """너는 신중한 시장 애널리스트다. 아래 한미 증시 지표·뉴스·특징주 정량데이터를 바탕으로
한국어 데일리 브리핑을 작성하라. 과장·단정 금지, 확률·시나리오·근거 중심으로.

지표:
{data}

최신 뉴스 헤드라인:
{news}

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
    {{"name":"상승 시나리오","prob":"확률%","desc":"조건과 근거"}},
    {{"name":"횡보 시나리오","prob":"확률%","desc":"조건과 근거"}},
    {{"name":"하락 시나리오","prob":"확률%","desc":"조건과 근거"}}
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
triggers는 시장 '전환 초입'을 확인하는 관찰 조건 4~5개. VIX·원/달러·달러인덱스 등 지표값으로 자동 판정하고,
지정학·만기 같은 항목은 뉴스로 판단하라. status는 ✅충족 ⚠️부분 ❌미충족 ❓미확인.
stock_scores는 특징주 데이터에서 등급이 높은 순으로 최대 4개. 확률 3개 합은 100%.
holdings_scores는 '내 보유 종목' 전체를 차수재시실로 채점. 등급은 보유 관점(코어유지/관심·단기/비중조절 검토)으로.
holdings_scores와 watchlist_scores의 comment는 5축 중 핵심 2~3개 축을 '왜 그렇게 채점했는지' 구체적 근거로 3~4문장 작성하고, 마지막에 '오늘의 포인트'를 한 가지 짚어라(예: 어떤 지표·뉴스를 지켜볼지).
watchlist_scores는 '관심 종목' 전체를 차수재시실로 채점. 등급은 미보유 관점(진입 검토/관심 지속/관망)으로.
ETF(SCHD·SPY·VOO·SOXX·ITA 등)는 개별주와 달리 재료·실적 축을 '추종 지수·섹터 추세'로 해석해 채점하고, comment에 분산상품 특성과 어떤 섹터·지수에 노출되는지 명시하라.
지표에 next_earnings(다가오는 실적 발표일)·d_left(남은 일수)·rev_qoq_pct(최근 분기 매출 증감)가 있으면 '실' 채점과 comment에 반영하라.
실적 발표가 임박(d_left 7 이하)하면 caution에 '실적 발표 D-N 대기'를 명시하라.
시황 게이트는 보유·관심 종목에도 동일 적용. 레버리지·테마성 종목은 caution에 구조적 리스크를 명시.
미10년물금리·금·달러인덱스 동향을 매크로로 해석하라: 금리 상승은 성장주(반도체·테크)에 부담, 하락은 우호.
금이 오르며 증시가 약하면 위험회피 국면이다. 이 해석을 volatility_outlook과 key_issues에 반영하라.
theme_plays는 오늘 뉴스·호재에서 포착되는 테마 1~2개에 대해, 밸류체인 수혜 종목을 rank 순위로 제시한다.
각 종목을 차수재시실로 채점하고 reason에 '왜 이 종목이 더 유력한지' 비교 근거를 쓴다. picks는 테마당 2~4개.
단, 매수 시점·진입 타이밍·'지금 사라'는 절대 쓰지 마라. 목표가도 단정하지 마라. 순위와 비교 근거까지만 제시하고 결정은 사용자 몫이다.
모든 종목 표기는 매수 권유가 아니라 채점·분석 결과임을 전제로 한다."""


def run_llm(data: dict, news: dict, featured: dict, holdings: dict, watchlist: dict) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"headline": "LLM 키 없음", "error": "GEMINI_API_KEY 미설정"}

    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)
    prompt = ANALYSIS_PROMPT.format(
        data=json.dumps(data, ensure_ascii=False),
        news=json.dumps(news, ensure_ascii=False),
        featured=json.dumps(featured, ensure_ascii=False),
        holdings=json.dumps(holdings, ensure_ascii=False),
        watchlist=json.dumps(watchlist, ensure_ascii=False))

    # 한도(429) 등 일시 오류 시 자동 재시도
    last_err = None
    for attempt in range(3):
        try:
            resp = model.generate_content(prompt)
            text = resp.text.strip()
            if text.startswith("```"):
                text = text.split("```")[1].lstrip("json").strip()
            return json.loads(text)
        except Exception as e:
            last_err = e
            msg = str(e)
            if "429" in msg or "quota" in msg.lower() or "rate" in msg.lower():
                wait = 40 * (attempt + 1)
                print(f"rate limit, {wait}s 대기 후 재시도...")
                time.sleep(wait)
                continue
            # JSON 파싱 실패 등은 1회만 재시도
            if attempt == 0:
                time.sleep(5)
                continue
            break
    return {"headline": "분석 일시 실패", "error": str(last_err)}


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

    brief = run_llm(metrics, news, featured, holdings, watchlist_stocks)

    out = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "metrics": metrics,
        "news": news,
        "featured": featured,
        "holdings": holdings,
        "watchlist_stocks": watchlist_stocks,
        "brief": brief,
        "disclaimer": "본 자료는 정보 제공·참고용이며 투자 권유가 아닙니다. 모든 투자 판단과 책임은 본인에게 있습니다.",
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(_clean(out), f, ensure_ascii=False, indent=2, allow_nan=False)
    print("saved:", OUT_PATH)

    notify_telegram(brief)
    notify_email(brief)


if __name__ == "__main__":
    main()
