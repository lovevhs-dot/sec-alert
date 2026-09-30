"""SEC 공시 → 텔레그램 알림 봇.

텔레그램 명령:
  /add NCT AAPL   종목 추가 (여러 개 가능)
  /remove NCT     종목 삭제
  /list           현재 목록

GitHub Actions가 주기적으로 실행: 명령 처리 → 새 공시 확인 → state.json 저장.
외부 패키지 없음 (파이썬 표준 라이브러리만 사용).
"""
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = str(os.environ["TG_CHAT_ID"])
UA = os.environ.get("SEC_UA") or "sec-alert bot contact@example.com"  # SEC는 연락처가 담긴 User-Agent 요구
STATE = Path("state.json")
SKIP_FORMS = set()  # 받기 싫은 공시 유형. 예: {"4", "144", "SC 13G/A"}

HELP = "/add 티커 [티커...]  종목 추가\n/remove 티커  종목 삭제\n/list  목록 보기"


# ---------- HTTP ----------
def sec_json(url):
    time.sleep(0.2)  # SEC 제한: 초당 10회
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def tg(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=30) as r:
        return json.load(r)


def send(text):
    tg("sendMessage", chat_id=CHAT_ID, text=text, disable_web_page_preview="true")


# ---------- SEC ----------
_tickers = None


def lookup(ticker):
    """티커 → {'cik_str', 'ticker', 'title'} (없으면 None)"""
    global _tickers
    if _tickers is None:
        raw = sec_json("https://www.sec.gov/files/company_tickers.json")
        _tickers = {v["ticker"].upper(): v for v in raw.values()}
    return _tickers.get(ticker.upper())


def recent_filings(cik):
    """최근 공시 최대 100건, 최신순."""
    r = sec_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")["filings"]["recent"]
    return [
        {
            "acc": acc,
            "form": r["form"][i],
            "date": r["filingDate"][i],
            "doc": r["primaryDocument"][i],
            "desc": r["primaryDocDescription"][i],
        }
        for i, acc in enumerate(r["accessionNumber"][:100])
    ]


def filing_url(cik, f):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{f['acc'].replace('-', '')}/{f['doc'] or ''}"


# ---------- 텔레그램 명령 처리 ----------
def handle_commands(state):
    watch = state["watch"]
    for u in tg("getUpdates", offset=state["offset"], timeout=0).get("result", []):
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != CHAT_ID:
            continue  # 본인 채팅 외 무시
        parts = (msg.get("text") or "").split()
        if not parts:
            continue
        cmd = parts[0].lower().split("@")[0]
        args = [a.upper() for a in parts[1:]]

        if cmd == "/add" and args:
            for t in args:
                if t in watch:
                    send(f"{t}: 이미 목록에 있음")
                    continue
                info = lookup(t)
                if not info:
                    send(f"{t}: SEC에서 티커를 못 찾음")
                    continue
                filings = recent_filings(info["cik_str"])
                # 기존 공시는 '본 것'으로 처리 → 추가 직후 알림 폭탄 방지
                watch[t] = {"cik": info["cik_str"], "name": info["title"], "seen": [f["acc"] for f in filings]}
                last = f"\n최근 공시: {filings[0]['form']} ({filings[0]['date']})" if filings else ""
                send(f"✅ {t} 추가 — {info['title']}{last}")
        elif cmd == "/remove" and args:
            for t in args:
                send(f"🗑 {t} 삭제" if watch.pop(t, None) else f"{t}: 목록에 없음")
        elif cmd == "/list":
            send("\n".join(f"{t} — {w['name']}" for t, w in sorted(watch.items())) or "목록 비어 있음")
        else:
            send(HELP)


# ---------- 새 공시 확인 ----------
def check_filings(state):
    for t, w in state["watch"].items():
        try:
            filings = recent_filings(w["cik"])
        except Exception as e:  # 한 종목 실패해도 나머지는 계속
            print(f"{t}: {e}")
            continue
        seen = set(w["seen"])
        for f in reversed([f for f in filings if f["acc"] not in seen]):  # 오래된 것부터 전송
            if f["form"] in SKIP_FORMS:
                continue
            desc = f"\n{f['desc']}" if f["desc"] else ""
            send(f"📄 {t} · {f['form']} ({f['date']})\n{w['name']}{desc}\n{filing_url(w['cik'], f)}")
        w["seen"] = [f["acc"] for f in filings]


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {"offset": 0, "watch": {}}
    handle_commands(state)
    check_filings(state)
    # 한 달에 한 번은 커밋이 생기게 해서 GitHub의 '60일 비활성 시 스케줄 중지' 방지
    state["heartbeat"] = time.strftime("%Y-%m")
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
