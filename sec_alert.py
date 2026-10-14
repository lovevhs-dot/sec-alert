"""SEC filing -> Telegram alert bot.

Telegram commands:
  /add NVDA AAPL     add tickers (one or more)
  /remove NVDA       remove tickers
  /removeall        clear the whole watchlist
  /list             show the watchlist
  /recent NVDA [N]   latest N filings with what each one is about (default 5)
  /dividends NVDA    dividend history from SEC financial data (per share + total paid)
  /dilution NVDA 260102 [260703]   dilution-related filings between two dates (YYMMDD; no end = today)
  /delisting NVDA    delisting status: warned / scheduled / delisted / fixed, from SEC filings

Alerts: new SEC filings and trading halts (Nasdaq halt feed, all US exchanges) for your watchlist.
Filings that mention a dividend are marked 💰.

Run periodically by GitHub Actions: handle commands -> check filings -> check halts -> save state.json.
No external packages (Python standard library only).
"""
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

TOKEN = os.environ["TG_TOKEN"].strip()
CHAT_ID = os.environ["TG_CHAT_ID"].strip()
UA = (os.environ.get("SEC_UA") or "").strip() or "sec-alert bot contact@example.com"  # SEC requires a User-Agent with contact info
STATE = Path("state.json")
SKIP_FORMS = set()  # filing types you don't want alerts for. e.g. {"4", "144"}
MENU_VERSION = 7  # bump when COMMANDS change so Telegram's menu gets refreshed
HALTS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
# Halt types you don't want alerts for. Default: skip the short automatic volatility pauses (usually 5 minutes).
# Remove codes from this set to get those too.
SKIP_HALT_CODES = {"M", "LUDP", "LUDS"}
HALT_REASONS = {
    "T1": "News coming — paused until it's out", "T2": "News is out — waiting to resume",
    "T3": "News is out — resume time set", "T5": "Single-stock pause",
    "T6": "Unusual market activity", "T7": "Quotes only, no trading yet", "T8": "ETF halt",
    "T12": "Exchange asked the company for info",
    "H4": "🚨 Not meeting listing rules", "H9": "🚨 Late on required reports",
    "H10": "🚨 SEC suspended trading", "H11": "🚨 Regulator concern",
    "O1": "Operations halt", "IPO1": "IPO not trading yet",
    "M1": "Corporate action", "M2": "No quotes available",
    "M": "Price moved too fast (short pause)", "LUDP": "Price moved too fast (short pause)",
    "LUDS": "Price moved too fast (short pause)",
    "MWC1": "Whole market halted (level 1)", "MWC2": "Whole market halted (level 2)",
    "MWC3": "Whole market halted for the day (level 3)", "D": "🚨 Removed from the exchange",
}

HELP = (
    "Commands:\n"
    "/add TICKER [TICKER...] — add to watchlist\n"
    "/remove TICKER [TICKER...] — remove from watchlist\n"
    "/removeall — clear the watchlist\n"
    "/list — show watchlist\n"
    "/recent TICKER [N] — latest N filings (default 5, max 20)\n"
    "/dividends TICKER — dividend history (per share + total paid)\n"
    "/dilution TICKER YYMMDD [YYMMDD] — dilution filings in a date range (no end = today)\n"
    "/delisting TICKER — delisting status: warned, scheduled, delisted or fixed\n\n"
    "You get alerts for new SEC filings and trading halts on your watchlist.\n\n"
    "Marks: ⚠️ dilution risk · 💰 dividend · 🔀 reverse split · 🚨 delisting risk"
)
COMMANDS = [
    {"command": "add", "description": "Add tickers: /add NVDA AAPL"},
    {"command": "remove", "description": "Remove tickers: /remove NVDA"},
    {"command": "removeall", "description": "Clear the whole watchlist"},
    {"command": "list", "description": "Show watchlist"},
    {"command": "recent", "description": "Latest filings: /recent NVDA 5"},
    {"command": "dividends", "description": "Dividend history: /dividends AAPL"},
    {"command": "dilution", "description": "Dilution filings: /dilution NVDA 260102 260703"},
    {"command": "delisting", "description": "Delisting status: /delisting NVDA"},
]

# ---------- Dilution / share-count warning ----------
DILUTION_FORMS = {"S-1", "F-1", "S-3", "F-3", "S-8", "EFFECT", "D"}  # plus any 424B prospectus
DILUTION_WORDS = re.compile(
    r"(?i)\b(offering|warrants?|at[- ]the[- ]market|ATM program|registered direct|private placement|"
    r"securities purchase agreement|share purchase agreement|equity line|purchase agreement with|"
    r"convertible|PIPE|underwrit\w*|prospectus supplement|issuance of (ordinary|common) shares)\b"
)
SPLIT_WORDS = re.compile(r"(?i)\b(reverse (stock |share )?split|share consolidation)\b")
DIVIDEND_WORDS = re.compile(
    r"(?i)\b(dividends?|cash distributions?|special distributions?|distributions? (to|per) (share|unit)holders?)\b"
)

