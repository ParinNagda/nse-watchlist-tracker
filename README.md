# NSE Watchlist Tracker

A self-help stock tracking app for NSE traders and investors to monitor short-term opportunities and long-term holdings in one simple dashboard.

## Features

- Add script + reference price to watchlist
- Auto refreshes latest price every 30 seconds
- Highlights alert when stock moves ±3% from your reference price
- Optional browser notification popup when threshold is hit

## Setup

1. Open terminal in project folder:
   ```bash
   cd nse-close-ui
   ```

2. (Optional) Create and activate virtual environment:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. (Optional) Use PostgreSQL instead of SQLite by setting `DATABASE_URL`:
   ```bash
   set DATABASE_URL=postgresql+psycopg://username:password@localhost:5432/nse_watchlist
   ```

   Or create a `.env` file in the project root:
   ```
   DATABASE_URL=postgresql+psycopg://username:password@localhost:5432/nse_watchlist
   ```

5. Run app:
   ```bash
   python app.py
   ```

6. Open in browser:
   ```
   http://127.0.0.1:5000
   ```

## Notes

- Data source is NSE public quote endpoint (`/api/quote-equity`).
- Availability of final market close depends on market hours and NSE updates.
- Default DB is local SQLite file `watchlist.db`.
- Set `DATABASE_URL` to PostgreSQL connection string to use Postgres.
