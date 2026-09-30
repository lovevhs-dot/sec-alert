"""SEC filing -> Telegram alert bot.

Telegram commands:
  /add NCT AAPL     add tickers (one or more)
  /remove NCT       remove tickers
  /removeall        clear the whole watchlist
  /list             show the watchlist
  /recent NCT [N]   latest N filings with what each one is about (default 5)

Run periodically by GitHub Actions: handle commands -> check new filings -> save state.json.
No external packages (Python standard library only).
"""
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN = os.environ["TG_TOKEN"].strip()
CHAT_ID = os.environ["TG_CHAT_ID"].strip()
UA = (os.environ.get("SEC_UA") or "").strip() or "sec-alert bot contact@example.com"  # SEC requires a User-Agent with contact info
STATE = Path("state.json")
SKIP_FORMS = set()  # filing types you don't want alerts for. e.g. {"4", "144"}
MENU_VERSION = 2  # bump when COMMANDS change so Telegram's menu gets refreshed

HELP = (
    "Commands:\n"
    "/add TICKER [TICKER...] — add to watchlist\n"
    "/remove TICKER [TICKER...] — remove from watchlist\n"
    "/removeall — clear the watchlist\n"
    "/list — show watchlist\n"
    "/recent TICKER [N] — latest N filings (default 5, max 20)"
)
COMMANDS = [
    {"command": "add", "description": "Add tickers: /add NCT AAPL"},
    {"command": "remove", "description": "Remove tickers: /remove NCT"},
    {"command": "removeall", "description": "Clear the whole watchlist"},
    {"command": "list", "description": "Show watchlist"},
    {"command": "recent", "description": "Latest filings: /recent NCT 5"},
]

FORM_NAMES = {
    "10-K": "Annual report", "10-Q": "Quarterly report", "8-K": "Current report",
    "20-F": "Annual report (foreign issuer)", "40-F": "Annual report (Canadian issuer)",
    "6-K": "Current report (foreign issuer)",
    "S-1": "Registration statement", "F-1": "Registration statement (foreign issuer)",
    "S-3": "Shelf registration", "F-3": "Shelf registration (foreign issuer)",
    "S-4": "Merger registration", "F-4": "Merger registration (foreign issuer)",
    "S-8": "Employee stock plan registration", "EFFECT": "Registration declared effective",
    "3": "Initial insider ownership", "4": "Insider transaction", "5": "Annual insider ownership",
    "144": "Notice of proposed insider sale",
    "SC 13D": "5%+ ownership stake (active)", "SC 13G": "5%+ ownership stake (passive)",
    "SCHEDULE 13D": "5%+ ownership stake (active)", "SCHEDULE 13G": "5%+ ownership stake (passive)",
    "DEF 14A": "Proxy statement (shareholder meeting)", "PRE 14A": "Preliminary proxy statement",
    "25-NSE": "Delisting notice", "NT 10-K": "Late annual report notice",
    "NT 10-Q": "Late quarterly report notice", "NT 20-F": "Late annual report notice",
    "CORRESP": "Company letter to SEC", "UPLOAD": "SEC letter to company", "D": "Private placement notice",
}
ITEMS_8K = {
    "1.01": "Material agreement", "1.02": "Agreement terminated", "1.03": "Bankruptcy",
    "1.05": "Cybersecurity incident", "2.01": "Acquisition or sale of assets",
    "2.02": "Earnings / financial results", "2.03": "New debt or obligation",
    "2.04": "Debt acceleration", "2.05": "Restructuring costs", "2.06": "Impairment",
    "3.01": "Delisting / listing rule notice", "3.02": "Unregistered share sale",
    "3.03": "Change to shareholder rights", "4.01": "Auditor change",
    "4.02": "Prior financials unreliable", "5.01": "Change in control",
    "5.02": "Director / officer change", "5.03": "Bylaws or fiscal year change",
    "5.07": "Shareholder vote results", "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
}


# ---------- HTTP ----------
def sec_get(url):
    time.sleep(0.2)  # SEC limit: 10 requests/second
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def sec_json(url):
    return json.loads(sec_get(url))


def tg(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=30) as r:
        return json.load(r)


def send(text):
    # Telegram limit is 4096 characters per message -> split on blank lines
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > 3800:
            chunks.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    chunks.append(cur)
    for c in chunks:
        tg("sendMessage", chat_id=CHAT_ID, text=c[:4000], disable_web_page_preview="true")


# ---------- SEC lookups ----------
_tickers = None