# ---------- Dividend history (SEC XBRL financial data) ----------
# (taxonomy, concept). First ones are preferred; the bot uses whichever has the newest data.
DIV_PER_SHARE = [
    ("us-gaap", "CommonStockDividendsPerShareDeclared"),
    ("us-gaap", "CommonStockDividendsPerShareCashPaid"),
    ("ifrs-full", "DividendsRecognisedAsDistributionsToOwnersPerShare"),
]
DIV_TOTAL = [
    ("us-gaap", "PaymentsOfDividendsCommonStock"),
    ("us-gaap", "PaymentsOfDividends"),
    ("us-gaap", "DividendsCommonStockCash"),
    ("us-gaap", "DividendsCommonStock"),
    ("ifrs-full", "DividendsPaidClassifiedAsFinancingActivities"),
    ("ifrs-full", "DividendsPaidToEquityHoldersOfParentClassifiedAsFinancingActivities"),
    ("ifrs-full", "DividendsPaid"),
]

FORM_NAMES = {  # plain-English meaning shown at the top of each alert
    "10-K": "📘 Annual report", "10-Q": "📗 Quarterly report",
    "20-F": "📘 Annual report", "40-F": "📘 Annual report",
    "8-K": "📄 Company news", "6-K": "📄 Company news",
    "S-1": "💸 Plans to sell new shares", "F-1": "💸 Plans to sell new shares",
    "S-3": "💸 Can sell new shares anytime (shelf)", "F-3": "💸 Can sell new shares anytime (shelf)",
    "S-4": "🤝 Merger paperwork", "F-4": "🤝 Merger paperwork",
    "S-8": "👔 New shares for employee stock plans",
    "EFFECT": "💸 SEC cleared a share sale — shares can be sold now",
    "3": "👤 New insider's holdings", "4": "👤 Insider bought or sold", "5": "👤 Insider's yearly holdings",
    "144": "👤 Insider plans to sell shares",
    "SC 13D": "🐋 Big holder owns 5%+ (may push for changes)", "SC 13G": "🐋 Big holder owns 5%+ (passive)",
    "SCHEDULE 13D": "🐋 Big holder owns 5%+ (may push for changes)", "SCHEDULE 13G": "🐋 Big holder owns 5%+ (passive)",
    "DEF 14A": "🗳 Shareholder vote coming", "PRE 14A": "🗳 Draft of upcoming shareholder vote",
    "25-NSE": "🚨 Exchange is delisting the stock", "25": "🚨 Company is delisting itself",
    "NT 10-K": "⏰ Annual report will be late", "NT 10-Q": "⏰ Quarterly report will be late",
    "NT 20-F": "⏰ Annual report will be late",
    "CORRESP": "✉️ Company's reply to the SEC", "UPLOAD": "✉️ SEC questions for the company",
    "D": "💸 Sold shares privately",
}
ITEMS_8K = {
    "1.01": "🤝 Signed a major deal", "1.02": "✂️ Ended a major deal", "1.03": "🆘 Bankruptcy",
    "1.05": "🔓 Cyberattack", "2.01": "🏢 Bought or sold a business / assets",
    "2.02": "📊 Earnings results", "2.03": "💳 Took on new debt",
    "2.04": "⚠️ Lender demands early repayment", "2.05": "✂️ Restructuring / layoffs",
    "2.06": "📉 Wrote down asset values",
    "3.01": "🚨 Exchange listing problem (delisting risk)", "3.02": "💸 Sold new shares privately",
    "3.03": "⚠️ Shareholder rights changed", "4.01": "🔄 Changed auditor",
    "4.02": "🚩 Past financials can't be trusted", "5.01": "👑 New controlling owner",
    "5.02": "👥 Director / executive change", "5.03": "📜 Bylaws or fiscal year changed",
    "5.07": "🗳 Shareholder vote results", "7.01": "📢 Public announcement",
    "8.01": "📌 Other news",
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
    name = FORM_NAMES.get(base) or ("💸 Final offering terms — new shares priced" if base.startswith("424B") else "")
    return name + amend if name else ""


def exhibit99_docs(cik, f):
    """File names of the Exhibit 99 documents (press releases etc.), sorted."""
    idx = sec_json(folder_url(cik, f) + "index.json")["directory"]["item"]
    return sorted(i["name"] for i in idx if re.search(r"ex-?_?99", i["name"], re.I) and i["name"].lower().endswith((".htm", ".html")))


def headline_of(lines):
    for line in lines[:15]:
        if len(line) >= 15 and not re.match(r"(?i)(exhibit|ex-?99|press release$|for immediate release)", line):
            return line
    return ""


def exhibit_headline(cik, f):
    """Headline of the first Exhibit 99 document (usually the press release)."""
    names = exhibit99_docs(cik, f)
    return headline_of(html_lines(sec_get(folder_url(cik, f) + names[0]))) if names else ""


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
            f["_text"] = " ".join(body + [d for _, d in exhibits] + [headline])
            if headline:
                out.append("📰 " + clip(headline))
            elif exhibits:
                out += [f"• {clip(d, 160)}" for _, d in exhibits[:3]]
            if body and not headline:
                out.insert(0, clip(" — ".join(body[:2])))
        elif base == "8-K":
            codes = [c.strip() for c in (f.get("items") or "").split(",") if c.strip() and c.strip() != "9.01"]
            if codes:
                out += [ITEMS_8K.get(c, f"Item {c}") for c in codes]
            headline = exhibit_headline(cik, f)
            if headline:
                out.append("📰 " + clip(headline))
            f["_text"] = headline + (" private placement" if "3.02" in codes else "")
    except Exception as e:
        print(f"summary failed for {f['acc']}: {e}")
    if not out and f.get("desc") and f["desc"].upper() not in (f["form"].upper(), base.upper()):
        out.append(clip(f["desc"]))
    return out


def filing_block(cik, f, number=None):
    label = form_label(f["form"])
    code = f"{f['form']} · {f['date']}"
    head = f"{label}\n{code}" if label else code
    if number is not None:
        head = f"{number}) {head}"
    lines = summarize(cik, f)
    base = f["form"].replace("/A", "")
    text = f.get("_text", "")
    if base in DILUTION_FORMS or base.startswith("424B") or DILUTION_WORDS.search(text):
        lines.append("⚠️ Dilution risk — may create new shares")
    if SPLIT_WORDS.search(text):
        lines.append("🔀 Reverse split mentioned — fewer shares, higher price each")
    if DIVIDEND_WORDS.search(text):
        lines.append("💰 Dividend mentioned — check the ex-date if you hold")
    kind = listing_status(text)[0]
    if kind == "resolved":
        lines.append("✅ Listing problem fixed — back in compliance")
    elif kind in ("warning", "decision", "extension") and "3.01" not in (f.get("items") or ""):
        lines.append("🚨 Delisting risk — exchange listing problem")
    return "\n".join([head] + lines)



ANNUAL_FORMS = ("10-K", "20-F", "40-F", "10-KT")
LISTING_FORMS = ("8-A12B", "424B4", "424B1")  # exchange registration / final IPO prospectus


def listing_info(cik):
    """(listing_date, traded_before) for the first exchange listing.

    listing_date: date of the first exchange registration / final IPO prospectus, "" if unknown.
    traded_before: True if the company already filed annual reports before that date
                   (e.g. it traded OTC and later uplisted), so earlier holders could be public.
    Only trusted when SEC returns the full filing history; long-listed companies return ("", True)."""
    sub = sec_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")["filings"]
    if sub.get("files"):  # very long history -> listed long ago
        return "", True
    r = sub["recent"]
    rows = list(zip(r["form"], r["filingDate"]))
    listed = min((d for f, d in rows if f in LISTING_FORMS), default="")
    if not listed:
        return "", False
    traded_before = any(f.split("/")[0] in ANNUAL_FORMS + ("10-Q",) and d < listed for f, d in rows)
    return listed, traded_before


def year_long(r):
    if "start" not in r or "end" not in r:
        return False
    days = (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days
    return 330 <= days <= 380  # full fiscal year, not a quarter


def annual_series(facts, concepts):
    """{fiscal_year_end: (value, unit)} merged across concepts; earlier concepts in the list win."""
    out = {}
    for tax, name in concepts:
        node = facts.get(tax, {}).get(name)
        if not node:
            continue
        found = {}  # end -> (filed, val, unit); a later filing (restatement) wins
        for unit, rows in node.get("units", {}).items():
            for r in rows:
                if year_long(r) and (r["end"] not in found or r.get("filed", "") > found[r["end"]][0]):
                    found[r["end"]] = (r.get("filed", ""), r["val"], unit)
        for end, (_, val, unit) in found.items():
            out.setdefault(end, (val, unit))
    return out


def latest_fiscal_year_end(facts):
    """End date of the company's newest full fiscal year in any annual report."""
    best = ""
    for concepts in facts.values():
        for node in concepts.values():
            for rows in node.get("units", {}).values():
                for r in rows:
                    if r.get("form", "").split("/")[0] in ANNUAL_FORMS and r["end"] > best and year_long(r):
                        best = r["end"]
    return best


def money(val, unit, per_share=False):
    cur = unit.split("/")[0].upper()
    sym = "$" if cur == "USD" else cur + " "
    if per_share:
        return f"{sym}{val:,.4f}".rstrip("0").rstrip(".") if val else f"{sym}0"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(val) >= div:
            return f"{sym}{val / div:,.2f}{suf}"
    return f"{sym}{val:,.0f}"


def dividend_report(t, info, years=6):
    head = f"💰 Dividends — {t} ({info['name']})"
    note = ("Annual reports only (10-K / 20-F); the current fiscal year isn't covered. "
            "New dividend announcements show up as 💰 in filing alerts and /recent.")
    try:
        facts = sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(info['cik']):010d}.json").get("facts", {})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"{head}\n\n❔ No financial data on SEC (common for funds/ETFs and some small issuers). Check /recent."
        raise
    latest = latest_fiscal_year_end(facts)
    if not latest:
        return f"{head}\n\n❔ No annual report data on SEC yet, so this can't be checked. Check /recent."
    try:
        listed, traded_before = listing_info(info["cik"])
    except Exception as e:
        print(f"listing info failed: {e}")
        listed, traded_before = "", False
    long_listed = not listed and traded_before

    ps, tot = annual_series(facts, DIV_PER_SHARE), annual_series(facts, DIV_TOTAL)
    cutoff = date.fromisoformat(latest)
    ends = sorted({e for e in list(ps) + list(tot) if (cutoff - date.fromisoformat(e)).days < years * 366}, reverse=True)

    rows, kinds = [], {}  # kinds: end -> "public" | "private" | "unsure"
    for e in ends:
        per_share, total = (ps.get(e) or (0, ""))[0], (tot.get(e) or (0, ""))[0]
        if not (per_share or total):
            continue  # reported as zero -> nothing to show
        start = (date.fromisoformat(e) - timedelta(days=365)).isoformat()
        if long_listed or (listed and start >= listed):
            kinds[e], mark = "public", "✅ public got it"
        elif listed and e < listed and not traded_before:
            kinds[e], mark = "private", "🔒 pre-listing, public couldn't get it"
        elif listed and e < listed:
            kinds[e], mark = "unsure", "❓ traded OTC before listing — check"
        elif listed:
            kinds[e], mark = "unsure", "❓ listing year — check"
        else:
            kinds[e], mark = "unsure", "❓ listing date unknown — check"
        parts = [f"FY ending {e}"]
        if per_share:
            parts.append(f"{money(per_share, ps[e][1], True)}/share")
        if total:
            parts.append(money(total, tot[e][1]))
        rows.append(" · ".join(parts + [mark]))

    public = [e for e, k in kinds.items() if k == "public"]
    unsure = [e for e, k in kinds.items() if k == "unsure"]
    if kinds.get(latest) == "public":
        verdict = f"✅ YES — public shareholders received a dividend in its latest fiscal year (ending {latest})."
    elif public:
        verdict = f"⚠️ YES before — public shareholders received dividends (last: FY ending {max(public)}), but none in its latest fiscal year (ending {latest})."
    elif unsure:
        verdict = "❓ UNCLEAR — SEC data can't tell if public holders got the payout(s) marked ❓. Check those filings."
    elif kinds:
        verdict = f"🔒 NO — no dividend has gone to public shareholders through its latest fiscal year (ending {latest}). Earlier payouts were before listing."
    else:
        verdict = (f"🚫 NO — no dividends found in the last {years} fiscal years (latest ends {latest}).\n"
                   "Some issuers don't tag dividend data, so check /recent to be 100% sure.")

    lines = [head]
    if listed:
        lines.append(f"Listed: {listed}")
    lines.append("")
    lines.append(verdict)
    if rows:
        lines += ["", "\n".join(rows)]
    lines += ["", note]
    return "\n".join(lines)


# ---------- Dilution check for a date range ----------
def parse_yymmdd(s):
    """'261012' -> date(2026, 10, 12). Raises ValueError if the format is wrong."""
    if len(s) != 6 or not s.isdigit():
        raise ValueError(s)
    return date(2000 + int(s[:2]), int(s[2:4]), int(s[4:6]))


def dilution_report(t, info, start, end):
    """Only the dilution-related filings filed between start and end (inclusive)."""
    filings = [f for f in recent_filings(info["cik"], 1000) if start.isoformat() <= f["date"] <= end.isoformat()]
    hits = []
    for f in reversed(filings):  # oldest first
        block = filing_block(info["cik"], f)
        if "Dilution risk" in block:
            hits.append(f"{block}\n🔗 {filing_url(info['cik'], f)}")
    head = (f"⚠️ Dilution check — {t} ({info['name']})\n"
            f"{start:%y%m%d} → {end:%y%m%d} · {len(filings)} filing(s) checked")
    if not hits:
        return head + "\n\n✅ No dilution-related filings in this period."
    return head + f"\n\n🚩 {len(hits)} dilution-related filing(s):\n\n" + "\n\n".join(hits)


# ---------- Delisting check ----------
DELIST_YEARS = 3       # how far back /delisting looks
DELIST_MAX_READ = 60   # max 8-K / 6-K filings whose text is read (newest first), to keep the run short
ITEM_301_TITLE = re.compile(r"(?i)notice of delisting or failure to satisfy a continued listing rule or standard;? (and )?transfer of listing")
LISTING_CONTEXT = re.compile(
    r"(?i)nasdaq|nyse|listing|bid price|(stockholders|shareholders)['’]? equity|market value|\b5\d{3}\(|\b80[23]\.0"
)
CONDITIONAL = re.compile(r"(?i)\b(if|unless|until|whether|in the event|no assurance)\b")
# Warning letters say the company "may be eligible for an additional 180 days" -> that's not an extension yet.
MAYBE = re.compile(r"(?i)\b(if|unless|whether|in the event|may|eligib\w*|no assurance)\b")
# Checked in this order: the first kind with a matching sentence wins.
LISTING_PATTERNS = [
    ("resolved", re.compile(
        r"(?i)\bregained (full )?compliance|\bhas regained\b|\bregains compliance\b|\b(is|was) (now|again) in compliance\b|"
        r"\breturned to compliance\b|\bmatter (is|has been|was) (now )?closed\b|"
        r"\bconfirm\w* (that )?the company (had |has )?(regained|evidenced|demonstrated) compliance")),
    ("decision", re.compile(
        r"(?i)delisting determination|determined to delist|determination to delist|proceedings to delist|"
        r"commence\w* (delisting )?proceedings|will be suspended|requested a hearing|request for a hearing|"
        r"hearing (is |was |has been )?scheduled|(will|to) be delisted|suspend\w* trading")),
    ("extension", re.compile(
        r"(?i)additional (180|\d+)[- ]calendar[- ]day|second (180|compliance period)|"
        r"(granted|approved) (the company['’]?s? )?(request for )?(an |a )?(extension|exception)|extended .{0,40}deadline")),
    ("warning", re.compile(
        r"(?i)deficiency|notification letter|notice letter|written notice|not in compliance|non-?compliance|"
        r"no longer (in compliance|compl\w+|satisf\w+|meets?)|did not (comply|meet|satisfy)|fail\w* to (satisfy|comply|meet|timely)|"
        r"below (the )?(minimum|continued listing)|(below|less than) \$1\.00")),
    ("transfer", re.compile(
        r"(?i)transfer\w* (of )?(the |its )?(listing|securities|common stock|ordinary shares|shares|american depositary shares)\b|"
        r"\bto transfer\b.{0,60}(capital market|global market|nyse|nasdaq)")),
]
LISTING_REASONS = [  # (key, pattern, plain-English reason)
    ("price", re.compile(r"(?i)bid price|\$1\.00|average closing price|5550\(a\)\(2\)|5450\(a\)\(1\)|802\.01c"), "Share price under $1"),
    ("equity", re.compile(r"(?i)(stockholders|shareholders)['’]? equity|5550\(b\)\(1\)|5450\(b\)\(1\)"), "Shareholders' equity too low"),
    ("mvls", re.compile(r"(?i)market value of listed securities|\bMVLS\b|5550\(b\)\(2\)|5450\(b\)\(2\)"), "Company's total market value too low"),
    ("public", re.compile(r"(?i)publicly held shares|\bMVPHS\b|round lot|total holders|5550\(a\)\([345]\)"), "Not enough shares / holders in public hands"),
    ("mcap", re.compile(r"(?i)market capitali[sz]ation|802\.01b"), "Market cap too low"),
    ("reports", re.compile(r"(?i)5250\(c\)|periodic (financial )?reports?|timely fil|delinquen|form 10-[kq]\b|form 20-f|802\.01e"), "Late on financial reports"),
    ("governance", re.compile(r"(?i)audit committee|independent directors?|5605"), "Board / audit committee rule broken"),
    ("meeting", re.compile(r"(?i)annual (general |shareholders['’]? |stockholders['’]? )?meeting|5620\(a\)"), "Missed the annual shareholder meeting"),
    ("interest", re.compile(r"(?i)public interest|5101"), "Exchange has public-interest concerns"),
]
REASON_NAMES = {k: name for k, _, name in LISTING_REASONS}
REASON_NAMES["other"] = "Listing rule (reason not clear from the filing)"
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MONTH_DATE = r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})"
DEADLINE = re.compile(rf"(?i)\b(?:until|by|through|before|expir\w* on)\s+(?:on\s+)?{MONTH_DATE}")
DELIST_KINDS = {  # what each event means, in plain English
    "warning": "⚠️ Exchange warning — not meeting a listing rule (not delisted yet)",
    "extension": "⏳ Got more time to fix the problem",
    "decision": "🚨 Exchange decided to delist — company can appeal",
    "resolved": "✅ Fixed — back in compliance",
    "transfer": "🔁 Moved to another market tier (often to buy time to fix a problem)",
    "split": "🔀 Reverse split — usually done to get the price back above $1",
    "delist": "🚨 Exchange filed to remove the stock (Form 25-NSE)",
    "selfdelist": "🚪 Company filed to delist itself (Form 25)",
    "switch": "🔁 Switched exchange — not a real delisting",
    "partial": "ℹ️ Only {cls} removed — the main shares are not affected",
    "deregister": "📴 Company stopped reporting to the SEC (Form 15)",
}


