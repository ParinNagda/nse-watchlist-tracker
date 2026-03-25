import os
from urllib.parse import quote_plus

from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import requests
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


with app.app_context():
    db.create_all()


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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/close", methods=["GET"])
def get_close():
    symbol = request.args.get("symbol", "").strip().upper()

    if not symbol:
        return jsonify({"error": "Please provide a stock symbol."}), 400

    try:
        payload = fetch_nse_quote(symbol)
        price_info = payload.get("priceInfo") or {}

        close_value = price_info.get("close")
        if close_value is None:
            return jsonify({"error": f"Close price not available for '{symbol}'."}), 404

        last_update = payload.get("metadata", {}).get("lastUpdateTime", "N/A")

        response = {
            "inputSymbol": symbol,
            "ticker": symbol,
            "close": round(float(close_value), 2),
            "date": last_update,
            "isToday": True,
        }

        return jsonify(response)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        if status_code == 404:
            return jsonify({"error": f"Stock symbol '{symbol}' was not found on NSE."}), 404
        return jsonify({"error": "NSE data service is unavailable. Please try again shortly."}), 502
    except Exception as exc:
        return jsonify({"error": f"Failed to fetch data: {str(exc)}"}), 500


@app.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    items = WatchlistItem.query.order_by(WatchlistItem.symbol.asc()).all()
    serialized = [{"symbol": item.symbol, "price": round(item.target_price, 2)} for item in items]
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
    return jsonify({"item": {"symbol": item.symbol, "price": round(item.target_price, 2)}}), 201


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
            payload = fetch_nse_quote(symbol)
            current = payload.get("priceInfo", {}).get("lastPrice")
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
                    "lastUpdate": payload.get("metadata", {}).get("lastUpdateTime", "N/A"),
                }
            )
        except Exception as exc:
            error_text = str(exc)
            if "403" in error_text or "401" in error_text:
                error_text = "NSE blocked this request from the hosting network. Try again later or run locally."
            tracked.append(
                {
                    "symbol": symbol,
                    "targetPrice": target_price,
                    "error": f"Unable to fetch latest price: {error_text}",
                }
            )

    return jsonify({"items": tracked})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