def lookup(ticker):
    """ticker -> {'cik', 'name', 'exchange'} (None if not found)"""
    global _tickers
    if _tickers is None:
        _tickers = {}
        try:
            raw = sec_json("https://www.sec.gov/files/company_tickers_exchange.json")
            for cik, name, tk, exch in raw["data"]:
                _tickers[tk.upper()] = {"cik": cik, "name": name, "exchange": exch or "?"}
        except Exception as e:
            print(f"exchange list failed: {e}")
        if not _tickers:  # fallback list without exchange names
            raw = sec_json("https://www.sec.gov/files/company_tickers.json")
            for v in raw.values():
                _tickers[v["ticker"].upper()] = {"cik": v["cik_str"], "name": v["title"], "exchange": "?"}
    return _tickers.get(ticker.upper())


def recent_filings(cik, n=100):
    """Up to n most recent filings, newest first."""
    r = sec_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")["filings"]["recent"]
    items = r.get("items") or [""] * len(r["accessionNumber"])
    return [
        {
            "acc": acc,
            "form": r["form"][i],
            "date": r["filingDate"][i],
            "doc": r["primaryDocument"][i],
            "desc": r["primaryDocDescription"][i],
            "items": items[i],
        }
        for i, acc in enumerate(r["accessionNumber"][:n])
    ]


def folder_url(cik, f):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{f['acc'].replace('-', '')}/"


def filing_url(cik, f):
    return folder_url(cik, f) + (f["doc"] or "")