def listing_status(text, title=""):
    """What a filing says about the exchange listing.

    Returns (kind, reason keys, matching sentences); kind is "" when the filing isn't about listing."""
    text = ITEM_301_TITLE.sub(" ", text or "")
    sentences = [s for s in re.split(r"(?<=[.;!?])\s+", f"{title}. {text}") if LISTING_CONTEXT.search(s)]
    for kind, pattern in LISTING_PATTERNS:
        hits = [s for s in sentences if pattern.search(s)
                and not (kind in ("resolved", "decision") and CONDITIONAL.search(s))
                and not (kind == "extension" and MAYBE.search(s))]
        if hits:
            joined = " ".join(hits)
            reasons = [k for k, rx, _ in LISTING_REASONS if rx.search(joined)]
            if "equity" in reasons and "mvls" in reasons:  # letters list the alternatives too -> keep the one named first
                eq, mv = (re.search(rx, joined).start() for k, rx, _ in LISTING_REASONS if k in ("equity", "mvls"))
                reasons.remove("mvls" if eq < mv else "equity")
            return kind, reasons, hits
    return "", [], []


def find_deadline(texts, filed):
    """First 'until / by <Month D, YYYY>' date after the filing date, as YYYY-MM-DD ('' if none)."""
    for s in texts:
        for m in DEADLINE.finditer(s):
            try:
                d = date(int(m.group(3)), MONTHS.index(m.group(1).capitalize()) + 1, int(m.group(2))).isoformat()
            except ValueError:
                continue
            if d > filed:
                return d
    return ""


