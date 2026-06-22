"""
Alpaca Interface
=================
Trades crypto-correlated stocks (MSTR, COIN, MARA) on Alpaca Markets
when strong BTC/ETH signals fire on Kraken.

Strategy rationale:
  BTC pumps → MSTR/COIN/MARA follow 1-3 hours later (high correlation ~0.85)
  This captures the lag between crypto moves and stock market reaction.

Authentication:
  ALPACA_API_KEY     — from alpaca.markets -> Account -> API Keys
  ALPACA_API_SECRET  — same
  ALPACA_BASE_URL    — https://paper-api.alpaca.markets (paper)
                       https://api.alpaca.markets (live)

Set all three in Railway Variables.
"""

import os
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_PAPER_URL = "https://paper-api.alpaca.markets"
_DEFAULT_LIVE_URL  = "https://api.alpaca.markets"

# Crypto-correlated stocks to trade
# When BTC signal fires → buy these; when BTC sell → exit
BTC_CORRELATES = ["MSTR", "COIN", "MARA"]
ETH_CORRELATES = ["COIN"]           # COIN tracks ETH too

# Allocation per correlated stock: % of Alpaca portfolio value
ALPACA_ALLOCATION_PCT = 5.0


class AlpacaClient:
    """Lightweight REST wrapper for the Alpaca v2 broker API."""

    def __init__(self):
        self.api_key    = os.getenv("ALPACA_API_KEY", "")
        self.api_secret = os.getenv("ALPACA_API_SECRET", "")
        self.base_url   = os.getenv("ALPACA_BASE_URL", _DEFAULT_PAPER_URL).rstrip("/")
        self.paper_mode = "paper" in self.base_url
        self._headers   = {
            "APCA-API-KEY-ID":     self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type":        "application/json",
        }

    def _configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        if not self._configured():
            return None
        try:
            r = requests.get(
                f"{self.base_url}/v2{path}",
                headers=self._headers,
                params=params or {},
                timeout=10,
            )
            if r.status_code == 200:
                return r.json()
            logger.debug("Alpaca GET %s → %d: %s", path, r.status_code, r.text[:120])
        except Exception as exc:
            logger.debug("Alpaca GET %s failed: %s", path, exc)
        return None

    def _post(self, path: str, body: dict) -> Optional[dict]:
        if not self._configured():
            return None
        try:
            r = requests.post(
                f"{self.base_url}/v2{path}",
                headers=self._headers,
                json=body,
                timeout=10,
            )
            if r.status_code in (200, 201):
                return r.json()
            logger.warning("Alpaca POST %s → %d: %s", path, r.status_code, r.text[:200])
        except Exception as exc:
            logger.debug("Alpaca POST %s failed: %s", path, exc)
        return None

    def _delete(self, path: str) -> bool:
        if not self._configured():
            return False
        try:
            r = requests.delete(
                f"{self.base_url}/v2{path}",
                headers=self._headers,
                timeout=10,
            )
            return r.status_code in (200, 204)
        except Exception:
            return False

    # ── Account ───────────────────────────────────────────────────────────────

    def get_account(self) -> Optional[dict]:
        return self._get("/account")

    def get_portfolio_value(self) -> float:
        """Return total portfolio value in USD."""
        acc = self.get_account()
        if acc:
            return float(acc.get("portfolio_value") or acc.get("equity") or 0)
        return 0.0

    def get_buying_power(self) -> float:
        acc = self.get_account()
        if acc:
            return float(acc.get("buying_power") or 0)
        return 0.0

    # ── Positions ─────────────────────────────────────────────────────────────

    def get_positions(self) -> list:
        result = self._get("/positions")
        return result if isinstance(result, list) else []

    def get_position(self, symbol: str) -> Optional[dict]:
        return self._get(f"/positions/{symbol}")

    def has_position(self, symbol: str) -> bool:
        pos = self.get_position(symbol)
        return bool(pos and float(pos.get("qty", 0)) > 0)

    # ── Orders ────────────────────────────────────────────────────────────────

    def market_buy(self, symbol: str, notional_usd: float) -> Optional[dict]:
        """Buy $notional_usd worth of symbol at market price (fractional shares)."""
        if notional_usd < 1.0:
            logger.info("Alpaca: notional $%.2f too small for %s, skipping", notional_usd, symbol)
            return None
        body = {
            "symbol":        symbol,
            "notional":      str(round(notional_usd, 2)),
            "side":          "buy",
            "type":          "market",
            "time_in_force": "day",
        }
        result = self._post("/orders", body)
        if result:
            logger.info("Alpaca BUY %s $%.2f → order %s %s",
                        symbol, notional_usd,
                        result.get("id", "?"), result.get("status", "?"))
        return result

    def market_sell_all(self, symbol: str) -> Optional[dict]:
        """Close entire position in symbol at market."""
        if not self.has_position(symbol):
            return None
        result = self._delete(f"/positions/{symbol}")
        if result:
            logger.info("Alpaca SELL ALL %s", symbol)
        return result

    def close_all_positions(self) -> None:
        """Emergency: close every open position."""
        self._delete("/positions")
        logger.info("Alpaca: closed all positions")

    # ── Market status ─────────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        """True when US stock market is open for trading."""
        clock = self._get("/clock")
        if clock:
            return bool(clock.get("is_open", False))
        return False


# ── Singleton ─────────────────────────────────────────────────────────────────

_client: Optional[AlpacaClient] = None


def get_client() -> AlpacaClient:
    global _client
    if _client is None:
        _client = AlpacaClient()
    return _client


def is_available() -> bool:
    c = get_client()
    return c._configured()
