# 한미 증시 데일리 브리핑 (완전 무료 자동화)

매일 정해진 시간에 한국·미국 증시를 자동 분석하고, 변동성 예측·시나리오·여론을
대시보드에 갱신하는 키트입니다.

**비용: 0원** (GitHub Actions + Gemini 무료 티어 + GitHub Pages)

---

## 작동 구조

```
매일 cron 발동 (GitHub Actions)
  → yfinance로 한미 지수·반도체·VIX·환율·달러인덱스·금·ETF 수집
  → 지표 계산(등락률·변동성·RSI·이동평균)
  → Google News RSS로 한미 증시 여론(헤드라인) 수집
  → pykrx로 거래대금 상위 특징주 추출 + 종목선정 9기준 정량 계산
  → Gemini가 흐름/여론/변동성 + 특징주 9기준 종합 진단
  → docs/data.json 저장·커밋 → 대시보드 자동 갱신
```

---

## 설치 (4단계, 약 7분) — 알림 없이 가장 단순하게

### 1. 저장소 만들기
이 폴더 전체를 본인 GitHub 계정에 새 저장소로 올립니다(public 권장 — Actions 완전 무료).

### 2. Gemini API 키 발급 + 등록
1. https://aistudio.google.com/apikey → **Create API key** → 키 복사
2. 저장소 → **Settings → Secrets and variables → Actions → New repository secret**
3. 이름 `GEMINI_API_KEY`, 값에 복사한 키 → Save

### 3. GitHub Pages 켜기
저장소 → **Settings → Pages** → Source: `Deploy from a branch`,
Branch: `main` / 폴더: `/docs` → Save.
잠시 후 `https://<아이디>.github.io/<저장소명>/` 에서 대시보드가 열립니다.

### 4. 첫 실행
저장소 → **Actions → Daily Stock Briefing → Run workflow** (수동 실행)으로 즉시 테스트.
이후 매주 평일 한국시간 07:00 자동 실행됩니다.

> 끝입니다. 알림 설정은 필요 없습니다.

---

## 운영: 알림 없이 쓰는 법 (권장)

- **휴대폰 홈화면에 추가** → 앱처럼 사용
  - 아이폰 Safari: 공유 → '홈 화면에 추가'
  - 안드로이드 Chrome: 메뉴(⋮) → '홈 화면에 추가'
  - 매일 아침 아이콘 한 번 탭하면 그날 브리핑이 떠 있습니다.
- **갱신은 자동** — GitHub Actions가 정해진 시간에 알아서 데이터를 채웁니다.
- **고장 알림은 자동** — 워크플로가 실패하면 GitHub이 가입 이메일로 자동 통지하므로,
  별도 설정 없이도 "잘 돌고 있는지" 알 수 있습니다.

---

## (선택) 푸시 알림을 원한다면

### 방법 A — 이메일 (텔레그램보다 친숙)
Gmail 기준. Secrets에 아래 3개 추가:

| 이름 | 값 |
|---|---|
| `SMTP_USER` | 보내는 Gmail 주소 |
| `SMTP_PASS` | Gmail **앱 비밀번호** (아래) |
| `EMAIL_TO` | 받는 주소 (생략 시 본인에게) |

앱 비밀번호 발급: Google 계정 → 보안 → 2단계 인증 켜기 → '앱 비밀번호' 생성 → 16자리 복사.
(일반 Gmail 비밀번호로는 안 되며, 반드시 앱 비밀번호여야 합니다.)

### 방법 B — 텔레그램
Secrets에 `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` 추가.
1. @BotFather → `/newbot` → 토큰
2. 봇에게 메시지 전송 후 `https://api.telegram.org/bot<토큰>/getUpdates` 에서 `chat.id` 확인

> 두 방법 모두 **선택**입니다. 시크릿을 등록하지 않으면 알림은 자동으로 비활성화됩니다.

---

## 실행 시간 바꾸기

`.github/workflows/daily-briefing.yml` 의 cron 수정 (UTC 기준, 한국=UTC+9):

| 원하는 한국 시간 | cron |
|---|---|
| 평일 07:00 (기본) | `0 22 * * 1-5` |
| 평일 08:30 | `30 23 * * 1-5` |
| 평일 16:00 (한국 장 마감 후) | `0 7 * * 1-5` |

> GitHub Actions cron은 수 분~십수 분 지연될 수 있습니다(무료 정책).

---

## 종목선정 9기준 진단 (특징주 자동)

매일 KRX 거래대금 상위 종목을 자동 추출해 9기준으로 진단합니다.

| 기준 | 처리 방식 |
|---|---|
| 2 거래대금 · 6 갭상승 · 7 음봉소화/반등 · 9 과거력 · 3 하방경직 | pykrx 데이터로 **자동 정량 계산** |
| 1 선발성 · 4 재료독점 · 8 테마지배력 | 뉴스 맥락으로 **LLM 정성 판단** |
| 5 수급(스마트머니) | 무료 데이터 한계 — 정성 보조 |

> KRX 서버 접근이 실패하면 특징주 진단은 건너뛰고 뉴스 기반 분석으로 자동 대체됩니다.
> 모든 종목 표기는 매수 권유가 아니라 기준 기반 진단입니다.

## 종목/ETF 추가·변경

`analyze.py` 상단의 `TICKERS`(지표)와 `ETF_TICKERS`(ETF) 딕셔너리 수정.
티커는 Yahoo Finance 표기 (예: 코스피 `^KS11`, 카카오 `035720.KS`, 애플 `AAPL`, `TQQQ`).

## Claude API로 바꾸기 (선택, 소액 유료)

`analyze.py` 의 `run_llm()` 하단 주석을 해제하고 Gemini 부분을 주석 처리.
Secret에 `ANTHROPIC_API_KEY` 추가. Haiku 모델 기준 1회 실행 비용은 수 원 이하.

---

## ⚠️ 면책

본 자료는 정보 제공·참고용이며 투자 권유가 아닙니다.
LLM 분석과 변동성 예측은 빗나갈 수 있으며, 모든 투자 판단과 책임은 본인에게 있습니다.