def listing_text(cik, f):
    """(text, headline) of an 8-K / 6-K: main document plus its press releases if it looks listing-related."""
    main = " ".join(html_lines(sec_get(filing_url(cik, f)))) if f["doc"] else ""
    texts, title = [main[:20000]], ""
    if LISTING_CONTEXT.search(main) or re.search(r"(?i)press release|exhibit 99", main):
        for name in exhibit99_docs(cik, f)[:2]:
            lines = html_lines(sec_get(folder_url(cik, f) + name))
            title = title or headline_of(lines)
            texts.append(" ".join(lines)[:20000])
    return " ".join(texts), title


def delisted_class(cik, f):
    """Class a Form 25 removes, e.g. 'Warrants'. '' means the main shares (or unknown)."""
    doc = re.sub(r"^xsl[^/]*/", "", f["doc"] or "")  # raw XML instead of SEC's rendered page
    raw = sec_get(folder_url(cik, f) + doc).decode("utf-8", "replace")
    m = re.search(r"(?is)<(\w*class\w*)>(.*?)</\1>", raw) or re.search(r"(?i)class of securities\W{0,20}(.{0,120})", raw)
    desc = html.unescape(re.sub(r"<[^>]+>", " ", m.group(m.lastindex))) if m else ""
    main = re.search(r"(?i)common (stock|shares)|ordinary shares|american depositary|class [a-c] (common|ordinary)", desc)
    side = re.search(r"(?i)warrants?|notes?|debentures?|preferred|units?|rights|bonds?|depositary shares", desc)
    if side and (not main or side.start() < main.start()):
        return side.group(0).capitalize()
    return ""


