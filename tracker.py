"""Investment Tracker: local server (Python standard library only)."""
import math
import urllib.error
import storage

import base64, bisect, csv, datetime, hashlib, io, json, os, re, socket, struct, sys, threading, time, urllib.parse, urllib.request, uuid, webbrowser, zipfile
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8765
DB, SNAP = HERE / "holdings.json", HERE / "history.json"
CACHE2Y = HERE / "chart2y.json"
BACKUPS = HERE / "backups"
METALS = {"gold": "XAU", "silver": "XAG"}
TYPES = {"stock", "mf", "crypto", "fd", "manual", *METALS}
XL_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
UA = {"User-Agent": "Mozilla/5.0"}
cache = {}
LOG = HERE / "tracker.log"
LOCK = storage.LOCK
MAX_FILE = 5 * 1024 * 1024
MAX_BODY = 7 * 1024 * 1024

# When started with pythonw.exe (no console window) there is nowhere to print errors,
# so send them to tracker.log instead of crashing.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(LOG, "a", encoding="utf-8", buffering=1))

def fetch(url, **params):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
        return json.load(r)

def read(path, default):
    return storage.read(path, default, BACKUPS)


def write(path, data, indent=None):
    storage.write(path, data, BACKUPS, indent, backup=path != CACHE2Y)


def nonstock_quote(kind, sym):
    key = ("quote", kind, sym)
    if key in cache and time.time() - cache[key][0] < 600:
        return cache[key][1]
    if kind == "mf":
        code = urllib.parse.quote(sym, safe="")
        nav = fetch(f"https://api.mfapi.in/mf/{code}/latest")["data"][0]
        value = float(nav["nav"])
        quoted_at = datetime.datetime.strptime(nav["date"], "%d-%m-%Y").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    elif kind == "crypto":
        quote = fetch("https://api.coingecko.com/api/v3/simple/price", ids=sym,
                      vs_currencies="inr", include_last_updated_at="true")[sym]
        value, quoted_at = quote["inr"], quote.get("last_updated_at")
    else:
        raise ValueError("Unsupported quote type")
    result = {"price": value, "native_price": value, "currency": "INR", "fx": 1.0,
              "quoted_at": quoted_at}
    cache[key] = (time.time(), result)
    return result


def fx_rate_on(cur, date=None):
    """INR per unit of currency on a date (or latest when date is omitted)."""
    if not cur or cur == "INR":
        return 1.0
    if not re.fullmatch(r"[A-Z]{3}", cur):
        raise ValueError("Invalid currency code")
    if not date:
        return fx_rate(cur)
    d = datetime.date.fromisoformat(str(date))
    start = int(datetime.datetime.combine(d, datetime.time.min, datetime.timezone.utc).timestamp())
    end = start + 86400
    start -= 7 * 86400
    r = fetch("https://query1.finance.yahoo.com/v8/finance/chart/" +
              urllib.parse.quote(f"{cur}INR=X"), period1=start, period2=end, interval="1d")
    res = r["chart"]["result"][0]
    closes = daily_closes(res)
    dates = [day for day, rate in closes.items() if day <= d.isoformat() and rate > 0]
    if not dates:
        raise ValueError("historical FX rate unavailable")
    return closes[max(dates)]

def metal_quote(kind):
    """Indicative international spot price in INR per gram of fine metal."""
    symbol = METALS[kind]
    key = ("spot-metal", symbol)
    if key in cache and time.time() - cache[key][0] < 600:
        return cache[key][1]
    quote = fetch(f"https://api.gold-api.com/price/{symbol}/INR")
    price = number(quote["price"], "spot price") / 31.1034768
    if price <= 0:
        raise ValueError("Invalid metal quote")
    stamp = quote.get("updatedAt")
    quoted_at = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() if stamp else None
    result = {"price": price, "currency": "INR", "quoted_at": quoted_at, "source": "Gold API"}
    cache[key] = (time.time(), result)
    return result


def price_info(kind, sym):
    """Return current unit price in INR and its original market currency."""
    if kind in METALS:
        return metal_quote(kind)
    if kind != "stock":
        return nonstock_quote(kind, sym)
    key = ("price-info", sym)
    if key in cache and time.time() - cache[key][0] < 600:
        return cache[key][1]
    q = urllib.parse.quote(sym, safe="")
    meta = fetch("https://query1.finance.yahoo.com/v8/finance/chart/" + q)["chart"]["result"][0]["meta"]
    native = float(meta["regularMarketPrice"])
    currency = meta.get("currency", "INR")
    fx = fx_rate(currency)
    result = {"price": native * fx, "native_price": native, "currency": currency, "fx": fx, "quoted_at": meta.get("regularMarketTime")}
    cache[key] = (time.time(), result)
    return result

def search(kind, q):
    if len(q) < 2:
        return []
    try:
        if kind == "stock":
            r = fetch("https://query1.finance.yahoo.com/v1/finance/search", q=q, quotesCount=8, newsCount=0)
            return [{"symbol": x["symbol"], "name": x.get("shortname") or x.get("longname") or x["symbol"],
                     "note": f'{x["symbol"]} · {x.get("exchDisp", "")}'}
                    for x in r.get("quotes", []) if x.get("quoteType") in ("EQUITY", "ETF")]
        if kind == "mf":
            r = fetch("https://api.mfapi.in/mf/search", q=q)
            return [{"symbol": str(x["schemeCode"]), "name": x["schemeName"], "note": f'code {x["schemeCode"]}'} for x in r[:8]]
        if kind == "crypto":
            r = fetch("https://api.coingecko.com/api/v3/search", query=q)
            return [{"symbol": x["id"], "name": x["name"], "note": x["symbol"].upper()} for x in r.get("coins", [])[:8]]
    except Exception:
        pass
    return []

