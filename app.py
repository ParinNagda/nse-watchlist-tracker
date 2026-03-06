import os

from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import requests

load_dotenv()

app = Flask(__name__)


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL", "sqlite:///watchlist.db")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    return database_url


app.config["SQLALCHEMY_DATABASE_URI"] = get_database_url()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

NSE_BASE = "https://www.nseindia.com"
NSE_QUOTE_API = f"{NSE_BASE}/api/quote-equity"


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
    session = requests.Session()
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    session.get(
        NSE_BASE,
        headers={"User-Agent": user_agent, "Accept": "text/html"},
        timeout=15,
    )

    response = session.get(
        NSE_QUOTE_API,
        params={"symbol": symbol},
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json,text/plain,*/*",
            "Referer": f"{NSE_BASE}/get-quotes/equity?symbol={symbol}",
        },
        timeout=15,
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
            tracked.append(
                {
                    "symbol": symbol,
                    "targetPrice": target_price,
                    "error": f"Unable to fetch latest price: {str(exc)}",
                }
            )

    return jsonify({"items": tracked})


if __name__ == "__main__":
    app.run(debug=True)