def delisting_events(cik, filings):
    """Listing-related events, oldest first, plus how many 8-K / 6-K were left unread (cap)."""
    events, read, skipped = [], 0, 0
    exchange_regs = [x["date"] for x in filings if x["form"] == "8-A12B"]
    for f in filings:  # newest first, so the read cap drops the oldest
        form = f["form"].replace("/A", "")
        items = {c.strip() for c in (f.get("items") or "").split(",")}
        ev = {"date": f["date"], "form": f["form"], "url": filing_url(cik, f), "reasons": [], "deadline": "", "title": ""}
        if form in ("25-NSE", "25"):
            near = [d for d in exchange_regs if abs((date.fromisoformat(d) - date.fromisoformat(f["date"])).days) <= 30]
            try:
                cls = delisted_class(cik, f)
            except Exception as e:
                print(f"form 25 read failed for {f['acc']}: {e}")
                cls = ""
            if cls:
                ev.update(kind="partial", cls=cls)
            else:
                ev["kind"] = "switch" if near else ("delist" if form == "25-NSE" else "selfdelist")
            events.append(ev)
        elif form.startswith(("15-", "15F-")):
            events.append({**ev, "kind": "deregister"})
        elif form == "6-K" or (form == "8-K" and items & {"3.01", "7.01", "8.01"}):
            if read >= DELIST_MAX_READ:
                skipped += 1
                continue
            read += 1
            try:
                text, title = listing_text(cik, f)
            except Exception as e:
                print(f"listing text failed for {f['acc']}: {e}")
                text, title = "", ""
            kind, reasons, _ = listing_status(text, title)
            if not kind and "3.01" in items:  # Item 3.01 is a listing problem by definition
                kind = "warning"
            if kind == "warning" and SPLIT_WORDS.search(title or text[:3000]):  # the fix for a low price, not a new warning
                kind, reasons = "split", ["price"]
            if kind:
                ev.update(kind=kind, reasons=reasons, title=title,
                          deadline=find_deadline([text], f["date"]) if kind in ("warning", "extension") else "")
                events.append(ev)
    return list(reversed(events)), skipped