def fx_rate(cur):
    """INR per 1 unit of `cur` (for non-INR listings like US stocks)."""
    if not cur or cur == "INR":
        return 1.0
    key = ("fx", cur)
    if key in cache and time.time() - cache[key][0] < 86400:
        return cache[key][1]
    p = fetch("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(f"{cur}INR=X")
              )["chart"]["result"][0]["meta"]["regularMarketPrice"]
    cache[key] = (time.time(), p)
    return p

def daily_closes(result):
    timestamps = result.get("timestamp") or []
    closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    return {datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date().isoformat(): float(c)
            for t, c in zip(timestamps, closes) if c is not None and math.isfinite(c)}


def fx_history(currency, range_name):
    key = ("fx-history", currency, range_name)
    if key in cache and time.time() - cache[key][0] < 3600:
        return cache[key][1]
    result = fetch("https://query1.finance.yahoo.com/v8/finance/chart/" +
                   urllib.parse.quote(currency + "INR=X"), range=range_name, interval="1d")
    rates = daily_closes(result["chart"]["result"][0])
    rates = {day: rate for day, rate in rates.items() if rate > 0}
    if not rates:
        raise ValueError("historical FX unavailable")
    cache[key] = (time.time(), rates)
    return rates


def stock_closes_in_inr(result, range_name):
    closes = daily_closes(result)
    currency = result["meta"].get("currency") or "INR"
    if currency == "INR":
        return closes
    rates = fx_history(currency, range_name)
    days = sorted(rates)
    converted = {}
    for day, close in closes.items():
        i = bisect.bisect_right(days, day) - 1
        if i >= 0 and (datetime.date.fromisoformat(day) - datetime.date.fromisoformat(days[i])).days <= 7:
            converted[day] = close * rates[days[i]]
    return converted


def hist_prices(kind, sym):
    """Historical weekly prices for the past ~2 years: {iso-date: price-INR}."""
    key = ("hist", kind, sym)
    if key in cache and time.time() - cache[key][0] < 3600:
        return cache[key][1]
    q = urllib.parse.quote(sym, safe="")
    out = {}
    if kind == "stock":
        r = fetch("https://query1.finance.yahoo.com/v8/finance/chart/" + q, range="2y", interval="1wk")
        res = r["chart"]["result"][0]
        cache[("series-currency", sym)] = (time.time(), res["meta"].get("currency") or "INR")
        out = stock_closes_in_inr(res, "2y")
    elif kind == "mf":
        r = fetch(f"https://api.mfapi.in/mf/{q}")          # full NAV history
        for d in r.get("data", []):
            try:
                dd, mm, yy = str(d["date"]).split("-")
                out[f"{yy}-{mm}-{dd}"] = float(d["nav"])
            except Exception:
                pass
    elif kind == "crypto":
        now = int(time.time())
        r = fetch(f"https://api.coingecko.com/api/v3/coins/{q}/market_chart/range",
                  **{"vs_currency": "inr", "from": now - int(2 * 365.25 * 86400), "to": now})
        for t, p in r.get("prices", []):
            out[datetime.date.fromtimestamp(t / 1000).isoformat()] = p
    cache[key] = (time.time(), out)
    return out

def series(kind, sym, days):
    """Closing prices for the past `days` days: {iso-date: price-INR}."""
    days = max(1, min(days, 730))
    key = ("series", kind, sym, days)
    if key in cache and time.time() - cache[key][0] < 600:
        return cache[key][1]
    q = urllib.parse.quote(sym, safe="")
    out = {}
    if kind == "stock":
        rng = "1mo" if days <= 31 else "6mo" if days <= 183 else "1y" if days <= 366 else "2y"
        r = fetch("https://query1.finance.yahoo.com/v8/finance/chart/" + q,
                  range=rng, interval="1d" if days <= 31 else "1wk")
        res = r["chart"]["result"][0]
        out = stock_closes_in_inr(res, rng)
    elif kind == "mf":
        cut = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        for d in fetch(f"https://api.mfapi.in/mf/{q}").get("data", []):
            try:
                dd, mm, yy = str(d["date"]).split("-")
                if f"{yy}-{mm}-{dd}" >= cut:
                    out[f"{yy}-{mm}-{dd}"] = float(d["nav"])
            except Exception:
                pass
    elif kind == "crypto":
        r = fetch(f"https://api.coingecko.com/api/v3/coins/{q}/market_chart",
                  **{"vs_currency": "inr", "days": min(days, 365), "interval": "daily"})
        for t, p in r.get("prices", []):
            out[datetime.date.fromtimestamp(t / 1000).isoformat()] = round(p, 4)
    today = datetime.date.today()
    cut = (today - datetime.timedelta(days=days)).isoformat()
    out = {date: value for date, value in out.items() if cut <= date <= today.isoformat()}
    cache[key] = (time.time(), out)
    return out

def benchmark_series():
    """Two-year weekly Nifty 50 index history for optional normalized comparison."""
    r = fetch("https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI", range="2y", interval="1wk")
    res = r["chart"]["result"][0]
    ts = res.get("timestamp") or []
    cl = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    pairs = [(datetime.date.fromtimestamp(t).isoformat(), float(v))
             for t, v in zip(ts, cl) if v is not None]
    return {"dates": [x[0] for x in pairs], "values": [x[1] for x in pairs]}

