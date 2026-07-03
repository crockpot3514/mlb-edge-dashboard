"""
MLB edge finder — live dashboard
---------------------------------
Polls Kalshi's public market-data API (no auth needed) for MLB game-winner
markets, refreshes them in the background, and serves a local webpage where
you type in Real's win % for each team and see where it diverges from the
Kalshi-implied probability.

Setup:
    pip install flask requests --break-system-packages   (or in a venv, drop the flag)

Run:
    python mlb_edge_dashboard.py

Then open:
    http://127.0.0.1:5050

This only reads public market data — it does not place orders, does not
touch Real's app, and does not automate anything on your behalf. You still
manually check Real's percentages and manually place any bets yourself.
"""

import os
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template_string

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES_TICKER = "KXMLBGAME"
POLL_SECONDS = 30

app = Flask(__name__)

_lock = threading.Lock()
_state = {
    "games": [],
    "last_updated": None,
    "error": None,
    "debug": {},
}


def fetch_kalshi_mlb_games():
    """Pull open KXMLBGAME markets and normalize into a simple list."""
    url = f"{KALSHI_BASE}/markets"
    params = {"series_ticker": SERIES_TICKER, "status": "open", "limit": 200}
    resp = requests.get(url, params=params, timeout=15, headers={"User-Agent": "mlb-edge-dashboard/1.0"})
    resp.raise_for_status()
    data = resp.json()

    raw_markets = data.get("markets", [])
    debug = {
        "requested_url": resp.url,
        "raw_market_count": len(raw_markets),
        "sample_tickers": [m.get("ticker") for m in raw_markets[:5]],
    }

    games = []
    for m in raw_markets:
        # Skip non-standard / multivariate combo markets, which have a
        # different ticker format and aren't single-game moneylines.
        ticker = m.get("ticker", "")
        if not ticker.startswith(SERIES_TICKER + "-"):
            continue

        yes_team = (m.get("yes_sub_title") or "").replace("yes ", "").strip()
        no_team = (m.get("no_sub_title") or "").replace("no ", "").strip()
        if not yes_team:
            continue

        yes_bid = float(m.get("yes_bid_dollars") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or 0)
        last_price = float(m.get("last_price_dollars") or 0)

        # Prefer the mid of bid/ask when there's an active market; fall
        # back to last traded price if the book is empty.
        if yes_bid > 0 and yes_ask > 0:
            yes_prob = (yes_bid + yes_ask) / 2
        elif yes_ask > 0:
            yes_prob = yes_ask
        elif last_price > 0:
            yes_prob = last_price
        else:
            yes_prob = None

        games.append({
            "ticker": ticker,
            "event_ticker": m.get("event_ticker"),
            "yes_team": yes_team,
            "no_team": no_team,
            "yes_prob": yes_prob,
            "no_prob": (1 - yes_prob) if yes_prob is not None else None,
            "close_time": m.get("close_time"),
            "volume": m.get("volume_fp"),
        })

    debug["matched_after_filter"] = len(games)

    # Group both sides of the same game together by event_ticker so each
    # card shows one matchup instead of duplicate legs.
    grouped = {}
    for g in games:
        key = g["event_ticker"]
        if key not in grouped:
            grouped[key] = g
    return list(grouped.values()), debug


def poll_loop():
    while True:
        try:
            games, debug = fetch_kalshi_mlb_games()
            with _lock:
                _state["games"] = games
                _state["last_updated"] = datetime.now(timezone.utc).isoformat()
                _state["error"] = None
                _state["debug"] = debug
        except Exception as e:
            with _lock:
                _state["error"] = str(e)
        time.sleep(POLL_SECONDS)


@app.route("/api/games")
def api_games():
    with _lock:
        return jsonify({
            "games": _state["games"],
            "last_updated": _state["last_updated"],
            "error": _state["error"],
            "poll_seconds": POLL_SECONDS,
            "debug": _state["debug"],
        })


PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>MLB edge finder</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 16px; color: #222; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #666; font-size: 13px; margin-bottom: 24px; }
  .card { border: 1px solid #ddd; border-radius: 10px; padding: 14px 18px; margin-bottom: 12px; }
  .row { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
  .team { flex: 1; min-width: 220px; }
  .team-name { font-weight: 600; font-size: 14px; }
  .market-prob { color: #666; font-size: 12px; font-family: monospace; margin-top: 2px; }
  input { width: 70px; padding: 6px; border: 1px solid #ccc; border-radius: 6px; font-size: 13px; }
  .edge { margin-top: 8px; font-size: 13px; font-weight: 600; padding: 4px 10px; border-radius: 6px; display: inline-block; }
  .edge-none { color: #888; }
  .edge-value { background: #e1f5ee; color: #085041; }
  .edge-small { background: #faeeda; color: #633806; }
  .status { font-size: 12px; color: #999; margin-bottom: 16px; }
</style>
</head>
<body>
<h1>MLB edge finder</h1>
<div class="sub">Live Kalshi win probabilities. Enter Real's win % for each team to see the gap.</div>
<div class="status" id="status">Loading...</div>
<div id="games"></div>

<script>
const realInputs = {};

function pct(x) { return x === null || x === undefined ? '—' : (x*100).toFixed(1) + '%'; }

function render(data) {
  const statusEl = document.getElementById('status');
  if (data.error) {
    statusEl.textContent = 'Error fetching Kalshi data: ' + data.error;
  } else {
    const t = data.last_updated ? new Date(data.last_updated).toLocaleTimeString() : '—';
    statusEl.textContent = 'Last updated ' + t + ' · refreshes every ' + data.poll_seconds + 's · ' + data.games.length + ' games';
  }

  const container = document.getElementById('games');
  container.innerHTML = '';

  if (!data.games.length) {
    let debugHtml = '';
    if (data.debug && Object.keys(data.debug).length) {
      debugHtml = '<pre style="background:#f5f5f5; padding:10px; border-radius:6px; font-size:11px; overflow-x:auto;">' + JSON.stringify(data.debug, null, 2) + '</pre>';
    }
    container.innerHTML = '<p style="color:#888;">No open KXMLBGAME markets right now.</p>' + debugHtml;
    return;
  }

  data.games.forEach(g => {
    const key = g.ticker;
    if (!realInputs[key]) realInputs[key] = { yes: '', no: '' };

    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="row">
        <div class="team">
          <div class="team-name">${g.yes_team}</div>
          <div class="market-prob">Kalshi: ${pct(g.yes_prob)}</div>
        </div>
        <input type="number" min="1" max="99" placeholder="Real %" data-key="${key}" data-side="yes" value="${realInputs[key].yes}" />
      </div>
      <div class="row" style="margin-top:8px;">
        <div class="team">
          <div class="team-name">${g.no_team}</div>
          <div class="market-prob">Kalshi: ${pct(g.no_prob)}</div>
        </div>
        <input type="number" min="1" max="99" placeholder="Real %" data-key="${key}" data-side="no" value="${realInputs[key].no}" />
      </div>
      <div class="edge-slot" data-key="${key}"></div>
    `;
    container.appendChild(card);

    card.querySelectorAll('input').forEach(inp => {
      inp.addEventListener('input', (e) => {
        const k = e.target.getAttribute('data-key');
        const side = e.target.getAttribute('data-side');
        realInputs[k][side] = e.target.value;
        updateEdge(g, card);
      });
    });

    updateEdge(g, card);
  });
}

function updateEdge(g, card) {
  const key = g.ticker;
  const yesReal = realInputs[key].yes === '' ? null : Number(realInputs[key].yes) / 100;
  const noReal = realInputs[key].no === '' ? null : Number(realInputs[key].no) / 100;
  const slot = card.querySelector('.edge-slot');

  if (yesReal === null && noReal === null) { slot.innerHTML = ''; return; }
  if (g.yes_prob === null) { slot.innerHTML = '<span class="edge edge-none">No live Kalshi price yet</span>'; return; }

  const edgeYes = yesReal !== null ? (g.yes_prob - yesReal) : null;
  const edgeNo = noReal !== null ? (g.no_prob - noReal) : null;

  let best = null;
  if (edgeYes !== null && (!best || edgeYes > best.edge)) best = { team: g.yes_team, edge: edgeYes };
  if (edgeNo !== null && (!best || edgeNo > best.edge)) best = { team: g.no_team, edge: edgeNo };

  if (!best || best.edge <= 0) {
    slot.innerHTML = '<span class="edge edge-none">No clear edge on Real at these numbers</span>';
    return;
  }
  const cls = best.edge >= 0.05 ? 'edge-value' : 'edge-small';
  slot.innerHTML = '<span class="edge ' + cls + '">Value on ' + best.team + ' — Kalshi ' + (best.edge*100).toFixed(1) + ' pts higher than Real</span>';
}

async function refresh() {
  try {
    const res = await fetch('/api/games');
    const data = await res.json();
    render(data);
  } catch (e) {
    document.getElementById('status').textContent = 'Could not reach local server: ' + e;
  }
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


# Start the background poller as soon as the module loads, so it also
# works when run under a production server like gunicorn (which imports
# this file rather than calling the block below).
_poller_started = False
_poller_lock = threading.Lock()


def ensure_poller_started():
    global _poller_started
    with _poller_lock:
        if not _poller_started:
            t = threading.Thread(target=poll_loop, daemon=True)
            t.start()
            _poller_started = True


ensure_poller_started()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print(f"Starting MLB edge dashboard on {host}:{port}")
    app.run(host=host, port=port, debug=False)
