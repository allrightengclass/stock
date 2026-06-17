#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
한미 증시 데일리 브리핑 자동 생성기
- yfinance로 한국/미국 지수·섹터·VIX·환율 수집
- 등락률, 변동성(ATR/표준편차), 이동평균, RSI 계산
- Gemini로 흐름·이슈 분석 및 변동성 시나리오 생성
- docs/data.json 저장 + (선택) 텔레그램 알림

환경변수:
  GEMINI_API_KEY   (필수) Google AI Studio 무료 키
  TELEGRAM_TOKEN   (선택) 봇 토큰
  TELEGRAM_CHAT_ID (선택) 채팅 ID
"""

import os
import json
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
    # 한국
    "KOSPI":        "^KS11",
    "KOSDAQ":       "^KQ11",
    "삼성전자":      "005930.KS",
    "SK하이닉스":    "000660.KS",
    # 미국
    "S&P500":       "^GSPC",
    "나스닥":        "^IXIC",
    "다우":          "^DJI",
    "반도체(SOXX)":  "SOXX",
    "엔비디아":      "NVDA",
    # 위험·환율·원자재
    "VIX(공포지수)": "^VIX",
    "원/달러":       "KRW=X",
    "달러인덱스":    "DX-Y.NYB",
    "금(선물)":      "GC=F",
}

# ETF (별도 섹션으로 표시)
ETF_TICKERS = {
    "KODEX200":           "069500.KS",
    "TIGER미국S&P500":    "360750.KS",
    "KODEX반도체":        "091160.KS",
    "QQQ(나스닥100)":     "QQQ",
    "SMH(미반도체)":      "SMH",
    "ARKK(혁신)":         "ARKK",
}

# 내 보유 종목 (코어 — 자유롭게 추가/제거)
HOLDINGS = {
    # 국내
    "삼성전자":      "005930.KS",
    "현대모비스":    "012330.KS",
    "LG이노텍":      "011070.KS",
    # 해외 코어
    "애플":          "AAPL",
    "엔비디아":      "NVDA",
    "알파벳":        "GOOGL",
    "아마존":        "AMZN",
    "TSMC":          "TSM",
    "마이크로소프트": "MSFT",
}

# 뉴스 검색어 (Google News RSS, 무료·키 불필요)
NEWS_QUERIES = {
    "한국증시": "코스피 OR 코스닥 증시",
    "미국증시": "미국 증시 OR S&P500 OR 나스닥",
    "반도체":   "반도체 OR 엔비디아 OR SK하이닉스",
}

OUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "data.json")


# ── 지표 계산 ──────────────────────────────────────────────
def rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    val = 100 - (100 / (1 + rs))
    last = val.dropna()
    return round(float(last.iloc[-1]), 1) if len(last) else None


def analyze_ticker(name: str, symbol: str, group: str = "지표") -> dict:
    try:
        df = yf.Ticker(symbol).history(period="3mo", interval="1d")
        if df.empty or len(df) < 5:
            return {"name": name, "symbol": symbol, "group": group, "error": "no data"}

        close = df["Close"]
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        chg_pct = round((last - prev) / prev * 100, 2)

        # 일간 수익률 표준편차(연율화) → 변동성 지표
        rets = close.pct_change().dropna()
        vol_daily = float(rets.tail(20).std())
        vol_annual = round(vol_daily * math.sqrt(252) * 100, 1)

        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean()) if len(close) >= 60 else None
        trend = "상승" if (ma60 and ma20 > ma60) else "하락/횡보"

        # 최근 20일 변동폭
        recent = close.tail(20)
        range_pct = round((recent.max() - recent.min()) / recent.min() * 100, 1)

        return {
            "name": name,
            "symbol": symbol,
            "group": group,
            "last": round(last, 2),
            "chg_pct": chg_pct,
            "vol_annual": vol_annual,   # 연율화 변동성(%)
            "range20_pct": range_pct,   # 20일 변동폭(%)
            "rsi": rsi(close),
            "ma20": round(ma20, 2),
            "trend": trend,
        }
    except Exception as e:
        return {"name": name, "symbol": symbol, "group": group, "error": str(e)}


# ── 여론(뉴스) 수집 ────────────────────────────────────────
def fetch_news(per_query: int = 4) -> dict:
    """Google News RSS로 한미 증시 헤드라인 수집 (무료, 키 불필요)."""
    out = {}
    for label, q in NEWS_QUERIES.items():
        try:
            url = ("https://news.google.com/rss/search?q="
                   + urllib.parse.quote(q)
                   + "&hl=ko&gl=KR&ceid=KR:ko")
            xml = requests.get(url, timeout=15,
                               headers={"User-Agent": "Mozilla/5.0"}).text
            root = ET.fromstring(xml)
            items = root.findall(".//item")[:per_query]
            heads = []
            for it in items:
                title = it.findtext("title", "").strip()
                # "기사제목 - 언론사" 형태에서 매체명 분리
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
  "watchlist": ["주목 종목·섹터와 이유 2~3개"],
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
시황 게이트는 보유 종목에도 동일 적용. 레버리지·테마성 종목은 caution에 구조적 리스크를 명시.
모든 종목 표기는 매수 권유가 아니라 채점 결과임을 전제로 한다."""


