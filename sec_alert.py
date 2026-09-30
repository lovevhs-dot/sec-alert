"""SEC filing -> Telegram alert bot.

Telegram commands:
  /add NCT AAPL   add tickers (one or more)
  /remove NCT     remove tickers
  /list           show the watchlist
  /recent NCT     show the 5 latest filings (any ticker; /recent NCT 10 for more)

Run periodically by GitHub Actions: handle commands -> check new filings -> save state.json.
No external packages (Python standard library only).
"""
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = str(os.environ["TG_CHAT_ID"])
UA = os.environ.get("SEC_UA") or "sec-alert bot contact@example.com"  # SEC requires a User-Agent with contact info
STATE = Path("state.json")
SKIP_FORMS = set()  # filing types you don't want. e.g. {"4", "144", "SC 13G/A"}

HELP = "/add TICKER [TICKER...]  add to watchlist\n/remove TICKER  remove from watchlist\n/list  show watchlist\n/recent TICKER [N]  latest N filings (default 5)"


# ---------- HTTP ----------
def sec_json(url):
    time.sleep(0.2)  # SEC limit: 10 requests/second
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
    """ticker -> {'cik_str', 'ticker', 'title'} (None if not found)"""
    global _tickers
    if _tickers is None:
        raw = sec_json("https://www.sec.gov/files/company_tickers.json")
        _tickers = {v["ticker"].upper(): v for v in raw.values()}
    return _tickers.get(ticker.upper())


def recent_filings(cik):
    """Up to 100 most recent filings, newest first."""
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


# ---------- Telegram commands ----------
def handle_commands(state):
    watch = state["watch"]
    for u in tg("getUpdates", offset=state["offset"], timeout=0).get("result", []):
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != CHAT_ID:
            continue  # ignore anyone but you
        parts = (msg.get("text") or "").split()
        if not parts:
            continue
        cmd = parts[0].lower().split("@")[0]
        args = [a.upper() for a in parts[1:]]

        if cmd == "/add" and args:
            for t in args:
                if t in watch:
                    send(f"{t}: already on the watchlist")
                    continue
                info = lookup(t)
                if not info:
                    send(f"{t}: ticker not found on SEC")
                    continue
                filings = recent_filings(info["cik_str"])
                # mark existing filings as seen -> no flood of old alerts right after adding
                watch[t] = {"cik": info["cik_str"], "name": info["title"], "seen": [f["acc"] for f in filings]}
                last = f"\nLatest filing: {filings[0]['form']} ({filings[0]['date']})" if filings else ""
                send(f"✅ Added {t} — {info['title']}{last}")
        elif cmd == "/remove" and args:
            for t in args:
                send(f"🗑 Removed {t}" if watch.pop(t, None) else f"{t}: not on the watchlist")
        elif cmd == "/recent" and args:
            t = args[0]
            n = min(int(args[1]), 20) if len(args) > 1 and args[1].isdigit() else 5
            info = watch.get(t) or lookup(t)
            if not info:
                send(f"{t}: ticker not found on SEC")
                continue
            cik = info.get("cik") or info.get("cik_str")
            name = info.get("name") or info.get("title")
            lines = [f"📋 {t} — {name} (latest {n})"]
            for f in recent_filings(cik)[:n]:
                lines.append(f"\n{f['form']} · {f['date']}\n{filing_url(cik, f)}")
            send("\n".join(lines))
        elif cmd == "/list":
            send("\n".join(f"{t} — {w['name']}" for t, w in sorted(watch.items())) or "Watchlist is empty")
        else:
            send(HELP)


# ---------- Check new filings ----------
def check_filings(state):
    for t, w in state["watch"].items():
        try:
            filings = recent_filings(w["cik"])
        except Exception as e:  # one ticker failing shouldn't stop the rest
            print(f"{t}: {e}")
            continue
        seen = set(w["seen"])
        for f in reversed([f for f in filings if f["acc"] not in seen]):  # send oldest first
            if f["form"] in SKIP_FORMS:
                continue
            desc = f"\n{f['desc']}" if f["desc"] else ""
            send(f"📄 {t} · {f['form']} ({f['date']})\n{w['name']}{desc}\n{filing_url(w['cik'], f)}")
        w["seen"] = [f["acc"] for f in filings]


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {"offset": 0, "watch": {}}
    handle_commands(state)
    check_filings(state)
    # guarantees a commit at least monthly, so GitHub doesn't disable the schedule after 60 idle days
    state["heartbeat"] = time.strftime("%Y-%m")
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