def chart_date(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def chart_series():
    """Estimate historical values and cost basis for positions open on each date."""
    holdings = read(DB, [])
    today = datetime.date.today()
    start = today - datetime.timedelta(days=730)
    key = hashlib.sha256(json.dumps(holdings, sort_keys=True).encode()).hexdigest()
    cached = read(CACHE2Y, {})
    if (cached.get("version") != 3 or cached.get("hash") != key
            or cached.get("day") != today.isoformat() or time.time() - cached.get("t", 0) > 3600):
        positions = []
        dates = {start + datetime.timedelta(days=7 * i) for i in range(105)} | {today}
        prices = {}
        estimated = False
        for holding in holdings:
            kind = holding.get("type")
            trades = holding.get("transactions")
            if trades:
                _, values = historical_position(holding, prices)
                trade_days = sorted({trade["date"] for trade in trades})
                for index, trade_day in enumerate(trade_days):
                    opened = chart_date(trade_day)
                    closed = chart_date(trade_days[index + 1]) if index + 1 < len(trade_days) else None
                    if closed and closed <= start:
                        continue
                    units, basis, _, _ = equity_state([trade for trade in trades if trade["date"] <= trade_day])
                    if start <= opened <= today:
                        dates.add(opened)
                    if units:
                        positions.append(({**holding, "units": units}, opened, closed, basis, values))
                continue
            opened = chart_date(holding.get("start" if kind == "fd" else "date"))
            closed = chart_date(holding.get("closed_date"))
            if (closed and closed <= start) or (opened and opened > today):
                continue
            dates.update(d for d in (opened, closed) if d and start <= d <= today)
            cost, values = historical_position(holding, prices)
            estimated |= opened is None or kind == "manual"
            positions.append((holding, opened or start, closed, cost, values))
        points = {}
        for day in sorted(dates):
            invested, value = 0, 0
            for holding, opened, closed, cost, values in positions:
                if day < opened or (closed and day >= closed):
                    continue
                amount = historical_value(holding, opened, day, values)
                invested = None if invested is None or cost is None else invested + cost
                value = None if value is None or amount is None else value + amount
            points[day.isoformat()] = [invested, value]
        cached = {"version": 3, "hash": key, "day": today.isoformat(), "t": time.time(),
                  "points": points, "estimated": estimated}
        write(CACHE2Y, cached)
    points = cached["points"]
    # Add an exact cost basis to saved snapshot dates using the most recent
    # reconstructed point. Every recorded acquisition and closure is a point.
    dates = sorted(points)
    result = dict(points)
    for date, value in read(SNAP, {}).items():
        if dates[0] <= date <= dates[-1] and isinstance(value, (int, float)) and math.isfinite(value):
            index = bisect.bisect_right(dates, date) - 1
            result[date] = [points[dates[index]][0], value]
    dates = sorted(result)
    return {"dates": dates, "invested": [result[d][0] for d in dates],
            "values": [result[d][1] for d in dates], "estimated": cached["estimated"]}


def historical_position(holding, prices):
    kind = holding.get("type")
    if kind == "fd":
        return float(holding.get("principal") or 0), None
    if kind == "manual":
        return float(holding.get("principal", holding.get("value", 0))), None
    if kind in METALS:
        return float(holding.get("units") or 0) * float(holding.get("buy") or 0), ([], {})
    symbol = str(holding.get("symbol", ""))
    key = (kind, symbol)
    if key not in prices:
        try:
            quotes = hist_prices(kind, symbol)
            prices[key] = (sorted(quotes), quotes)
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            prices[key] = ([], {})
    currency = holding.get("currency") or cache.get(("series-currency", symbol), (0, None))[1]
    fx = holding.get("fx_buy")
    if kind == "stock" and not currency:
        quote_key = ("currency", symbol)
        if quote_key not in prices:
            try:
                prices[quote_key] = price_info(kind, symbol)["currency"]
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                prices[quote_key] = None
        currency = prices[quote_key]
    if kind == "stock" and currency and currency != "INR" and not fx and holding.get("date"):
        rate_key = ("purchase-fx", currency, holding["date"])
        if rate_key not in prices:
            try:
                prices[rate_key] = fx_rate_on(currency, holding["date"])
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                prices[rate_key] = None
        fx = prices[rate_key]
    if kind == "stock" and currency != "INR" and not fx:
        return None, prices[key]
    return float(holding.get("units") or 0) * float(holding.get("buy") or 0) * (fx or 1), prices[key]


def historical_value(holding, opened, day, values):
    kind = holding.get("type")
    if kind == "fd":
        maturity = chart_date(holding.get("maturity")) or day
        years = max(0, (min(day, maturity) - opened).days / 365.25)
        rate = float(holding.get("rate") or 0) / 100
        frequency = float(holding.get("freq") or 4)
        return float(holding.get("principal") or 0) * (1 + rate / frequency) ** (frequency * years)
    if kind == "manual":
        return float(holding.get("value", holding.get("principal", 0)))
    dates, quotes = values
    index = bisect.bisect_right(dates, day.isoformat()) - 1
    if index < 0 or (day - datetime.date.fromisoformat(dates[index])).days > 14:
        return None
    return float(holding.get("units") or 0) * quotes[dates[index]]


# ---------------- import (xlsx / csv, stdlib only) ----------------

NUMBER_FIELDS = ("units", "buy", "principal", "rate", "value", "closed_value", "fx_buy", "freq")
DATE_FIELDS = ("date", "start", "maturity", "closed_date")


def number(value, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be a number") from None
    if not math.isfinite(result) or abs(result) > 1e18:
        raise ValueError(f"{field} is outside the supported range")
    return result


def validate_holding(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str) or raw["type"] not in TYPES:
        raise ValueError("Choose a supported investment type")
    typ = raw["type"]
    rec = {"type": typ}
    for field, limit in {"name": 200, "symbol": 100, "currency": 3, "tax_class": 20}.items():
        value = raw.get(field, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"Invalid {field}")
        if value.strip():
            rec[field] = value.strip()
    if "name" not in rec:
        raise ValueError("Missing name")
    if "currency" in rec and not re.fullmatch(r"[A-Z]{3}", rec["currency"]):
        raise ValueError("Invalid currency code")
    if "symbol" in rec and not re.fullmatch(r"[A-Za-z0-9._^=+&-]+", rec["symbol"]):
        raise ValueError("Invalid symbol")
    for field in DATE_FIELDS:
        value = raw.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(f"{field} must use YYYY-MM-DD")
        rec[field] = datetime.date.fromisoformat(value).isoformat()
    for field in NUMBER_FIELDS:
        value = raw.get(field)
        if value is not None and value != "":
            rec[field] = number(value, field)
            if field != "rate" and rec[field] < 0:
                raise ValueError(f"{field} cannot be negative")
    if typ == "gold" and raw.get("kitco_bid") not in (None, ""):
        bid = number(raw["kitco_bid"], "Kitco bid")
        if bid <= 0:
            raise ValueError("Kitco bid must be positive")
        rec["kitco_bid"] = bid
    if typ in METALS:
        if rec.get("units", 0) <= 0 or "buy" not in rec:
            raise ValueError("Precious metals need positive fine-metal grams and an INR buy price per gram")
        rec.update(symbol=METALS[typ], currency="INR", fx_buy=1)
    if typ in ("stock", "mf", "crypto"):
        rec["symbol"] = rec.get("symbol", "").lower() if typ == "crypto" else rec.get("symbol", "").upper()
        if not rec["symbol"] or rec.get("units", 0) <= 0 or "buy" not in rec:
            raise ValueError("Market holdings need a symbol, positive units and a buy price")
    if typ == "fd":
        if not all(k in rec for k in ("principal", "rate", "start", "maturity")):
            raise ValueError("FD needs principal, rate, start and maturity")
        if rec["maturity"] < rec["start"] or rec.get("freq", 4) not in (1, 2, 4, 12):
            raise ValueError("Invalid fixed deposit dates or compounding frequency")
        if not -99 <= rec["rate"] <= 100:
            raise ValueError("Fixed deposit rate must be between -99 and 100")
        rec["freq"] = int(rec.get("freq", 4))
    if typ == "manual" and "value" not in rec and "principal" not in rec:
        raise ValueError("Manual investments need value or principal")
    if "fx_buy" in rec and rec["fx_buy"] <= 0:
        raise ValueError("Purchase FX rate must be positive")
    if ("closed_date" in rec) != ("closed_value" in rec):
        raise ValueError("Closed date and amount received must be supplied together")
    start = rec.get("start") if typ == "fd" else rec.get("date")
    if start and rec.get("closed_date", start) < start:
        raise ValueError("Closed date precedes investment date")
    return rec


def equity_state(trades):
    units = basis = realized = 0.0
    opened = None
    for trade in trades:
        quantity = trade["units"]
        fee = trade["fee"]
        if trade["side"] == "buy":
            if not units:
                opened = trade["date"]
            units += quantity
            basis += quantity * trade["price"] + fee
        else:
            if not units or quantity > units + 1e-9:
                raise ValueError("Sale exceeds the shares held on that date")
            cost = basis * min(1, quantity / units)
            realized += quantity * trade["price"] - fee - cost
            units -= quantity
            basis -= cost
            if units < 1e-9:
                units = basis = 0.0
    return units, basis, realized, opened


def equity_transactions(db, holding):
    sample = str(holding["id"]).startswith("sample")
    lots = [row for row in db if row.get("type") == "stock" and not row.get("closed_date")
            and str(row.get("symbol", "")).upper() == holding["symbol"].upper()
            and str(row["id"]).startswith("sample") == sample]
    entries = []
    for row in lots:
        if "transactions" in row:
            entries.extend(row["transactions"])
            continue
        date = chart_date(row.get("date"))
        if date is None:
            raise ValueError("Add a purchase date to each open lot for this ticker first")
        fx = row.get("fx_buy")
        currency = row.get("currency")
        if not currency and not fx and not row["symbol"].upper().endswith((".NS", ".BO")):
            currency = price_info("stock", row["symbol"])["currency"]
        if currency and currency != "INR" and not fx:
            raise ValueError("Save the purchase exchange rate before starting the ledger")
        entries.append({"side": "buy", "date": date.isoformat(), "units": float(row["units"]),
                        "price": float(row["buy"]) * (float(fx) if fx else 1),
                        "fee": 0, "opening": True})
    return sorted(entries, key=lambda trade: trade["date"]), [row["id"] for row in lots]


def validated_trade(body):
    side = body.get("side")
    date = chart_date(body.get("date"))
    if side not in ("buy", "sell") or date is None or date > datetime.date.today():
        raise ValueError("Choose Buy or Sell and a valid trade date through today")
    units = number(body.get("units"), "shares")
    price = number(body.get("price"), "INR price per share")
    fee = number(body.get("fee", 0), "INR fee")
    if units <= 0 or price < 0 or fee < 0 or (side == "sell" and fee > units * price):
        raise ValueError("Check shares, price and fees")
    return {"side": side, "date": date.isoformat(), "units": units, "price": price, "fee": fee}


def save_equity_trade(db, holding, body):
    trade = validated_trade(body)
    trades, ids = equity_transactions(db, holding)
    return save_equity_ledger(db, holding, [*trades, trade], ids)


def save_equity_ledger(db, holding, trades, ids):
    trades = sorted(trades, key=lambda trade: trade["date"])
    balance, basis, realized, opened = equity_state(trades)
    updated = {**holding, "transactions": trades, "units": balance,
               "buy": basis / balance if balance else 0, "currency": "INR", "fx_buy": 1,
               "date": opened, "realized": realized}
    write(DB, [updated if row["id"] == holding["id"] else row for row in db
               if row["id"] == holding["id"] or row["id"] not in ids], indent=1)
    return updated


def lot_key(rec):
    fields = ("units", "buy", "date", "currency", "principal", "rate", "start", "maturity",
              "freq", "value", "closed_date", "closed_value")
    identity = (rec.get("symbol") or rec.get("name", "")).casefold()
    return (rec.get("type"), identity, *(rec.get(k) for k in fields))


def col_idx(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group():
        n = n * 26 + ord(ch) - 64
    return n - 1

def parse_table(data, name):
    """Returns rows as lists of strings; handles .xlsx (zip of XML) and .csv."""
    if data[:2] != b"PK":                       # plain CSV / text
        return [r for r in csv.reader(io.StringIO(data.decode("utf-8-sig", "replace")))
                if any(str(c).strip() for c in r)]
    with zipfile.ZipFile(io.BytesIO(data)) as workbook:
        return parse_workbook(workbook)


def parse_workbook(z):
    members = z.infolist()
    if len(members) > 200 or sum(m.file_size for m in members) > 20 * 1024 * 1024:
        raise ValueError("Excel workbook expands beyond the supported size")
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(XL_NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(XL_NS + "t")))
    sheets = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    if not sheets:
        raise ValueError("no worksheet found in the xlsx file")
    root = ET.fromstring(z.read(sheets[0]))
    rows = []
    for row in root.iter(XL_NS + "row"):
        cells = {}
        for c in row.iter(XL_NS + "c"):
            t, v = c.get("t", ""), c.find(XL_NS + "v")
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(XL_NS + "t"))
            elif v is None:
                continue
            elif t == "s":
                val = shared[int(v.text)]
            else:
                val = v.text
            if val is not None and str(val).strip() != "":
                column = col_idx(c.get("r", "A1"))
                if column >= 256:
                    raise ValueError("Excel workbook has data beyond column IV")
                cells[column] = str(val).strip()
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return rows

def do_import(body, preview=False):
    try:
        data = base64.b64decode(body["data"], validate=True)
    except Exception:
        return {"error": "invalid file data"}
    if len(data) > MAX_FILE:
        return {"error": "Import files must be 5 MB or smaller"}
    try:
        rows = parse_table(data, body.get("filename", ""))
    except Exception as e:
        return {"error": f"could not read file: {e}"}
    if len(rows) > 20001:
        return {"error": "Import files must contain no more than 20,000 data rows"}
    if not rows:
        return {"error": "file is empty"}
    hdr = [str(c).strip().lower() for c in rows[0]]

    def col(*names):
        for n in names:
            if n in hdr:
                return hdr.index(n)
        return None
    idx = {k: col(*v) for k, v in {
        "type": ("type",), "name": ("name",), "symbol": ("symbol", "code", "ticker"),
        "units": ("units", "qty", "quantity", "shares"), "buy": ("buy", "buy price", "avg buy price", "buy_price", "avg. buy price"),
        "date": ("date", "buy date", "buy_date"), "principal": ("principal", "amount invested", "invested", "amount"),
        "rate": ("rate", "interest rate", "interest_rate"), "start": ("start", "start date", "start_date"),
        "maturity": ("maturity", "maturity date", "maturity_date"), "freq": ("freq", "frequency", "compounded"),
        "value": ("value", "current value", "current_value"), "currency": ("currency",), "fx_buy": ("fx_buy", "buy fx"), "tax_class": ("tax_class",),
        "closed_date": ("closed_date", "closed date"), "closed_value": ("closed_value", "closed value")}.items()}
    if any("trade no" in h for h in hdr) and any("trade type" in h for h in hdr) and any("segment" in h for h in hdr):
        return dhan_import(rows, preview)          # Dhan tradebook export detected
    if idx["type"] is None or idx["name"] is None:
        return {"error": "first row must be a header with at least 'type' and 'name' columns - use the Download template button"}
    db = read(DB, [])
    added, skipped, errors = [], [], []
    ledger_symbols = {h["symbol"].upper() for h in db if h.get("transactions")
                      and not str(h.get("id", "")).startswith("sample")}
    keys = set()
    for holding in db:
        try:
            keys.add(lot_key(validate_holding(holding)))
        except ValueError:
            continue

    def dval(v):
        v = str(v).strip()
        if re.fullmatch(r"\d{5}", v):          # Excel serial date -> ISO
            return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(v))).isoformat()
        return v
    for rnum, r in enumerate(rows[1:], start=2):
        try:
            g = lambda k: (str(r[idx[k]]).strip() if idx[k] is not None and idx[k] < len(r) else "")
            typ = g("type").lower()
            if typ not in TYPES:
                raise ValueError(f"unknown type '{g('type')}' (use stock/mf/crypto/gold/silver/fd/manual)")
            name = g("name")
            if re.match(r"^'[\s]*[=+@-]", name):
                name = name[1:]
            if not name:
                raise ValueError("missing name")
            rec = {"type": typ, "name": name}
            if g("symbol"):                       # CoinGecko ids are lowercase (bitcoin); tickers upper
                symbol = g("symbol")
                if re.match(r"^'[\s]*[=+@-]", symbol):
                    symbol = symbol[1:]
                rec["symbol"] = symbol.lower() if typ == "crypto" else symbol.upper()
            if g("currency"):
                rec["currency"] = g("currency").upper()
            if g("tax_class"):
                rec["tax_class"] = g("tax_class").lower()
            for k in ("date", "start", "maturity", "closed_date"):
                if g(k):
                    rec[k] = dval(g(k))
            for k in ("units", "buy", "principal", "rate", "value", "closed_value", "fx_buy"):
                if g(k):
                    try:
                        rec[k] = float(str(g(k)).replace(",", ""))
                    except ValueError:
                        raise ValueError(f"'{k}' must be a number, got '{g(k)}'")
            if g("freq"):
                rec["freq"] = {"quarterly": "4", "monthly": "12", "half-yearly": "2",
                               "half yearly": "2", "yearly": "1"}.get(g("freq").lower(), g("freq"))
            rec = validate_holding(rec)
            key = lot_key(rec)
            if rec["type"] == "stock" and rec["symbol"] in ledger_symbols:
                skipped.append(f"{name} (record trades in the existing ledger)")
                continue
            if key in keys:
                skipped.append(name)
                continue
            keys.add(key)
            rec["id"] = uuid.uuid4().hex[:8]
            db.append(rec)
            added.append(name)
        except ValueError as e:
            errors.append(f"row {rnum}: {e}")
    if added and not preview:
        write(DB, db, indent=1)
    return {"added": len(added), "names": added, "skipped": skipped, "errors": errors}



