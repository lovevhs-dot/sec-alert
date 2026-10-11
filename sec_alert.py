"""SEC filing -> Telegram alert bot.

Telegram commands:
  /add NCT AAPL     add tickers (one or more)
  /remove NCT       remove tickers
  /removeall        clear the whole watchlist
  /list             show the watchlist
  /recent NCT [N]   latest N filings with what each one is about (default 5)
  /dividends NCT    dividend history: could public shareholders receive it, plus recent announcements
  /shares NCT       share count history, split-adjusted (real dilution vs. splits)
  /delisting NCT    listing deficiency notices, deadlines, reverse-split rule risks

Alerts: new SEC filings and trading halts (Nasdaq halt feed, all US exchanges) for your watchlist.
Filings that announce a dividend are marked 💰 with ex-date and the last day to sell to avoid it.
Listing deficiency / delisting filings are marked 🚨, splits 🔀.

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
MENU_VERSION = 6  # bump when COMMANDS change so Telegram's menu gets refreshed
HALTS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
# Halt types you don't want alerts for. Default: skip the short automatic volatility pauses (usually 5 minutes).
# Remove codes from this set to get those too.
SKIP_HALT_CODES = {"M", "LUDP", "LUDS"}
HALT_REASONS = {
    "T1": "News pending", "T2": "News released", "T3": "News and resumption times",
    "T5": "Single stock trading pause", "T6": "Extraordinary market activity",
    "T7": "Quotation-only period", "T8": "ETF halt", "T12": "Additional information requested",
    "H4": "Non-compliance", "H9": "Not current in required filings", "H10": "SEC trading suspension",
    "H11": "Regulatory concern", "O1": "Operations halt", "IPO1": "IPO not yet trading",
    "M1": "Corporate action", "M2": "Quotation not available",
    "M": "Volatility pause (LULD)", "LUDP": "Volatility pause (LULD)", "LUDS": "Volatility pause (LULD)",
    "MWC1": "Market-wide circuit breaker (level 1)", "MWC2": "Market-wide circuit breaker (level 2)",
    "MWC3": "Market-wide circuit breaker (level 3)", "D": "Security deletion",
}

HELP = (
    "Commands:\n"
    "/add TICKER [TICKER...] — add to watchlist\n"
    "/remove TICKER [TICKER...] — remove from watchlist\n"
    "/removeall — clear the watchlist\n"
    "/list — show watchlist\n"
    "/recent TICKER [N] — latest N filings (default 5, max 20)\n"
    "/dividends TICKER — dividend history + recent announcements\n"
    "/shares TICKER — share count history (split-adjusted)\n"
    "/delisting TICKER — listing notices, deadlines, delisting risk\n\n"
    "You get alerts for new SEC filings and trading halts on your watchlist. "
    "Marks: ⚠️ dilution, 💰 dividend, 🔀 split, 🚨 listing problem."
)
COMMANDS = [
    {"command": "add", "description": "Add tickers: /add NCT AAPL"},
    {"command": "remove", "description": "Remove tickers: /remove NCT"},
    {"command": "removeall", "description": "Clear the whole watchlist"},
    {"command": "list", "description": "Show watchlist"},
    {"command": "recent", "description": "Latest filings: /recent NCT 5"},
    {"command": "dividends", "description": "Dividend history: /dividends AAPL"},
    {"command": "shares", "description": "Share count history: /shares NCT"},
    {"command": "delisting", "description": "Listing status / delisting risk: /delisting NCT"},
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


def ex99_lines(cik, f):
    """Text lines of the first Exhibit 99 document (usually the press release)."""
    try:
        idx = sec_json(folder_url(cik, f) + "index.json")["directory"]["item"]
        names = [i["name"] for i in idx if re.search(r"ex-?_?99", i["name"], re.I) and i["name"].lower().endswith((".htm", ".html"))]
        return html_lines(sec_get(folder_url(cik, f) + sorted(names)[0])) if names else []
    except Exception as e:
        print(f"exhibit failed for {f['acc']}: {e}")
        return []


def headline_of(lines):
    for line in lines[:15]:
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
            ex = ex99_lines(cik, f)
            headline = headline_of(ex) if mentions_pr or not body else ""
            f["_text"] = " ".join(body + [d for _, d in exhibits] + [headline])
            f["_full"] = " ".join(lines + ex)
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
            ex = ex99_lines(cik, f)
            headline = headline_of(ex)
            if headline:
                out.append("📰 " + clip(headline))
            f["_text"] = headline + (" private placement" if "3.02" in codes else "")
            f["_full"] = " ".join(html_lines(sec_get(filing_url(cik, f))) + ex)
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
    lines = summarize(cik, f)
    base = f["form"].replace("/A", "")
    text = f.get("_text", "")
    if base in DILUTION_FORMS or base.startswith("424B") or DILUTION_WORDS.search(text):
        lines.append("⚠️ Possible dilution: may add new shares (check the filing)")
    full = f.get("_full", "") or text
    if base.startswith("25"):
        eff = (date.fromisoformat(f["date"]) + timedelta(days=10)).isoformat()
        lines.append(f"🚨🚨 Delisting filed — removal takes effect around {nice(eff)}")
    ev = listing_event(full)
    if ev:
        lines += listing_lines(ev)
    sp = split_info(full)
    if sp:
        lines.append(split_line(sp))
    if announces_dividend(full) or announces_dividend(text):
        lines += dividend_lines(full)
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

    # announcements after the latest annual report (the part annual data can't see)
    since = (date.fromisoformat(latest) + timedelta(days=1)).isoformat()
    search_failed = False
    try:
        recent = dividend_announcements(info["cik"], since, listed)
    except Exception as e:
        print(f"announcement search failed: {e}")
        recent, search_failed = [], True
    ann, today = [], date.today().isoformat()
    for d in recent:
        line = f"📢 {d['filed']}" + (f" · {d['amount']}" if d["amount"] else "")
        ex = d["ex"] or d["record"]
        if ex:
            line += f" · ex-date {ex}" + ("" if d["ex"] else " (record date)")
        line += " · ✅ public" if d["public"] else " · 🔒 pre-listing"
        ann.append(line)
        if ex and ex >= today:
            ann.append(f"   ⏰ UPCOMING — to avoid it, don't hold at the close on {nice(prev_weekday(ex))}")
    if any(d["public"] for d in recent):
        new = "✅ YES — a dividend for public shareholders was announced after the last annual report."
        verdict = new if verdict.startswith("✅") else f"{new}\n{verdict}"
    note = ("Annual figures come from 10-K / 20-F. Announcements after the latest one are searched separately below. "
            "New ones also show up as 💰 in filing alerts.")

    lines = [head]
    if listed:
        lines.append(f"Listed: {listed}")
    lines.append("")
    lines.append(verdict)
    if rows:
        lines += ["", "\n".join(rows)]
    empty = ["❔ Search failed right now — can't confirm there's no new dividend. Try again later."] if search_failed \
        else ["None found."]
    lines += ["", f"Announced after FY ending {latest}:"] + (ann or empty)
    lines += ["", note]
    return "\n".join(lines)


# ---------- Dates in filing text ----------
_MON = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_RE = re.compile(
    rf"(?i)\b(?:({_MON})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})"
    rf"|(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON})[a-z]*\.?,?\s+(\d{{4}})"
    rf"|(\d{{4}})-(\d{{2}})-(\d{{2}}))\b"
)
MONTH_NUM = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def all_dates(text):
    out = []
    for m in DATE_RE.finditer(text):
        g = m.groups()
        try:
            if g[0]:
                d = date(int(g[2]), MONTH_NUM[g[0][:3].lower()], int(g[1]))
            elif g[3]:
                d = date(int(g[5]), MONTH_NUM[g[4][:3].lower()], int(g[3]))
            else:
                d = date(int(g[6]), int(g[7]), int(g[8]))
        except ValueError:
            continue
        out.append((m.start(), d.isoformat()))
    return out


def nearest_date(text, keyword, after=220, before=120):
    """Date closest to a keyword; dates after the keyword are preferred."""
    dates, best = all_dates(text), None
    for m in re.finditer(keyword, text):
        for pos, d in dates:
            dist = pos - m.end()
            if -before <= dist <= after:
                score = dist if dist >= 0 else -dist * 3
                if best is None or score < best[0]:
                    best = (score, d)
    return best[1] if best else ""


def dates_near(text, keyword, after=220, before=120):
    dates, out = all_dates(text), set()
    for m in re.finditer(keyword, text):
        out |= {d for pos, d in dates if -before <= pos - m.end() <= after}
    return out


def nice(iso):
    return date.fromisoformat(iso).strftime("%b %d, %Y (%a)") if iso else "?"


def prev_weekday(iso):
    d = date.fromisoformat(iso) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


# ---------- Dividend announcements ----------
DIV_DECLARED = re.compile(
    r"(?i)(declar\w*|approv\w*)\W+(?:\w+\W+){0,12}?(cash |special |interim |final |quarterly |annual )?(dividend|distribution)"
    r"|(dividend|distribution)\W+(?:\w+\W+){0,12}?(payable|declared|record date)"
)
DIV_NEGATION = re.compile(r"(?i)\b(never|not|no|nor|any)\b[^.]{0,40}$")


def announces_dividend(text):
    """True if the text declares a dividend (ignores 'has never declared ... dividends' boilerplate)."""
    for m in DIV_DECLARED.finditer(text or ""):
        if not DIV_NEGATION.search(text[max(0, m.start() - 60): m.start() + 25]):
            return True
    return False


DIV_AMOUNT = re.compile(
    r"(?i)(US\$|USD\s?|\$)\s?(\d+(?:\.\d+)?)\s*(?:\(\w+\)\s*)?(?:per|for each|a)\s+(?:ordinary |common |class \w+ )?(share|ADS)"
)


def dividend_details(text):
    m = DIV_AMOUNT.search(text)
    return {
        "amount": f"${m.group(2)}/{m.group(3).lower()}" if m else "",
        "ex": nearest_date(text, r"(?i)ex-?\s?dividend|ex-date|ex date"),
        "record": nearest_date(text, r"(?i)record date|of record|holders on record"),
        "pay": nearest_date(text, r"(?i)payable|payment date|paid on|to be paid|distributed on"),
    }


def dividend_lines(text):
    d = dividend_details(text)
    out = ["💰 Dividend" + (f": {d['amount']}" if d["amount"] else " announced / mentioned")]
    dates = [f"{k}: {nice(d[v])}" for k, v in (("Ex-date", "ex"), ("Record", "record"), ("Payable", "pay")) if d[v]]
    if dates:
        out.append("   " + " · ".join(dates))
    ex = d["ex"] or d["record"]
    if ex:
        if not d["ex"]:
            out.append("   Ex-date not stated — with T+1 settlement it's normally the record date. Confirm with your broker.")
        out.append(f"   🚫 To avoid it: don't hold shares at the close on {nice(prev_weekday(ex))} "
                   f"(or the trading day before, if that's a holiday). Selling on {nice(ex)} or later still gets it.")
        out.append("   If the payout is 25%+ of the share price, the ex-date is the day after the payable date instead.")
    else:
        out.append("   No dates found — open the filing to check.")
    return out


def dividend_announcements(cik, since, listed, limit=8):
    """Dividend announcements in 8-K/6-K filings after `since` (dates included)."""
    docs = fts_docs(cik, ['"cash dividend"', '"special dividend"', '"ex-dividend"', '"dividend of"',
                          '"cash distribution"', '"interim dividend"'], since, limit=limit * 2)
    out = []
    for doc in docs:
        if not announces_dividend(doc["text"]):
            continue
        d = dividend_details(doc["text"])
        d["filed"], d["public"] = doc["date"], not listed or doc["date"] >= listed
        out.append(d)
    return out[:limit]


# ---------- EDGAR full-text search ----------
def fts(q, cik, start, forms="8-K,6-K"):
    params = {"q": q, "ciks": f"{int(cik):010d}", "forms": forms, "dateRange": "custom",
              "startdt": start, "enddt": date.today().isoformat()}
    hits = sec_json("https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(params))
    out = []
    for h in hits.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        if ":" in h.get("_id", ""):
            out.append({"adsh": src.get("adsh", ""), "file": h["_id"].split(":", 1)[1],
                        "date": src.get("file_date", ""), "form": src.get("form", "")})
    return out


class SearchFailed(Exception):
    pass


def fts_docs(cik, queries, start, limit=12):
    """Newest filings matching any query, with the text of the matching documents.
    Raises SearchFailed if any search fails, so callers never mistake a failure for "nothing found"."""
    found = {}
    for q in queries:
        try:
            for h in fts(q, cik, start):
                doc = found.setdefault(h["adsh"], {**h, "files": set()})
                doc["files"].add(h["file"])
        except Exception as e:
            raise SearchFailed(f"SEC full-text search failed ({q}): {e}")
    docs = sorted(found.values(), key=lambda d: d["date"], reverse=True)[:limit]
    for doc in docs:
        texts = []
        for name in sorted(doc["files"]):
            try:
                url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{doc['adsh'].replace('-', '')}/{name}"
                texts.append(" ".join(html_lines(sec_get(url))))
            except Exception as e:
                print(f"doc failed {name}: {e}")
        doc["text"] = " ".join(texts)
        doc["url"] = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{doc['adsh'].replace('-', '')}/"
    return docs


# ---------- Splits ----------
SPLIT_ANY = re.compile(
    r"(?i)\b(reverse (stock |share )?split|share consolidation|consolidation of (its |the company['’]s )?(ordinary |common )?shares"
    r"|forward (stock |share )?split|share subdivision|stock split|share split)\b"
)
SPLIT_DONE = re.compile(
    r"(?i)split[- ]adjusted basis|market effective date|post[- ](reverse[- ])?split basis|consolidated basis"
    r"|(split|consolidation)\W+(?:\w+\W+){0,15}?(became|become|becomes|will be|was|is) effective"
    r"|effective\W+(?:\w+\W+){0,10}?(split|consolidation)"
    r"|(split|consolidation)\W+(?:\w+\W+){0,6}?effective (on|at|as of|after|before|upon)"
)
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "sixteen": 16, "twenty": 20, "twenty-five": 25,
             "thirty": 30, "thirty-five": 35, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "seventy-five": 75,
             "eighty": 80, "ninety": 90, "hundred": 100, "one hundred": 100, "two hundred": 200, "two hundred fifty": 250}
_NUM = r"(\d[\d,]*|" + "|".join(sorted(map(re.escape, NUM_WORDS), key=len, reverse=True)) + r")"
RATIO_RES = [
    re.compile(rf"(?i)\b{_NUM}\s*(?:\(\d+\)\s*)?[-‐–\s]for[-‐–\s]\s*{_NUM}\b"),
    re.compile(r"(?i)\b(1)\s*:\s*(\d{1,4})\b(?!\s*[ap]\.?m)"),
    re.compile(r"(?i)\b(\d{1,4})\s*:\s*(1)\b(?!\d|\s*[ap]\.?m)"),
    re.compile(rf"(?i)every\s+{_NUM}\s*(?:\(\d+\)\s*)?(?:issued and outstanding\s+|ordinary\s+|common\s+)*shares?\W+(?:\w+\W+){{0,8}}?into\s+{_NUM}\b"),
]


def _num(s):
    s = s.lower().replace(",", "")
    return int(s) if s.isdigit() else NUM_WORDS.get(s)


def split_info(text):
    """{'old_per_new', 'label', 'reverse', 'effective', 'done', 'range'} or None."""
    m0 = SPLIT_ANY.search(text or "")
    if not m0:
        return None
    ratios = []
    for m in SPLIT_ANY.finditer(text):
        window = text[max(0, m.start() - 300): m.end() + 300]
        for i, rx in enumerate(RATIO_RES):
            for r in rx.finditer(window):
                a, b = _num(r.group(1)), _num(r.group(2))
                if not a or not b or a == b:
                    continue
                if i == 3:  # "every N shares into 1"
                    a, b = b, a
                ratios.append(b / a)  # a new shares for b old shares
        if ratios:
            break
    distinct = sorted(set(round(r, 4) for r in ratios))
    info = {"old_per_new": distinct[0] if len(distinct) == 1 else None, "range": distinct if len(distinct) > 1 else [],
            "reverse": not re.search(r"(?i)forward|subdivision", m0.group(0)) and not (distinct and max(distinct) < 1),
            "done": bool(SPLIT_DONE.search(text)),
            "effective": nearest_date(text, r"(?i)split[- ]adjusted basis|market effective date|post[- ]split basis|effective")}
    info["label"] = ratio_label(info["old_per_new"]) if info["old_per_new"] else ""
    return info


def ratio_label(old_per_new):
    if old_per_new >= 1:
        return f"1-for-{old_per_new:g}"
    return f"{1 / old_per_new:g}-for-1"


def split_line(sp):
    kind = "Reverse split" if sp["reverse"] else "Stock split"
    if sp["range"]:
        return f"🔀 {kind} — ratio range {ratio_label(min(sp['range']))} to {ratio_label(max(sp['range']))}, not set yet"
    line = f"🔀 {kind}" + (f" {sp['label']}" if sp["label"] else "")
    if sp["done"] and sp["effective"]:
        line += f" · effective {nice(sp['effective'])}"
    elif not sp["done"]:
        line += " · proposed / mentioned"
    return line


def split_events(cik, start):
    """Splits that took effect since `start`, from 8-K/6-K text: [{'date', 'old_per_new', 'label'}]."""
    docs = fts_docs(cik, ['"reverse stock split"', '"reverse share split"', '"share consolidation"',
                          '"forward split"', '"stock split"'], start, limit=15)
    events = []
    for doc in docs:
        sp = split_info(doc["text"])
        if not sp or not sp["done"] or not sp["old_per_new"]:
            continue
        when = sp["effective"] or doc["date"]
        if any(abs(e["old_per_new"] - sp["old_per_new"]) < 0.01 and
               abs((date.fromisoformat(e["date"]) - date.fromisoformat(when)).days) < 60 for e in events):
            continue
        events.append({"date": when, "old_per_new": sp["old_per_new"], "label": sp["label"]})
    return sorted(events, key=lambda e: e["date"])


def xbrl_splits(facts):
    """Splits found from restated share counts: same period reported with very different
    values in two filings. Returns [{'date': first filing after the split, 'old_per_new'}]."""
    found = []
    for tax, name in (("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
                      ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
                      ("ifrs-full", "WeightedAverageShares")):
        rows = facts.get(tax, {}).get(name, {}).get("units", {}).get("shares", [])
        periods = {}
        for r in rows:
            if "start" in r and r.get("val"):
                periods.setdefault((r["start"], r["end"]), set()).add((r["filed"], r["val"]))
        for vals in periods.values():
            vals = sorted(vals)
            for (f1, v1), (f2, v2) in zip(vals, vals[1:]):
                ratio = v1 / v2
                if f1 != f2 and (ratio >= 1.5 or ratio <= 1 / 1.5):
                    found.append({"date": f2, "old_per_new": round(ratio, 2)})
    found.sort(key=lambda e: e["date"])
    merged = []
    for e in found:
        if merged and abs(merged[-1]["old_per_new"] / e["old_per_new"] - 1) < 0.1 and \
                (date.fromisoformat(e["date"]) - date.fromisoformat(merged[-1]["date"])).days < 200:
            continue
        merged.append(e)
    return merged


# ---------- Share count history ----------
def fmt_shares(n):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:,.2f}{suf}"
    return f"{n:,.0f}"


def shares_report(t, info, max_rows=8):
    head = f"📊 Shares outstanding — {t} ({info['name']})"
    try:
        facts = sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(info['cik']):010d}.json").get("facts", {})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"{head}\n\n❔ No financial data on SEC for this company."
        raise
    rows = facts.get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    reports = {}
    for r in rows:  # one point per filing; several share classes in one filing are added up
        rep_ = reports.setdefault(r["accn"], {"filed": r["filed"], "asof": r["end"], "form": r.get("form", ""), "vals": set()})
        rep_["vals"].add((r["end"], r["val"]))
    points = sorted(({"asof": p["asof"], "form": p["form"], "filed": p["filed"],
                      "val": sum(v for _, v in p["vals"])} for p in reports.values()), key=lambda p: p["asof"])
    if not points:
        return f"{head}\n\n❔ No share count data on SEC (the company doesn't tag it). Check the latest report directly."
    points = points[-max_rows:]
    start = (date.fromisoformat(points[0]["asof"]) - timedelta(days=30)).isoformat()

    # splits: text announcements (exact dates) first, restated data fills gaps
    split_warn = ""
    try:
        splits = split_events(info["cik"], start)
    except Exception as e:
        print(f"split search failed: {e}")
        splits = []
        split_warn = "⚠️ Split search failed — splits after the latest report may be missing, so numbers can be off. Try again later."
    for x in xbrl_splits(facts):
        if x["date"] < start:
            continue
        if not any(abs(s_["old_per_new"] / x["old_per_new"] - 1) < 0.15 and
                   0 <= (date.fromisoformat(x["date"]) - date.fromisoformat(s_["date"])).days < 400 for s_ in splits):
            splits.append({"date": x["date"], "old_per_new": x["old_per_new"], "label": ratio_label(x["old_per_new"]),
                           "approx": True})
    splits.sort(key=lambda e: e["date"])

    lines, total = [], 1.0
    for i, p in enumerate(points):
        if i == 0:
            lines.append(f"{p['asof']} · {fmt_shares(p['val'])} ({p['form']})")
            continue
        prev = points[i - 1]
        between = [s_ for s_ in splits if prev["asof"] < s_["date"] <= p["asof"]]
        factor = 1.0
        for s_ in between:
            factor *= s_["old_per_new"]
            when = "around " + s_["date"] if s_.get("approx") else s_["date"]
            lines.append(f"🔀 {'Reverse split' if s_['old_per_new'] > 1 else 'Stock split'} {s_['label']} ({when}) — not dilution")
        change = p["val"] / (prev["val"] / factor) - 1
        total *= 1 + change
        flag = " ⚠️ big dilution" if change >= 0.2 else " ⚠️ new shares" if change >= 0.05 else \
            " (fewer shares — buyback/cancel)" if change <= -0.05 else ""
        lines.append(f"{p['asof']} · {fmt_shares(p['val'])} ({p['form']}) · {change:+.0%}{flag}")

    last = points[-1]
    after = [s_ for s_ in splits if s_["date"] > last["asof"]]
    est = last["val"]
    for s_ in after:
        est /= s_["old_per_new"]
        lines.append(f"🔀 {'Reverse split' if s_['old_per_new'] > 1 else 'Stock split'} {s_['label']} ({s_['date']}) — after the latest report")
    try:
        price_head, pending, summary = pending_dilution(t, info, facts, last, splits, est)
    except Exception as e:
        print(f"pending dilution failed: {e}")
        price_head, pending, summary = ["Price / pending dilution unavailable right now"], [], []
    out = [head] + ([split_warn] if split_warn else []) + \
          [f"Latest report: {fmt_shares(last['val'])} as of {last['asof']} ({last['form']})"]
    if after:
        out.append(f"After later split(s): about {fmt_shares(est)}")
    if len(points) > 1:
        out.append(f"Real change since {points[0]['asof']}: {total - 1:+.0%} (split-adjusted)")
    out += price_head + ["", "History:", "\n".join(lines)]
    out += ["", f"Pending dilution (after {last['asof']}):"] + (pending or ["Nothing found."])
    if summary:
        out += ["", "Totals:"] + summary
    out += ["", "History % = real new shares (splits adjusted out). Pending items are parsed from prospectuses — "
                "✅ stated in filings, ≈ estimate, ⚠️ can't be counted. Market caps assume today's price holds."]
    return "\n".join(out)


# ---------- Prices (Yahoo Finance chart endpoint: unofficial, delayed, may fail) ----------
def yahoo_chart(t, **params):
    sym = urllib.parse.quote(t.replace(".", "-"))
    qs = urllib.parse.urlencode(params)
    err = None
    for host in ("query1", "query2"):
        try:
            req = urllib.request.Request(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?{qs}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.load(r).get("chart", {}).get("result") or []
            if res:
                return res[0]
        except Exception as e:
            err = e
    print(f"price lookup failed for {t}: {err}")
    return None


def current_price(t):
    r = yahoo_chart(t, range="5d", interval="1d")
    p = (r or {}).get("meta", {}).get("regularMarketPrice")
    return float(p) if p else None


def close_before(t, iso):
    """Last daily close before a date. Yahoo adjusts history for later splits (caller undoes that)."""
    d = date.fromisoformat(iso)
    epoch = lambda x: (x - date(1970, 1, 1)).days * 86400
    r = yahoo_chart(t, period1=epoch(d - timedelta(days=12)), period2=epoch(d), interval="1d")
    if not r:
        return None
    closes = (r.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    best = None
    for ts, c in zip(r.get("timestamp") or [], closes):
        if c and time.strftime("%Y-%m-%d", time.gmtime(ts)) < iso:
            best = float(c)
    return best


def usd(x):
    if x is None:
        return "?"
    if x >= 1e6:
        return f"${x / 1e6:,.2f}M" if x < 1e9 else f"${x / 1e9:,.2f}B"
    if x >= 1000:
        return f"${x:,.0f}"
    return f"${x:,.2f}" if x >= 1 else f"${x:.4f}".rstrip("0").rstrip(".")


# ---------- Pending dilution ----------
_CNT = r"(\d{1,3}(?:,\d{3})+|\d{4,})"
TOXIC = [
    ("Reset / alternative cashless warrants (count can grow if the price falls)",
     r"alternative cashless exercise|exercise price\W+(?:\w+\W+){0,12}?reset|reset\W+(?:\w+\W+){0,6}?exercise price"),
    ("Variable-price convertible (more shares the lower the price goes)",
     r"variable conversion price|conversion price\W+(?:\w+\W+){0,20}?(lowest|discount|percent of the)"),
    ("Equity line (sells shares to a fund over time at a discount)",
     r"standby equity purchase agreement|equity line|equity purchase facility|(common stock|ordinary share|share) purchase agreement\W+(?:\w+\W+){0,10}?(commitment|fund|capital)"),
]


def _cnt(s):
    return int(s.replace(",", ""))


def parse_offering(text):
    cover = text[:20000]
    out = {"kind": "unknown", "shares": 0, "prefunded": 0, "over": 0, "warrants": 0, "wprice": None,
           "price": None, "amount": None}
    m = re.search(r"(?i)(?:public )?offering price\W+(?:\w+\W+){0,4}?(?:US)?\$\s?(\d+(?:\.\d+)?)(?![\d,])", cover) or \
        re.search(r"(?i)(?:US)?\$\s?(\d+(?:\.\d+)?)(?![\d,])\s+per\s+(?:share|unit|ordinary share|share of common stock)", cover)
    out["price"] = float(m.group(1)) if m else None
    if re.search(r"(?i)selling (stock|share)holders?", cover) and re.search(r"(?i)will not receive any (of the )?proceeds", cover):
        out["kind"] = "resale"
        return out
    if re.search(r"(?i)at[- ]the[- ]market|sales agreement|equity distribution agreement|controlled equity offering", cover):
        out["kind"] = "atm"
        m = re.search(r"(?i)(?:aggregate offering price of )?up to (?:US)?\$\s?([\d,.]+)\s*(million|billion)?", cover)
        if m:
            amt = float(m.group(1).replace(",", ""))
            out["amount"] = amt * (1e6 if (m.group(2) or "").lower() == "million" else 1e9 if m.group(2) else 1)
        return out
    m = re.search(rf"(?i)\b{_CNT}\s+(?:shares|ordinary shares|common shares|class a ordinary shares|units|american depositary shares)\b", cover)
    if m:
        out["kind"], out["shares"] = "offering", _cnt(m.group(1))
    m = re.search(rf"(?i)\b{_CNT}\s+pre-?funded warrants", cover)
    out["prefunded"] = _cnt(m.group(1)) if m else 0
    m = re.search(rf"(?i)(?:option|over-?allotment)[^.]{{0,160}}?additional\s+{_CNT}", cover)
    out["over"] = _cnt(m.group(1)) if m else 0
    m = re.search(rf"(?i)\b{_CNT}\s+(?:series \S+ |common |ordinary |share |class \S+ )?warrants", cover)
    out["warrants"] = _cnt(m.group(1)) if m else 0  # "pre-funded warrants" never matches this pattern
    m = re.search(r"(?i)exercise price of (?:US)?\$\s?(\d+(?:\.\d+)?)", cover)
    out["wprice"] = float(m.group(1)) if m else None
    if out["kind"] == "unknown" and out["prefunded"]:
        out["kind"] = "offering"
    return out


def latest_warrants(facts):
    """(count, exercise_price, as_of) from the newest report, or None."""
    def newest(name, unit):
        rows = facts.get("us-gaap", {}).get(name, {}).get("units", {}).get(unit, [])
        if not rows:
            return None
        end = max(r["end"] for r in rows)
        filed = max(r["filed"] for r in rows if r["end"] == end)
        vals = {r["val"] for r in rows if r["end"] == end and r["filed"] == filed}
        return end, vals
    cnt = newest("ClassOfWarrantOrRightOutstanding", "shares")
    if not cnt:
        return None
    px = newest("ClassOfWarrantOrRightExercisePriceOfWarrantsOrRights1", "USD/shares")
    price = min(px[1]) if px and px[0] == cnt[0] and len(px[1]) == 1 else None
    return sum(cnt[1]), price, cnt[0]


def split_factor(splits, after, until="9999-12-31"):
    f = 1.0
    for s_ in splits:
        if after < s_["date"] <= until:
            f *= s_["old_per_new"]
    return f


def pending_dilution(t, info, facts, last, splits, est):
    """Lines describing dilution waiting to happen after the latest report, plus market cap estimates."""
    price = current_price(t)
    lines, now, extra, last_offer_px, toxic_seen = [], est, 0.0, None, set()
    filings = [f for f in recent_filings(info["cik"]) if f["form"].startswith("424B") and f["date"] > last["asof"]][:6]
    for f in reversed(filings):  # oldest first
        try:
            text = " ".join(html_lines(sec_get(filing_url(info["cik"], f))))
        except Exception as e:
            print(f"prospectus failed {f['acc']}: {e}")
            continue
        for label, rx in TOXIC:
            if re.search("(?i)" + rx, text):
                toxic_seen.add((label, f["date"]))
        o = parse_offering(text)
        k = split_factor(splits, f["date"])  # splits after this offering
        tag = f"{f['date']} ({f['form']})"
        if o["kind"] == "resale":
            lines.append(f"• {tag} resale by existing holders — no new shares (unless it covers warrant exercises)")
        elif o["kind"] == "atm":
            if o["amount"] and price:
                n = o["amount"] / price
                extra += n
                lines.append(f"≈ {tag} ATM: up to {usd(o['amount'])} → ~{fmt_shares(n)} shares at {usd(price)} "
                             f"(capacity; some may already be sold, more if price drops)")
            else:
                lines.append(f"≈ {tag} ATM: up to {usd(o['amount'])} — share count depends on price")
        elif o["kind"] == "offering":
            sh, pf, ov, wa = (x / k for x in (o["shares"], o["prefunded"], o["over"], o["warrants"]))
            px = o["price"] * k if o["price"] else None
            now += sh + pf
            extra += ov + wa
            last_offer_px = px or last_offer_px
            parts = [f"{fmt_shares(sh)} shares"] + ([f"{fmt_shares(pf)} pre-funded"] if pf else [])
            line = f"✅ {tag} offering" + (f" @ {usd(px)}" if px else "") + ": " + " + ".join(parts) + \
                   f" → {(sh + pf) / est:+.0%}"
            lines.append(line)
            if ov:
                lines.append(f"   + up to {fmt_shares(ov)} over-allotment")
            if wa:
                wp = f" @ {usd(o['wprice'] * k)}" if o["wprice"] else ""
                lines.append(f"   + {fmt_shares(wa)} warrants{wp}")
            if px:
                prior = close_before(t, f["date"])
                if prior:
                    prior /= split_factor(splits, f["date"])  # undo Yahoo's split adjustment
                    prior *= k  # back to today's share basis
                    lines.append(f"   Priced {1 - px / prior:.0%} below the prior close ({usd(prior)})" if px < prior
                                 else f"   Priced at/above the prior close ({usd(prior)})")
            if k != 1:
                lines.append("   (adjusted for later split)")
        else:
            lines.append(f"❔ {tag} — couldn't read the prospectus cover, open it to check")

    w = latest_warrants(facts)
    if w and w[0]:
        cnt, wpx, asof = w
        k = split_factor(splits, asof)
        cnt, wpx = cnt / k, (wpx * k if wpx else None)
        extra += cnt
        money_ = ""
        if wpx and price:
            money_ = " · in the money (likely exercised)" if wpx <= price else " · out of the money for now"
        lines.append(f"✅ Warrants outstanding (as of {asof}{', split-adjusted' if k != 1 else ''}): "
                     f"{fmt_shares(cnt)}" + (f" @ {usd(wpx)}" if wpx else "") + money_)

    try:
        docs = fts_docs(info["cik"], ['"alternative cashless exercise"', '"standby equity purchase agreement"',
                                      '"equity purchase facility"', '"variable conversion price"'],
                        (date.today() - timedelta(days=365)).isoformat(), limit=6)
        for doc in docs:
            for label, rx in TOXIC:
                if re.search("(?i)" + rx, doc["text"]):
                    toxic_seen.add((label, doc["date"]))
    except Exception as e:
        print(f"toxic search failed: {e}")
        lines.append("❔ Couldn't search for toxic financing terms right now — try again later")
    for label in sorted({l for l, _ in toxic_seen}):
        when = max(d for l, d in toxic_seen if l == label)
        lines.append(f"⚠️ {label} — last mentioned {when}; not counted, can't be predicted")

    head = []
    if price:
        head.append(f"Price: {usd(price)} (Yahoo, delayed) · Market cap now: {usd(price * now)}")
    else:
        head.append("Price: unavailable right now (Yahoo lookup failed) — share counts only")
    total = now + extra
    summary = [f"Now (est.): {fmt_shares(now)} shares" + (f" → {usd(price * now)}" if price else "")]
    if extra:
        caps = []
        if price:
            caps.append(f"{usd(price * total)} at {usd(price)}")
        if last_offer_px:
            caps.append(f"{usd(last_offer_px * total)} at last offering price {usd(last_offer_px)}")
        summary.append(f"If all of the above turns into shares: {fmt_shares(total)} ({total / now - 1:+.0%} vs now)"
                       + (" → " + " · ".join(caps) if caps else ""))
    return head, lines, summary


# ---------- Listing deficiencies / delisting ----------
LISTING_TRIGGER = re.compile(
    r"(?i)listing qualifications|deficiency (letter|notice|notification)|regain(ed)? compliance|delisting determination"
    r"|hearings? panel|minimum bid price|bid price requirement|continued listing (requirement|standard|rule|criteria)"
    r"|listing rule 5\d{3}|section 1003|notice of (non-?compliance|delisting)"
    r"|determined to delist|determination to delist|suspended from trading|trading .{0,40}will be suspended"
)
HYPOTHETICAL = re.compile(r"(?i)\b(if|would|may|might|could|in the event|should|whether)\b")
DETERMINATION = re.compile(
    r"(?i)delisting determination|determined to delist|determination to delist|staff determination"
    r"|will be suspended|trading\W+(?:\w+\W+){0,8}?(was|has been|will be) suspended|suspended from trading"
)


def sentences(text):
    return re.split(r"(?<=[.;])\s+", text)


def actual(rx, text):
    """True if some sentence states rx as fact, not as an 'if ... then' warning."""
    return any(re.search(rx, sn) and not HYPOTHETICAL.search(sn) for sn in sentences(text))


LISTING_KINDS = [
    ("Bid price ≤ $0.10", r"(closed|closing bid price)\W+(?:\w+\W+){0,6}?\$0\.10 or (less|below|lower)"),
    ("Bid price below $1", r"bid price"),
    ("Stockholders' equity", r"(stockholders|shareholders)['’]? equity"),
    ("Market value of listed securities", r"market value of listed securities|\bMVLS\b"),
    ("Market value of public float", r"market value of (unrestricted )?publicly held shares|\bMVPHS\b"),
    ("Late filing", r"periodic (financial )?(report|filing)|timely fil\w+|delinquen\w+|(failed|failure) to file"),
    ("Holders / public float", r"round lot holders|publicly held shares requirement|total holders"),
    ("Board / audit committee", r"audit committee|independent directors?|majority independent"),
    ("Annual meeting", r"annual (shareholders['’]? |stockholders['’]? |general )?meeting requirement|hold an annual meeting"),
]
RULE_HINTS = {
    "Bid price ≤ $0.10": "No grace period: closing at $0.10 or less for 10 business days means immediate delisting proceedings.",
    "Bid price below $1": "Fix: close at $1.00+ for 10 straight business days before the deadline (usually 180 days; Capital Market may get a 2nd 180).",
    "Stockholders' equity": "Fix: $2.5M+ equity (Capital Market), or $35M market value / $500K net income. Needs an accepted plan.",
    "Market value of listed securities": "Fix: market value back above the minimum for 10 straight business days within 180 days.",
    "Market value of public float": "Fix: public float value back above the minimum for 10 straight business days within 180 days.",
    "Late filing": "Fix: file the late report. Nasdaq usually gives 60 days for a plan, then up to 180 days from the due date.",
}


def listing_event(text):
    if not text or not LISTING_TRIGGER.search(text):
        return None
    kinds = [label for label, rx in LISTING_KINDS
             if (actual("(?i)" + rx, text) if label == "Bid price ≤ $0.10" else re.search("(?i)" + rx, text))]
    if "Bid price ≤ $0.10" in kinds and "Bid price below $1" in kinds:
        kinds.remove("Bid price below $1")
    ev = {"kinds": kinds or ["Listing standard"], "deadline": "", "suspend": "", "hearing": ""}
    if re.search(r"(?i)regained compliance|is (now )?in compliance with|has regained", text):
        ev["status"] = "regained"
    elif any(DETERMINATION.search(sn) and not HYPOTHETICAL.search(sn) for sn in sentences(text)):
        ev["status"] = "determination"
        ev["suspend"] = nearest_date(text, r"(?i)suspend\w*")
        ev["hearing"] = nearest_date(text, r"(?i)hearing (date|is scheduled|has been scheduled|scheduled for|will be held|on)", before=0)
        if re.search(r"(?i)panel\W+(?:\w+\W+){0,8}?grant\w*", text):
            ev["status"] = "extension"
            found = dates_near(text, r"(?i)grant\w*|extension|until")
            ev["deadline"] = max(found) if found else ""
    else:
        ev["status"] = "notice"
        found = dates_near(text, r"(?i)regain compliance|compliance period|additional 180|until")
        ev["deadline"] = max(found) if found else ""  # second compliance periods push the deadline out
        if not kinds and not ev["deadline"]:
            return None  # passing mention (e.g. in an earnings release), not an actual notice
    return ev


def listing_lines(ev, hints=True):
    kinds = ", ".join(ev["kinds"])
    if ev["status"] == "regained":
        return [f"✅ Listing: regained compliance ({kinds})"]
    if ev["status"] == "determination":
        out = [f"🚨🚨 Delisting determination — {kinds}"]
        if ev["suspend"]:
            out.append(f"   Trading suspension: {nice(ev['suspend'])} (unless an appeal/hearing stops it)")
        if ev["hearing"]:
            out.append(f"   Hearing: {nice(ev['hearing'])}")
        return out
    if ev["status"] == "extension":
        return [f"🚨 Hearing panel gave more time — {kinds}" + (f" · until {nice(ev['deadline'])}" if ev["deadline"] else "")]
    out = [f"🚨 Listing deficiency notice — {kinds}" + (f" · deadline {nice(ev['deadline'])}" if ev["deadline"] else "")]
    if hints:
        out += [f"   {RULE_HINTS[k]}" for k in ev["kinds"] if k in RULE_HINTS][:1]
    return out


NASDAQ_RULES = [
    "• Below $1.00 for 30 business days → notice, 180 days to fix (10 straight days at $1+).",
    "• $0.10 or less for 10 business days → immediate delisting, no grace period.",
    "• Below $1 again within 1 year of a reverse split → immediate delisting, no grace period.",
    "• Reverse splits totaling 250:1+ over 2 years, then below $1 → immediate delisting.",
    "• Still failing after 360 days → trading suspended, even while appealing.",
    "• Capital Market also needs: $2.5M equity (or $35M market value / $500K net income), "
    "$1M public float value, 500K public shares, 300 round-lot holders, filings on time.",
]
NYSE_RULES = [
    "• Below $1.00 average over 30 trading days → 6 months to fix.",
    "• Reverse split in the past year, or 200:1+ total over 2 years, then below $1 → immediate delisting.",
    "• Also needs minimum market cap / equity and filings on time (NYSE American rules differ).",
]


def delisting_report(t, info):
    exch = info.get("exchange", "?")
    head = f"🏛 Listing status — {t} · {exch}\n{info['name']}"
    today = date.today()
    start2y = (today - timedelta(days=730)).isoformat()
    try:
        docs = fts_docs(info["cik"], ['"regain compliance"', '"regained compliance"', '"delisting determination"',
                                       '"minimum bid price"', '"Hearings Panel"', '"Listing Qualifications"'], start2y, limit=12)
    except SearchFailed as e:
        print(e)
        return f"{head}\n\n❔ SEC filing search failed right now, so listing status can't be checked. Try again later."
    events = []
    for doc in docs:
        ev = listing_event(doc["text"])
        if ev:
            ev.update(date=doc["date"], form=doc["form"], url=doc["url"])
            events.append(ev)
    events.sort(key=lambda e: e["date"])

    # latest status per deficiency type
    latest = {}
    for ev in events:
        for k in ev["kinds"]:
            latest[k] = ev
    open_ = {k: ev for k, ev in latest.items() if ev["status"] != "regained"}

    form25 = [f for f in recent_filings(info["cik"]) if f["form"].startswith("25") and f["date"] >= start2y]
    if form25:
        eff = (date.fromisoformat(form25[0]["date"]) + timedelta(days=10)).isoformat()
        verdict = f"🚨🚨 DELISTING FILED ({form25[0]['form']}, {form25[0]['date']}) — removal around {nice(eff)}"
    elif any(ev["status"] == "determination" for ev in open_.values()):
        verdict = "🚨🚨 DELISTING DETERMINATION — the exchange has decided to delist (may be under appeal)"
    elif open_:
        verdict = "🚨 OPEN DEFICIENCY — not in compliance right now"
    elif events:
        verdict = f"✅ No open deficiency (last issue resolved {events[-1]['date']})"
    else:
        verdict = "✅ No listing deficiency notices found in the last 2 years"

    out = [head, "", verdict]
    for k, ev in open_.items():
        line = f"• {k}: {ev['status']} since {ev['date']}"
        if ev["deadline"]:
            days = (date.fromisoformat(ev["deadline"]) - today).days
            line += f" · deadline {nice(ev['deadline'])} ({days} days left)" if days >= 0 else f" · deadline passed {ev['deadline']}"
        if ev["suspend"]:
            line += f" · suspension {nice(ev['suspend'])}"
        out.append(line)
        if k in RULE_HINTS:
            out.append(f"  {RULE_HINTS[k]}")

    # reverse split risk (affects whether a future bid-price failure gets any grace period)
    try:
        splits = [s_ for s_ in split_events(info["cik"], start2y) if s_["old_per_new"] > 1]
    except Exception as e:
        print(f"split search failed: {e}")
        splits = []
        out += ["", "❔ Reverse split history couldn't be searched right now — the 1-year / 250:1 risks aren't checked."]
    if splits:
        cum = 1.0
        for s_ in splits:
            cum *= s_["old_per_new"]
        out += ["", "Reverse splits (2 yrs): " + ", ".join(f"{s_['label']} ({s_['date']})" for s_ in splits)
                + f" · total {cum:g}:1"]
        last_rs = splits[-1]["date"]
        if (today - date.fromisoformat(last_rs)).days < 365:
            out.append(f"⚠️ Reverse split within the last year — falling below $1 again before "
                       f"{(date.fromisoformat(last_rs) + timedelta(days=365)).isoformat()} means immediate delisting.")
        limit = 200 if exch.upper() == "NYSE" else 250
        if cum >= limit:
            out.append(f"🚨 {cum:g}:1 in reverse splits over 2 years — any new bid-price failure means immediate delisting.")

    if events:
        out += ["", "History (2 yrs):"]
        for ev in reversed(events[-6:]):
            label = {"regained": "✅ regained", "notice": "🚨 notice", "determination": "🚨🚨 determination",
                     "extension": "⏳ panel extension"}[ev["status"]]
            out.append(f"{ev['date']} · {ev['form']} · {label} · {', '.join(ev['kinds'])}")

    rules = NASDAQ_RULES if exch.lower() == "nasdaq" else NYSE_RULES if exch.upper() == "NYSE" else []
    if rules:
        out += ["", f"Key {exch} rules:"] + rules
    out += ["", "Found by searching the company's 8-K/6-K filings. Price-based problems only show up once the "
                "company discloses the notice (usually within 4 business days)."]
    return "\n".join(out)


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

    elif cmd in ("/shares", "/delisting") and args:
        t = args[0]
        info = watch.get(t) or lookup(t)
        if not info:
            send(f"{t}: not found on SEC — check the ticker")
            return
        if "exchange" not in info:
            info = {**info, "exchange": (lookup(t) or {}).get("exchange", "?")}
        send(shares_report(t, info) if cmd == "/shares" else delisting_report(t, info))

    elif cmd in ("/dividends", "/dividend") and args:
        t = args[0]
        info = watch.get(t) or lookup(t)
        if not info:
            send(f"{t}: not found on SEC — check the ticker")
            return
        send(dividend_report(t, info))

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
            try:
                block = filing_block(w["cik"], f)
            except Exception as e:  # never let parsing stop alerts or the state save
                print(f"{t}: filing_block failed for {f['acc']}: {e}")
                block = f"{f['form']} · {f['date']}\n(couldn't summarize — open the filing)"
            send(f"🔔 New SEC filing — {t} ({w['name']})\n\n{block}\n\n{filing_url(w['cik'], f)}")
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
