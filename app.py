import os
from datetime import datetime
from urllib.parse import quote_plus

from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import requests
from sqlalchemy import inspect, text
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

app = Flask(__name__)


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL", "sqlite:///watchlist.db")

    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://") and "+" not in database_url.split("://", 1)[0]:
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return database_url


app.config["SQLALCHEMY_DATABASE_URI"] = get_database_url()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

NSE_BASE = "https://www.nseindia.com"
NSE_QUOTE_API = f"{NSE_BASE}/api/quote-equity"
YAHOO_QUOTE_API = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_QUOTE_SUMMARY_API = "https://query2.finance.yahoo.com/v10/finance/quoteSummary"
GOOGLE_FINANCE_QUOTE_URL = "https://www.google.com/finance/quote"
QUOTE_PROVIDER = os.getenv("QUOTE_PROVIDER", "AUTO").strip().upper()

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
_NSE_TIMEOUT = 20


def _create_nse_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


NSE_SESSION = _create_nse_session()


def _nse_headers(symbol: str, wants_json: bool = False) -> dict:
    encoded_symbol = quote_plus(symbol)
    return {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json,text/plain,*/*" if wants_json else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{NSE_BASE}/get-quotes/equity?symbol={encoded_symbol}",
        "Origin": NSE_BASE,
        "Connection": "keep-alive",
        "DNT": "1",
    }


def _prime_nse_session(symbol: str) -> None:
    encoded_symbol = quote_plus(symbol)
    NSE_SESSION.get(NSE_BASE, headers=_nse_headers(symbol), timeout=_NSE_TIMEOUT)
    NSE_SESSION.get(
        f"{NSE_BASE}/get-quotes/equity?symbol={encoded_symbol}",
        headers=_nse_headers(symbol),
        timeout=_NSE_TIMEOUT,
    )


class WatchlistItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), unique=True, nullable=False, index=True)
    target_price = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


def ensure_watchlist_schema() -> None:
    inspector = inspect(db.engine)
    columns = {column["name"] for column in inspector.get_columns("watchlist_item")}
    if "created_at" in columns:
        return

    dialect = db.engine.dialect.name
    if dialect == "sqlite":
        db.session.execute(text("ALTER TABLE watchlist_item ADD COLUMN created_at DATETIME"))
    else:
        db.session.execute(text("ALTER TABLE watchlist_item ADD COLUMN created_at TIMESTAMP"))

    now_value = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    db.session.execute(
        text("UPDATE watchlist_item SET created_at = :now_value WHERE created_at IS NULL"),
        {"now_value": now_value},
    )
    db.session.commit()


with app.app_context():
    db.create_all()
    ensure_watchlist_schema()


def _format_created_at(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")

    text_value = str(value).strip()
    if not text_value:
        return "N/A"

    text_value = text_value.replace("T", " ")
    return text_value.split(".", 1)[0]


def parse_price(value) -> float:
    if value is None:
        raise ValueError("Price is required.")
    parsed = float(value)
    if parsed <= 0:
        raise ValueError("Price must be greater than 0.")
    return round(parsed, 2)


def fetch_nse_quote(symbol: str) -> dict:
    clean_symbol = symbol.strip().upper()
    if not clean_symbol:
        raise ValueError("Stock symbol is required.")

    _prime_nse_session(clean_symbol)
    response = NSE_SESSION.get(
        NSE_QUOTE_API,
        params={"symbol": clean_symbol},
        headers=_nse_headers(clean_symbol, wants_json=True),
        timeout=_NSE_TIMEOUT,
    )

    if response.status_code in (401, 403):
        _prime_nse_session(clean_symbol)
        response = NSE_SESSION.get(
            NSE_QUOTE_API,
            params={"symbol": clean_symbol},
            headers=_nse_headers(clean_symbol, wants_json=True),
            timeout=_NSE_TIMEOUT,
        )

    response.raise_for_status()
    return response.json()


def _format_epoch(value) -> str:
    if value is None:
        return "N/A"
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, TypeError):
        return "N/A"


def _parse_price_text(raw_value: str):
    if raw_value is None:
        return None
    cleaned = "".join(ch for ch in str(raw_value) if ch.isdigit() or ch in {".", "-"})
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_yahoo_quote(symbol: str) -> dict:
    clean_symbol = symbol.strip().upper()
    if not clean_symbol:
        raise ValueError("Stock symbol is required.")

    candidates = [f"{clean_symbol}.NS", f"{clean_symbol}.BO", clean_symbol]
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_error = None

    for ticker in candidates:
        try:
            response = requests.get(
                YAHOO_QUOTE_API,
                params={"symbols": ticker},
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()

            payload = response.json()
            results = (payload.get("quoteResponse") or {}).get("result") or []
            if results:
                quote = results[0]
                last_price = quote.get("regularMarketPrice")
                close_price = quote.get("regularMarketPreviousClose")
                if last_price is not None or close_price is not None:
                    return {
                        "source": "YAHOO",
                        "symbol": clean_symbol,
                        "close": close_price,
                        "lastPrice": last_price if last_price is not None else close_price,
                        "lastUpdate": _format_epoch(quote.get("regularMarketTime")),
                    }

            response2 = requests.get(
                f"{YAHOO_QUOTE_SUMMARY_API}/{ticker}",
                params={"modules": "price"},
                headers=headers,
                timeout=15,
            )
            response2.raise_for_status()
            payload2 = response2.json()
            result = ((payload2.get("quoteSummary") or {}).get("result") or [None])[0] or {}
            price_obj = result.get("price") or {}

            last_price2 = (price_obj.get("regularMarketPrice") or {}).get("raw")
            close_price2 = (price_obj.get("regularMarketPreviousClose") or {}).get("raw")
            market_time = (price_obj.get("regularMarketTime") or {}).get("raw")

            if last_price2 is not None or close_price2 is not None:
                return {
                    "source": "YAHOO",
                    "symbol": clean_symbol,
                    "close": close_price2,
                    "lastPrice": last_price2 if last_price2 is not None else close_price2,
                    "lastUpdate": _format_epoch(market_time),
                }
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise RuntimeError(f"Yahoo fallback failed: {last_error}")

    raise ValueError(f"Stock symbol '{clean_symbol}' was not found in fallback quote source.")


def fetch_google_finance_quote(symbol: str) -> dict:
    clean_symbol = symbol.strip().upper()
    if not clean_symbol:
        raise ValueError("Stock symbol is required.")

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_error = None
    for exchange in ("NSE", "BOM"):
        try:
            response = requests.get(
                f"{GOOGLE_FINANCE_QUOTE_URL}/{clean_symbol}:{exchange}",
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            html = response.text

            price_anchor = '<div class="YMlKec fxKbKc">'
            start = html.find(price_anchor)
            if start == -1:
                continue
            start += len(price_anchor)
            end = html.find("</div>", start)
            current_text = html[start:end] if end != -1 else None
            current_price = _parse_price_text(current_text)

            prev_label = "Previous close"
            prev_start = html.find(prev_label)
            close_price = None
            if prev_start != -1:
                close_anchor = '<div class="P6K39c">'
                close_anchor_start = html.find(close_anchor, prev_start)
                if close_anchor_start != -1:
                    close_anchor_start += len(close_anchor)
                    close_anchor_end = html.find("</div>", close_anchor_start)
                    close_text = html[close_anchor_start:close_anchor_end] if close_anchor_end != -1 else None
                    close_price = _parse_price_text(close_text)

            if current_price is None and close_price is None:
                continue

            return {
                "source": "GOOGLE",
                "symbol": clean_symbol,
                "close": close_price,
                "lastPrice": current_price if current_price is not None else close_price,
                "lastUpdate": "N/A",
            }
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise RuntimeError(f"Google fallback failed: {last_error}")

    raise ValueError(f"Stock symbol '{clean_symbol}' was not found in Google fallback source.")


def fetch_quote_data(symbol: str) -> dict:
    if QUOTE_PROVIDER == "YAHOO":
        try:
            return fetch_yahoo_quote(symbol)
        except Exception:
            return fetch_google_finance_quote(symbol)
    if QUOTE_PROVIDER == "GOOGLE":
        return fetch_google_finance_quote(symbol)

    try:
        payload = fetch_nse_quote(symbol)
        price_info = payload.get("priceInfo") or {}
        close_value = price_info.get("close")
        last_value = price_info.get("lastPrice")
        return {
            "source": "NSE",
            "symbol": symbol.strip().upper(),
            "close": close_value,
            "lastPrice": last_value if last_value is not None else close_value,
            "lastUpdate": payload.get("metadata", {}).get("lastUpdateTime", "N/A"),
        }
    except requests.HTTPError as exc:
        if QUOTE_PROVIDER == "NSE":
            raise
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in (401, 403, 429, 500, 502, 503, 504):
            try:
                return fetch_yahoo_quote(symbol)
            except Exception:
                return fetch_google_finance_quote(symbol)
        raise
    except requests.RequestException:
        if QUOTE_PROVIDER == "NSE":
            raise
        try:
            return fetch_yahoo_quote(symbol)
        except Exception:
            return fetch_google_finance_quote(symbol)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/close", methods=["GET"])
def get_close():
    symbol = request.args.get("symbol", "").strip().upper()

    if not symbol:
        return jsonify({"error": "Please provide a stock symbol."}), 400

    try:
        quote_data = fetch_quote_data(symbol)
        close_value = quote_data.get("close")
        if close_value is None:
            return jsonify({"error": f"Close price not available for '{symbol}'."}), 404

        response = {
            "inputSymbol": symbol,
            "ticker": symbol,
            "close": round(float(close_value), 2),
            "date": quote_data.get("lastUpdate", "N/A"),
            "isToday": True,
            "source": quote_data.get("source", "NSE"),
        }

        return jsonify(response)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        if status_code == 404:
            return jsonify({"error": f"Stock symbol '{symbol}' was not found on NSE."}), 404
        return jsonify({"error": "NSE data service is unavailable. Please try again shortly."}), 502
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": f"Failed to fetch data: {str(exc)}"}), 500


@app.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    items = WatchlistItem.query.order_by(WatchlistItem.symbol.asc()).all()
    serialized = [
        {
            "symbol": item.symbol,
            "price": round(item.target_price, 2),
            "createdAt": _format_created_at(item.created_at),
        }
        for item in items
    ]
    return jsonify({"items": serialized})


@app.route("/api/watchlist", methods=["POST"])
def add_watchlist_item():
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol", "")).strip().upper()

    if not symbol:
        return jsonify({"error": "Please provide a stock symbol."}), 400

    try:
        target_price = parse_price(body.get("price"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    existing = WatchlistItem.query.filter_by(symbol=symbol).first()
    if existing is not None:
        return jsonify({"error": f"{symbol} is already in watchlist."}), 409

    item = WatchlistItem(symbol=symbol, target_price=target_price)
    db.session.add(item)
    db.session.commit()
    return jsonify(
        {
            "item": {
                "symbol": item.symbol,
                "price": round(item.target_price, 2),
                "createdAt": _format_created_at(item.created_at),
            }
        }
    ), 201


@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
def delete_watchlist_item(symbol: str):
    clean_symbol = symbol.strip().upper()
    item = WatchlistItem.query.filter_by(symbol=clean_symbol).first()
    if item is None:
        return jsonify({"error": f"{clean_symbol} is not in watchlist."}), 404

    db.session.delete(item)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/watchlist/check", methods=["GET"])
def check_watchlist():
    items = WatchlistItem.query.order_by(WatchlistItem.symbol.asc()).all()
    tracked = []

    for item in items:
        symbol = item.symbol.upper()
        target = item.target_price

        try:
            target_price = parse_price(target)
        except ValueError:
            continue

        try:
            quote_data = fetch_quote_data(symbol)
            current = quote_data.get("lastPrice")
            if current is None:
                raise ValueError("Current price unavailable")

            current_price = round(float(current), 2)
            move_percent = round(((current_price - target_price) / target_price) * 100, 2)
            is_alert = abs(move_percent) >= 3.0
            tracked.append(
                {
                    "symbol": symbol,
                    "targetPrice": target_price,
                    "currentPrice": current_price,
                    "movePercent": move_percent,
                    "alert": is_alert,
                    "direction": "up" if move_percent >= 0 else "down",
                    "lastUpdate": quote_data.get("lastUpdate", "N/A"),
                    "source": quote_data.get("source", "NSE"),
                    "addedAt": _format_created_at(item.created_at),
                }
            )
        except Exception as exc:
            tracked.append(
                {
                    "symbol": symbol,
                    "targetPrice": target_price,
                    "addedAt": _format_created_at(item.created_at),
                    "error": f"Unable to fetch latest price: {str(exc)}",
                }
            )

    return jsonify({"items": tracked})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