def dhan_date(v):
    v = str(v).strip().split(" ", 1)[0].split("T", 1)[0]
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return v
    m = re.fullmatch(r"(\d{2})[-/](\d{2})[-/](\d{4})", v)     # DD-MM-YYYY or DD/MM/YYYY
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    if re.fullmatch(r"\d{5}", v):                               # Excel serial date
        return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(v))).isoformat()
    return None

def dhan_import(rows, preview=False):
    """Import a Dhan tradebook CSV: consolidate equity trades into holdings."""
    hdr = [str(c).strip().lower() for c in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(hdr):
                if h == n or (len(n) > 4 and h.startswith(n)):
                    return i
        return None
    ix = {"sym": col("symbol"), "tt": col("trade type"), "qty": col("quantity"),
          "prc": col("price"), "dt": col("trade date"), "exch": col("exchange"),
          "time": col("trade time"), "ins": col("instrument"), "exp": col("expiry date"),
          "strike": col("strike price"), "opt": col("option type")}
    if None in (ix["sym"], ix["tt"], ix["qty"], ix["prc"], ix["dt"]):
        return {"error": "this does not look like a Dhan tradebook "
                         "(need symbol, trade type, quantity, price and trade date columns)"}
    trades, ignored = {}, 0
    for row_number, r in enumerate(rows[1:], 2):
        g = lambda k: (str(r[ix[k]]).strip() if ix[k] is not None and ix[k] < len(r) else "")
        if (g("exp") or g("strike") or g("opt")
                or (ix["ins"] is not None and g("ins").lower() not in ("", "eq", "equity", "e"))):
            ignored += 1                      # skip F&O / derivatives
            continue
        tt = g("tt").upper()
        if tt not in ("BUY", "SELL"):
            ignored += 1
            continue
        try:
            qty = number(g("qty").replace(",", ""), "quantity")
            prc = number(g("prc").replace(",", ""), "price")
        except ValueError:
            ignored += 1
            continue
        if qty <= 0 or prc <= 0:
            ignored += 1
            continue
        sym = g("sym").upper().replace(" ", "")
        exch = g("exch").upper()
        tick = sym + (".NS" if exch == "NSE" and "." not in sym
                      else ".BO" if exch == "BSE" and "." not in sym else "")
        date = dhan_date(g("dt"))
        try:
            date = datetime.date.fromisoformat(date or "").isoformat()
        except ValueError:
            ignored += 1
            continue
        if not sym:
            ignored += 1
            continue
        clock = g("time") or g("dt").split(" ", 1)[-1]
        try:
            clock = datetime.time.fromisoformat(clock).isoformat()
        except ValueError:
            clock = ""
        trades.setdefault(tick, []).append((date, clock, row_number, tt, qty, prc))
    db = read(DB, [])
    added, skipped, errors = [], [], []
    existing = {str(h.get("symbol", "")).upper() for h in db
                if h.get("type") == "stock" and not h.get("closed_date")
                and not str(h.get("id", "")).startswith("sample")}
    for tick, rows_for_ticker in trades.items():
        units, basis, opened = 0.0, 0.0, None
        for date, _, _, side, qty, price in sorted(rows_for_ticker):
            if side == "BUY":
                if not units:
                    opened = date
                units += qty
                basis += qty * price
            elif not units or qty > units + 1e-9:
                errors.append(f"{tick}: sales exceed known purchases on {date}; import a complete tradebook")
                break
            else:
                basis *= max(0.0, units - qty) / units
                units = max(0.0, units - qty)
                if units < 1e-9:
                    units, basis = 0.0, 0.0
        else:
            if not units:
                skipped.append(f"{tick} (fully sold)")
                continue
            if tick in existing:
                skipped.append(f"{tick} (open holding already exists)")
                continue
            try:
                rec = validate_holding({"type": "stock", "name": tick, "symbol": tick,
                                        "units": units, "buy": round(basis / units, 8), "date": opened})
            except ValueError as e:
                errors.append(f"{tick}: {e}")
                continue
            rec["id"] = uuid.uuid4().hex[:8]
            db.append(rec)
            added.append(tick)
    if added and not preview:
        write(DB, db, indent=1)
    if ignored:
        errors.append(f"{ignored} unsupported or invalid trade rows ignored")
    return {"added": len(added), "names": added, "skipped": skipped, "errors": errors, "format": "dhan"}


TEMPLATE_CSV = (
    "type,name,symbol,units,buy,currency,fx_buy,tax_class,date,principal,rate,start,maturity,freq,value\n"
    "stock,Reliance Industries,RELIANCE.NS,10,2450,INR,1,,2024-03-12,,,,,,\n"
    "stock,Apple Inc.,AAPL,5,190,USD,83,,2025-03-12,,,,,,\n"
    "mf,Axis Bluechip Fund,120503,500,42.50,,,equity,2025-01-15,,,,,,\n"
    "fd,My SBI FD,,,,,,,,200000,7.1,2025-04-01,2028-04-01,4,\n"
    "manual,Gold jewelry,,,,,,,2025-11-20,150000,,,,,180000\n")


LEGACY_RELIANCE_SAMPLE = {
"id": "sample-reli",
"type": "stock",
"name": "Reliance Industries",
"symbol": "RELIANCE.NS",
"units": 15,
"buy": 2410,
"date": "2025-02-10"
}
RELIANCE_SAMPLE_TRADES = [
    {"side": "buy", "date": "2025-02-10", "units": 10, "price": 2300, "fee": 0},
    {"side": "buy", "date": "2025-09-12", "units": 5, "price": 2620, "fee": 50},
    {"side": "sell", "date": "2026-03-20", "units": 3, "price": 2800, "fee": 25},
]


def reliance_sample():
    units, basis, realized, opened = equity_state(RELIANCE_SAMPLE_TRADES)
    return {**LEGACY_RELIANCE_SAMPLE, "units": units, "buy": basis / units,
            "date": opened, "currency": "INR", "fx_buy": 1, "realized": realized,
            "transactions": RELIANCE_SAMPLE_TRADES}


SAMPLES = [
reliance_sample(),
{
"id": "sample-hdfc",
"type": "stock",
"name": "HDFC Bank",
"symbol": "HDFCBANK.NS",
"units": 25,
"buy": 1640,
"date": "2024-06-15"
},
{
"id": "sample-aapl",
"type": "stock",
"name": "Apple Inc.",
"symbol": "AAPL",
"units": 5,
"buy": 190,
"date": "2025-03-05"
},
{
"id": "sample-axis",
"type": "mf",
"name": "Axis Bluechip Fund",
"symbol": "120503",
"units": 800,
"buy": 38.5,
"date": "2024-09-20"
},
{
"id": "sample-btc",
"type": "crypto",
"name": "Bitcoin",
"symbol": "bitcoin",
"units": 0.05,
"buy": 5200000,
"date": "2025-05-12"
},
{
"id": "sample-fd1",
"type": "fd",
"name": "SBI Fixed Deposit",
"principal": 200000,
"rate": 7.1,
"start": "2025-04-01",
"maturity": "2028-04-01",
"freq": "4"
},
{
"id": "sample-gold",
"type": "manual",
"name": "Gold jewelry",
"principal": 150000,
"value": 168000,
"date": "2025-11-20"
},
{
"id": "sample-tata",
"type": "stock",
"name": "Tata Motors",
"symbol": "TATAMOTORS.NS",
"units": 20,
"buy": 780,
"date": "2024-04-10",
"closed_date": "2026-03-15",
"closed_value": 22000
}
]

def load_samples():
    """Append sample investments to the live portfolio (skipped if already present)."""
    db = read(DB, [])
    have = {h.get("id") for h in db}
    fresh = [s for s in SAMPLES if s["id"] not in have]
    updated = sum(row == LEGACY_RELIANCE_SAMPLE for row in db)
    if fresh or updated:
        db = [SAMPLES[0] if row == LEGACY_RELIANCE_SAMPLE else row for row in db]
        db.extend(fresh)
        write(DB, db, indent=1)
    return {"added": len(fresh), "updated": updated, "total": len(SAMPLES)}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # no console output (runs without a window)
        pass

    def send(self, code, body, ctype="application/json", filename=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")      # never serve stale UI
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, write):
        # Blocks other websites from talking to this local server.
        if self.headers.get("Host", "") not in (f"localhost:{PORT}", f"127.0.0.1:{PORT}"):
            return False
        if not write:
            return True
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers["Host"]:
            return False
        return self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/json"

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_BODY:
            raise OverflowError("Request is too large")
        body = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        return body

    def mutate(self, operation):
        try:
            with LOCK:
                operation()
        except OverflowError as e:
            self.send(413, {"error": str(e)})
        except (ValueError, KeyError) as e:
            self.send(400, {"error": str(e)})
        except OSError as e:
            print(f"Storage operation failed: {e}", file=sys.stderr)
            self.send(500, {"error": "Could not save portfolio data. Check the app folder and try again."})

    def do_GET(self):
        if not self.allowed(False):
            return self.send(403, {})
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/":
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/portfolio.js":
            return self.send(200, (HERE / "portfolio.js").read_bytes(), "text/javascript; charset=utf-8")
        if u.path == "/favicon.ico" and (HERE / "tracker.ico").exists():
            return self.send(200, (HERE / "tracker.ico").read_bytes(), "image/x-icon")
        if u.path == "/api/holdings":
            return self.send(200, read(DB, []))
        if u.path == "/api/history":
            return self.send(200, read(SNAP, {}))
        if u.path == "/api/chart":
            try:
                return self.send(200, chart_series())
            except Exception:
                return self.send(502, {"error": "chart unavailable"})
        if u.path == "/api/benchmark":
            try:
                return self.send(200, benchmark_series())
            except Exception:
                return self.send(502, {"error": "benchmark unavailable"})
        if u.path == "/api/template":
            return self.send(200, TEMPLATE_CSV.encode(), "text/csv; charset=utf-8",
                             filename="import_template.csv")
        if u.path == "/api/series":
            try:
                return self.send(200, series(q["type"], q["symbol"], int(q.get("days", "365"))))
            except Exception:
                return self.send(502, {"error": "series unavailable"})
        if u.path == "/api/fx":
            try:
                return self.send(200, {"rate": fx_rate_on(q.get("currency", "INR"), q.get("date"))})
            except Exception:
                return self.send(502, {"error": "FX rate unavailable"})
        if u.path == "/api/search":
            return self.send(200, search(q.get("type", ""), q.get("q", "")))
        if u.path == "/api/price":
            try:
                return self.send(200, price_info(q["type"], q["symbol"]))
            except Exception:
                return self.send(502, {"error": "price unavailable"})
        self.send(404, {})

    def do_POST(self):
        if not self.allowed(True):
            return self.send(403, {})
        if self.path == "/api/quit":
            self.send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown).start()
            return
        self.mutate(self._post)

    def _post(self):
        p = self.path
        body = self.body()
        if p.startswith("/api/transactions/"):
            hid = p[len("/api/transactions/"):]
            db = read(DB, [])
            holding = next((row for row in db if row.get("id") == hid), None)
            if not holding or holding.get("type") != "stock" or holding.get("closed_date"):
                return self.send(404, {"error": "Open equity holding not found"})
            try:
                updated = save_equity_trade(db, holding, body)
            except OSError as error:
                if isinstance(error, urllib.error.URLError):
                    return self.send(502, {"error": "Could not verify the equity currency. Edit the holding currency and retry."})
                raise
            return self.send(200, updated)
        if p == "/api/holdings":
            body = validate_holding(body)
            body["id"] = uuid.uuid4().hex[:8]
            d = read(DB, [])
            if body["type"] == "stock" and any(
                row.get("transactions") and row.get("symbol", "").upper() == body["symbol"]
                and not str(row.get("id", "")).startswith("sample") for row in d
            ):
                raise ValueError("Record this purchase in the existing ticker ledger")
            d.append(body)
            write(DB, d, indent=1)
            return self.send(200, body)
        if p == "/api/samples":
            return self.send(200, load_samples())
        if p in ("/api/import", "/api/import/preview"):
            result = do_import(body, preview=p.endswith("/preview"))
            return self.send(400 if "error" in result else 200, result)
        if p == "/api/history":
            value = number(body.get("value"), "portfolio value")
            if value < 0:
                raise ValueError("Portfolio value cannot be negative")
            d = read(SNAP, {})
            d[time.strftime("%Y-%m-%d")] = value
            write(SNAP, d)
            return self.send(200, d)
        if p == "/api/clear_samples":
            keep = [h for h in read(DB, []) if not str(h.get("id", "")).startswith("sample")]
            write(DB, keep, indent=1)
            return self.send(200, {"ok": True})
        self.send(404, {})

    def do_PUT(self):
        if not self.allowed(True):
            return self.send(403, {})
        self.mutate(self._put)

    def holding_target(self):
        match = re.fullmatch(r"/api/holdings/([A-Za-z0-9_-]{1,80})", self.path)
        if not match:
            self.send(404, {"error": "Holding not found"})
            return None
        hid = match[1]
        db = read(DB, [])
        if not any(row.get("id") == hid for row in db):
            self.send(404, {"error": "Holding not found"})
            return None
        return db, hid

    def change_transaction(self, delete=False):
        match = re.fullmatch(r"/api/transactions/([A-Za-z0-9_-]{1,80})/(\d{1,9})", self.path)
        if not match:
            return self.send(404, {"error": "Transaction not found"})
        hid, index = match[1], int(match[2])
        db = read(DB, [])
        holding = next((row for row in db if row.get("id") == hid), {})
        trades = list(holding.get("transactions", []))
        if index >= len(trades):
            return self.send(404, {"error": "Transaction not found. Refresh the ledger and try again."})
        body = self.body()
        if body.get("original") != trades[index]:
            return self.send(409, {"error": "This transaction changed. Refresh the ledger and try again."})
        if delete:
            del trades[index]
        else:
            replacement = validated_trade(body)
            if trades[index].get("opening") and replacement["side"] == "buy":
                replacement["opening"] = True
            trades[index] = replacement
        updated = save_equity_ledger(db, holding, trades, [hid])
        return self.send(200, updated)

    def _put(self):
        if self.path.startswith("/api/transactions/"):
            return self.change_transaction()
        target = self.holding_target()
        if target:
            db, hid = target
            if next(row for row in db if row["id"] == hid).get("transactions"):
                raise ValueError("Use the transaction ledger to change this equity balance")
            body = validate_holding(self.body())
            body["id"] = hid
            write(DB, [body if h.get("id") == hid else h for h in db], indent=1)
            self.send(200, body)

    def do_DELETE(self):
        if not self.allowed(True):
            return self.send(403, {})
        self.mutate(self._delete)

    def _delete(self):
        if self.path.startswith("/api/transactions/"):
            return self.change_transaction(delete=True)
        target = self.holding_target()
        if target:
            db, hid = target
            if next(row for row in db if row["id"] == hid).get("transactions"):
                raise ValueError("This equity has a transaction ledger")
            write(DB, [h for h in db if h.get("id") != hid], indent=1)
            self.send(200, {"ok": True})

def running():
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
        return True
    except OSError:
        return False

def tell(msg, error=False):
    """Show a message box on Windows (no console when started from the desktop icon)."""
    print(msg)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "Investment Tracker", 0x10 if error else 0x40)
        except Exception:
            pass

def main():
    url = f"http://localhost:{PORT}"
    if running():              # already started: just open the page
        webbrowser.open(url)
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        tell(f"Investment Tracker couldn't start because port {PORT} is in use by another program.\n\n"
             f"Close that program (or restart the PC) and try again.\n\nDetails: {e}", error=True)
        return
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()

if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        tell("Investment Tracker stopped because of an unexpected error.\n"
             f"Details were saved to {LOG}", error=True)