# ---------- What is this filing about? ----------
def html_lines(raw):
    """Turn an SEC HTML document into a list of clean text lines."""
    h = raw.decode("utf-8", "replace")
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"\s+", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table|center|title)>", "\n", h)
    h = re.sub(r"(?i)</t[dh]>", "  ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html.unescape(h).replace("\xa0", " ")
    return [l for l in (re.sub(r"\s+", " ", x).strip() for x in h.split("\n")) if l]


def clip(s, n=220):
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def form_label(form):
    base, amend = (form[:-2], " (amendment)") if form.endswith("/A") else (form, "")
    name = FORM_NAMES.get(base) or ("Prospectus / offering terms" if base.startswith("424B") else "")
    return name + amend if name else ""


def exhibit_headline(cik, f):
    """Headline of the first Exhibit 99 document (usually the press release)."""
    idx = sec_json(folder_url(cik, f) + "index.json")["directory"]["item"]
    names = [i["name"] for i in idx if re.search(r"ex-?_?99", i["name"], re.I) and i["name"].lower().endswith((".htm", ".html"))]
    if not names:
        return ""
    for line in html_lines(sec_get(folder_url(cik, f) + sorted(names)[0]))[:15]:
        if len(line) >= 15 and not re.match(r"(?i)(exhibit|ex-?99|press release$|for immediate release)", line):
            return line
    return ""


def summarize(cik, f):
    """A few short lines saying what the filing is about."""
    out = []
    base = f["form"].replace("/A", "")
    try:
        if base == "6-K":
            lines = html_lines(sec_get(filing_url(cik, f)))
            start = next((i + 1 for i, l in enumerate(lines) if re.search(r"(?i)information contained in this", l)), None)
            if start is None:
                start = max((i + 1 for i, l in enumerate(lines) if "40-F" in l), default=0)
            end = next((i for i in range(start, len(lines)) if re.match(r"(?i)(exhibit index|signatures?\b)", lines[i])), len(lines))
            body = lines[start:end]
            exhibits = []
            ex_start = next((i for i, l in enumerate(lines) if re.match(r"(?i)exhibit index", l)), None)
            if ex_start is not None:
                for l in lines[ex_start + 1:]:
                    if re.match(r"(?i)signatures?\b", l):
                        break
                    m = re.match(r"(\d{1,3}\.\d{1,2})\s+(.{8,})", l)
                    if m:
                        exhibits.append((m.group(1), m.group(2)))
            mentions_pr = any("press release" in x.lower() for x in body + [d for _, d in exhibits])
            headline = exhibit_headline(cik, f) if mentions_pr or not body else ""
            if headline:
                out.append("📰 " + clip(headline))
            elif exhibits:
                out += [f"• {clip(d, 160)}" for _, d in exhibits[:3]]
            if body and not headline:
                out.insert(0, clip(" — ".join(body[:2])))
        elif base == "8-K":
            codes = [c.strip() for c in (f.get("items") or "").split(",") if c.strip() and c.strip() != "9.01"]
            if codes:
                out.append("Items: " + "; ".join(ITEMS_8K.get(c, c) for c in codes))
            headline = exhibit_headline(cik, f)
            if headline:
                out.append("📰 " + clip(headline))
    except Exception as e:
        print(f"summary failed for {f['acc']}: {e}")
    if not out and f.get("desc") and f["desc"].upper() not in (f["form"].upper(), base.upper()):
        out.append(clip(f["desc"]))
    return out


def filing_block(cik, f, number=None):
    head = f"{f['form']} · {f['date']}"
    label = form_label(f["form"])
    if label:
        head += f" · {label}"
    if number is not None:
        head = f"{number}) {head}"
    return "\n".join([head] + summarize(cik, f))


# ---------- Telegram commands ----------
def handle_commands(state):
    watch = state["watch"]
    if state.get("menu_version") != MENU_VERSION:  # refresh Telegram's command menu
        tg("setMyCommands", commands=json.dumps(COMMANDS))
        state["menu_version"] = MENU_VERSION
    updates = tg("getUpdates", offset=state["offset"], timeout=0).get("result", [])
    print(f"{len(updates)} new Telegram message(s)")
    for u in updates:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat = str(msg.get("chat", {}).get("id"))
        print(f"  from chat {chat}: {msg.get('text')!r}")
        if chat != CHAT_ID:
            print(f"  ignored: chat {chat} does not match TG_CHAT_ID secret")
            continue  # ignore anyone but you
        parts = (msg.get("text") or "").split()
        if not parts:
            continue
        cmd = parts[0].lower().split("@")[0]
        args = [a.upper() for a in parts[1:]]
        try:
            run_command(watch, cmd, args)
        except Exception as e:
            send(f"⚠️ Error while running {cmd}: {e}")


def run_command(watch, cmd, args):
    if cmd == "/add" and args:
        lines = []
        for t in args:
            if t in watch:
                lines.append(f"• {t}: already on your watchlist")
                continue
            info = lookup(t)
            if not info:
                lines.append(f"• {t}: not found on SEC — check the ticker")
                continue
            filings = recent_filings(info["cik"])
            # mark existing filings as seen -> no flood of old alerts right after adding
            watch[t] = {**info, "seen": [f["acc"] for f in filings]}
            last = f"\n   Latest filing: {filings[0]['form']} ({filings[0]['date']})" if filings else ""
            lines.append(f"• {t} · {info['exchange']} — {info['name']}{last}")
        send("➕ Add to watchlist\n\n" + "\n".join(lines) + f"\n\nWatching {len(watch)} ticker(s). You'll get a message when any of them files something new.")

    elif cmd == "/removeall" or (cmd == "/remove" and args == ["ALL"]):
        if not watch:
            send("🗑 Watchlist is already empty.")
        else:
            names = ", ".join(sorted(watch))
            watch.clear()
            send(f"🗑 Removed all tickers: {names}\n\nWatchlist is now empty. No more alerts until you /add again.")

    elif cmd == "/remove" and args:
        lines = [f"• {t}: removed" if watch.pop(t, None) else f"• {t}: wasn't on your watchlist" for t in args]
        send("➖ Remove from watchlist\n\n" + "\n".join(lines) + f"\n\nWatching {len(watch)} ticker(s).")

    elif cmd == "/list":
        if not watch:
            send("📋 Your watchlist is empty.\n\nAdd tickers with /add NCT AAPL")
        else:
            rows = [f"{t} · {w.get('exchange', '?')}\n{w['name']}" for t, w in sorted(watch.items())]
            send(f"📋 Your watchlist — {len(watch)} ticker(s)\nYou'll get an alert when any of these files something new.\n\n" + "\n\n".join(rows))

    elif cmd == "/recent" and args:
        t = args[0]
        n = min(int(args[1]), 20) if len(args) > 1 and args[1].isdigit() else 5
        info = watch.get(t) or lookup(t)
        if not info:
            send(f"{t}: not found on SEC — check the ticker")
            return
        blocks = [filing_block(info["cik"], f, i + 1) for i, f in enumerate(recent_filings(info["cik"], n))]
        send(f"🗂 Latest {n} filings — {t} ({info['name']})\n\n" + "\n\n".join(blocks))

    else:
        send(HELP)


# ---------- Check new filings ----------
def check_filings(state):
    for t, w in state["watch"].items():
        try:
            if "exchange" not in w:  # older entries: fill in the exchange once
                w["exchange"] = (lookup(t) or {}).get("exchange", "?")
            filings = recent_filings(w["cik"])
        except Exception as e:  # one ticker failing shouldn't stop the rest
            print(f"{t}: {e}")
            continue
        seen = set(w["seen"])
        for f in reversed([f for f in filings if f["acc"] not in seen]):  # send oldest first
            if f["form"] in SKIP_FORMS:
                continue
            send(f"🔔 New SEC filing — {t} ({w['name']})\n\n{filing_block(w['cik'], f)}\n\n{filing_url(w['cik'], f)}")
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