def track_issues(events):
    """Replay events oldest first -> (open issues {reason: event}, list of 'resolved' events)."""
    open_issues, fixed = {}, []
    for e in events:
        if e["kind"] in ("warning", "decision", "extension"):
            for r in e["reasons"] or list(open_issues) or ["other"]:
                prev = open_issues.get(r, {})
                open_issues[r] = {**e, "since": prev.get("since", e["date"]),
                                  "deadline": e["deadline"] or (prev.get("deadline", "") if e["kind"] != "decision" else ""),
                                  "decided": prev.get("decided") or e["kind"] == "decision"}
        elif e["kind"] == "split" and "price" in open_issues:
            open_issues["price"]["split"] = e["date"]
        elif e["kind"] == "resolved":
            keys = [r for r in e["reasons"] if r in open_issues]
            for r in (keys + ["other"]) if keys else list(open_issues):
                open_issues.pop(r, None)
            fixed.append(e)
    return open_issues, fixed


def delisting_report(t, info):
    today = date.today()
    since = (today - timedelta(days=DELIST_YEARS * 365)).isoformat()
    filings = [f for f in recent_filings(info["cik"], 1000) if f["date"] >= since]
    events, skipped = delisting_events(info["cik"], filings)
    exchange = info.get("exchange") or "?"

    main = [e for e in events if e["kind"] in ("delist", "selfdelist")]
    stopped = [e for e in events if e["kind"] == "deregister"]
    open_issues, fixed = track_issues(events)
    if main:
        e = main[-1]
        effective = date.fromisoformat(e["date"]) + timedelta(days=10)  # Form 25 takes effect 10 days after filing
        who = "The exchange" if e["kind"] == "delist" else "The company itself"
        if effective > today:
            verdict = f"🚨 DELISTING SCHEDULED — {who} filed to remove the stock on {e['date']}. It takes effect around {effective}."
        else:
            verdict = f"🚨 DELISTED — {who} filed to remove the stock on {e['date']}; removed around {effective}."
        verdict += "\n(This also happens after a buyout/merger, so check the latest news.)"
    elif stopped:
        verdict = f"📴 STOPPED REPORTING — filed Form 15 on {stopped[-1]['date']}. Usually means it left the exchange or is going dark."
    elif open_issues:
        # An old warning with no delist decision a month after its deadline was most likely fixed without an announcement:
        # if it weren't, the exchange would have sent a delist decision, and that has to be filed.
        stale = {r for r, e in open_issues.items() if not e["decided"] and (
            (e["deadline"] and e["deadline"] < (today - timedelta(days=30)).isoformat()) or (today - date.fromisoformat(e["date"])).days > 365)}
        if any(e["decided"] for e in open_issues.values()):
            verdict = "🚨 AT RISK — the exchange has decided to delist; the company is appealing or running out of time."
        elif stale == set(open_issues):
            verdict = ("❔ PROBABLY FINE — got an exchange warning, but the deadline passed with no delist decision filed, "
                       "so it isn't being delisted. Companies often don't announce the fix.")
        else:
            verdict = "⚠️ ONGOING — the exchange warned the company and no 'fixed' filing has come out yet."
        rows = []
        for r, e in open_issues.items():
            row = f"• {REASON_NAMES.get(r, r)} — since {e['since']}"
            if e["deadline"]:
                row += f" · deadline {e['deadline']}"
            if e["decided"]:
                row += " · 🚨 delist decision"
            if e.get("split"):
                row += f"\n   (reverse split on {e['split']} — price likely back above $1)"
            if r in stale:
                row += "\n   (deadline passed with no delist decision — likely fixed quietly)"
            rows.append(row)
        verdict += "\n" + "\n".join(rows)
        if fixed:
            verdict += f"\nEarlier problems were fixed (last fix: {fixed[-1]['date']})."
    elif fixed:
        verdict = f"✅ RESOLVED — had an exchange listing problem, but it was fixed (last fix: {fixed[-1]['date']})."
    else:
        verdict = f"✅ NO — no delisting problems in its SEC filings since {since}."
    if not main and exchange.upper() == "OTC":
        verdict += "\nℹ️ It currently trades OTC (not on a major exchange)."

    blocks = []
    for e in events[-12:]:
        lines = [f"{e['date']} · {e['form']}", DELIST_KINDS[e["kind"]].format(cls=e.get("cls", ""))]
        if e["reasons"]:
            lines.append("Why: " + ", ".join(REASON_NAMES[r] for r in e["reasons"]))
        if e["deadline"]:
            lines.append(f"Deadline to fix: {e['deadline']}")
        if e["title"]:
            lines.append("📰 " + clip(e["title"]))
        lines.append(f"🔗 {e['url']}")
        blocks.append("\n".join(lines))

    head = (f"🚨 Delisting check — {t} ({info['name']})\n"
            f"Exchange: {exchange} · {len(filings)} filing(s) since {since} checked")
    out = f"{head}\n\n{verdict}"
    if blocks:
        more = f" (last {len(blocks)} of {len(events)})" if len(events) > len(blocks) else ""
        out += f"\n\nTimeline, oldest first{more}:\n\n" + "\n\n".join(blocks)
    note = "Based on SEC filings only. A warning is NOT a delisting — companies usually get 180 days to fix it."
    if skipped:
        note += f"\n{skipped} older 8-K / 6-K filing(s) weren't read (limit {DELIST_MAX_READ})."
    return out + "\n\n" + note


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
            send("📋 Your watchlist is empty.\n\nAdd tickers with /add NVDA AAPL")
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

    elif cmd in ("/dividends", "/dividend") and args:
        t = args[0]
        info = watch.get(t) or lookup(t)
        if not info:
            send(f"{t}: not found on SEC — check the ticker")
            return
        send(dividend_report(t, info))

    elif cmd == "/dilution" and len(args) >= 2:
        t = args[0]
        try:
            start = parse_yymmdd(args[1])
            end = parse_yymmdd(args[2]) if len(args) > 2 else date.today()
        except ValueError:
            send("Dates must be YYMMDD, e.g. /dilution NVDA 260102 260703")
            return
        if start > end:
            start, end = end, start
        info = watch.get(t) or lookup(t)
        if not info:
            send(f"{t}: not found on SEC — check the ticker")
            return
        send(dilution_report(t, info, start, end))

    elif cmd in ("/delisting", "/delist") and args:
        t = args[0]
        info = watch.get(t) or lookup(t)
        if not info:
            send(f"{t}: not found on SEC — check the ticker")
            return
        send(delisting_report(t, info))

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
            send(f"🔔 {t} · {w['name']}\n\n{filing_block(w['cik'], f)}\n\n🔗 {filing_url(w['cik'], f)}")
        w["seen"] = [f["acc"] for f in filings]