def run_llm(data: dict, news: dict, featured: dict, holdings: dict) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"headline": "LLM 키 없음", "error": "GEMINI_API_KEY 미설정"}

    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")  # 무료 티어
    prompt = ANALYSIS_PROMPT.format(
        data=json.dumps(data, ensure_ascii=False),
        news=json.dumps(news, ensure_ascii=False),
        featured=json.dumps(featured, ensure_ascii=False),
        holdings=json.dumps(holdings, ensure_ascii=False))
    resp = model.generate_content(prompt)
    text = resp.text.strip()
    # 코드펜스 제거
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip()
    try:
        return json.loads(text)
    except Exception:
        return {"headline": "분석 파싱 실패", "raw": text}

    # ── Claude API로 바꾸려면 위를 주석 처리하고 아래 사용 ──
    # import anthropic
    # client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    # msg = client.messages.create(
    #     model="claude-haiku-4-5-20251001", max_tokens=1500,
    #     messages=[{"role": "user", "content": prompt}])
    # return json.loads(msg.content[0].text)


# ── 알림 ───────────────────────────────────────────────────
def notify_telegram(brief: dict):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    msg = (
        f"📊 {brief.get('headline','')}\n\n"
        f"🇺🇸 {brief.get('us_summary','')}\n"
        f"🇰🇷 {brief.get('kr_summary','')}\n\n"
        f"⚡ 변동성: {brief.get('volatility_outlook','')}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": msg}, timeout=15)
    except Exception as e:
        print("telegram error:", e)


def notify_email(brief: dict):
    """Gmail SMTP로 이메일 발송 (선택). 안 쓰면 환경변수 비워두면 됨.
    SMTP_USER: 보내는 Gmail 주소
    SMTP_PASS: Gmail '앱 비밀번호'(2단계 인증 후 발급)
    EMAIL_TO : 받는 주소(미지정 시 SMTP_USER로)"""
    import smtplib
    from email.mime.text import MIMEText

    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    if not (user and pw):
        return
    to = os.environ.get("EMAIL_TO", user)

    issues = "\n".join(f"• {i}" for i in brief.get("key_issues", []))
    body = (
        f"[오늘의 한미 증시 브리핑]\n\n"
        f"■ {brief.get('headline','')}\n\n"
        f"🇺🇸 미국: {brief.get('us_summary','')}\n\n"
        f"🇰🇷 한국: {brief.get('kr_summary','')}\n\n"
        f"🗣 여론: {brief.get('sentiment','')}\n\n"
        f"⚡ 변동성: {brief.get('volatility_outlook','')}\n\n"
        f"주요 이슈:\n{issues}\n\n"
        f"※ 정보 제공용이며 투자 권유가 아닙니다."
    )
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
    featured = screener.featured_stocks(top=8)
    holdings = {name: analyze_ticker(name, sym, "보유") for name, sym in HOLDINGS.items()}
    brief = run_llm(metrics, news, featured, holdings)

    out = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "metrics": metrics,
        "news": news,
        "featured": featured,
        "holdings": holdings,
        "brief": brief,
        "disclaimer": "본 자료는 정보 제공·참고용이며 투자 권유가 아닙니다. 모든 투자 판단과 책임은 본인에게 있습니다.",
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved:", OUT_PATH)

    notify_telegram(brief)
    notify_email(brief)


if __name__ == "__main__":
    main()