# ---------- Check trading halts ----------
def fetch_halts():
    """Current Nasdaq halt feed (covers NYSE, Arca, AMEX too) as a list of dicts."""
    req = urllib.request.Request(HALTS_URL, headers={"User-Agent": "Mozilla/5.0 (sec-alert bot)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        xml = r.read().decode("utf-8", "replace")
    halts = []
    for item in re.findall(r"(?s)<item>(.*?)</item>", xml):
        def tag(name):
            m = re.search(rf"(?s)<ndaq:{name}>(.*?)</ndaq:{name}>", item)
            return html.unescape(m.group(1)).strip() if m else ""
        halts.append({
            "symbol": tag("IssueSymbol").upper(), "name": tag("IssueName"), "market": tag("Market"),
            "code": tag("ReasonCode"), "date": tag("HaltDate"), "time": tag("HaltTime").split(".")[0],
            "resume_date": tag("ResumptionDate"), "resume_time": tag("ResumptionTradeTime").split(".")[0],
        })
    return halts


def halt_reason(code):
    return f"{HALT_REASONS.get(code, 'Other')} ({code})" if code else "Not given"


def check_halts(state):
    watch = state["watch"]
    if not watch:
        return
    try:
        halts = fetch_halts()
    except Exception as e:  # halt feed down -> just try again next run
        print(f"halt feed failed: {e}")
        return
    known = state.setdefault("halts", {})  # key -> True once "resumed" has been reported
    recent = {time.strftime("%m/%d/%Y", time.gmtime(time.time() - d * 86400)) for d in range(3)}
    current = set()
    for h in halts:
        if h["symbol"] not in watch or h["code"] in SKIP_HALT_CODES:
            continue
        key = f"{h['symbol']}|{h['date']}|{h['time']}|{h['code']}"
        current.add(key)
        resumed = bool(h["resume_time"])
        where = f" · {h['market']}" if h["market"] else ""
        if key not in known:
            # new halt: alert if it's still ongoing, or it happened in the last few days
            if not resumed or h["date"] in recent:
                status = (f"Resumed: {h['resume_date']} {h['resume_time']} ET" if resumed
                          else "Still halted — you'll get a message when trading resumes.")
                send(f"⛔ Trading halt — {h['symbol']}{where}\n{watch[h['symbol']]['name']}\n\n"
                     f"Reason: {halt_reason(h['code'])}\nHalted: {h['date']} {h['time']} ET\n{status}")
            known[key] = resumed
        elif resumed and not known[key]:
            send(f"✅ Trading resumed — {h['symbol']}{where}\n{watch[h['symbol']]['name']}\n\n"
                 f"Halted {h['date']} {h['time']} → resumed {h['resume_date']} {h['resume_time']} ET\n"
                 f"Reason was: {halt_reason(h['code'])}")
            known[key] = True
    for key in list(known):  # forget halts that dropped off the feed or tickers you removed
        if key not in current:
            del known[key]


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {"offset": 0, "watch": {}}
    handle_commands(state)
    check_filings(state)
    check_halts(state)
    # guarantees a commit at least monthly, so GitHub doesn't disable the schedule after 60 idle days
    state["heartbeat"] = time.strftime("%Y-%m")
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
