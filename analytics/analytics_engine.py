"""
Market Microstructure Analytics Engine
Consumes market-data and trades from Kafka.
Computes: spread, depth, imbalance, volatility, VWAP, toxicity.
Broadcasts computed metrics back to market-data topic.
"""

from __future__ import annotations
import asyncio
import json
import time
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

KAFKA_BOOTSTRAP = "kafka:9092"


# ─── Per-Symbol Metrics State ─────────────────────────────────────────────────

@dataclass
class SymbolMetrics:
    symbol       : str
    price_history: deque = field(default_factory=lambda: deque(maxlen=500))
    volume_history: deque= field(default_factory=lambda: deque(maxlen=500))
    trade_prices  : deque = field(default_factory=lambda: deque(maxlen=200))
    trade_volumes : deque = field(default_factory=lambda: deque(maxlen=200))
    buy_volume    : float = 0.0
    sell_volume   : float = 0.0
    total_volume  : float = 0.0
    tick_count    : int   = 0
    last_computed : float = 0.0

    # Spread tracking
    best_bid      : float = 0.0
    best_ask      : float = 0.0
    spread_history: deque = field(default_factory=lambda: deque(maxlen=100))

    # Order book depth
    bid_depth     : float = 0.0
    ask_depth     : float = 0.0

    def update_book(self, snapshot: dict):
        bids = snapshot.get("bids", [])
        asks = snapshot.get("asks", [])
        self.best_bid = snapshot.get("best_bid", 0)
        self.best_ask = snapshot.get("best_ask", 0)
        if self.best_bid > 0 and self.best_ask > 0:
            self.spread_history.append(self.best_ask - self.best_bid)
        self.bid_depth = sum(l.get("total_qty", 0) for l in bids)
        self.ask_depth = sum(l.get("total_qty", 0) for l in asks)

    def update_trade(self, price: float, qty: int, side: str):
        self.price_history.append(price)
        self.volume_history.append(qty)
        self.trade_prices.append(price)
        self.trade_volumes.append(qty)
        self.total_volume += qty
        if side == "BUY":
            self.buy_volume += qty
        else:
            self.sell_volume += qty
        self.tick_count += 1

    # ── Computed Properties ──────────────────────────────────────────────────

    def bid_ask_spread(self) -> float:
        if self.spread_history:
            return float(np.mean(self.spread_history))
        return 0.0

    def relative_spread(self) -> float:
        mid = (self.best_bid + self.best_ask) / 2 if self.best_ask > 0 else 0
        return self.bid_ask_spread() / mid if mid > 0 else 0.0

    def order_imbalance(self) -> float:
        """(buy_vol - sell_vol) / (buy_vol + sell_vol). Range [-1, 1]."""
        total = self.buy_volume + self.sell_volume
        if total == 0:
            return 0.0
        return (self.buy_volume - self.sell_volume) / total

    def book_imbalance(self) -> float:
        total = self.bid_depth + self.ask_depth
        if total == 0:
            return 0.0
        return (self.bid_depth - self.ask_depth) / total

    def realised_volatility(self, window: int = 20) -> float:
        """Annualised realised vol from log returns."""
        prices = list(self.price_history)[-window-1:]
        if len(prices) < 5:
            return 0.0
        rets = np.diff(np.log(prices))
        return float(np.std(rets) * np.sqrt(252 * 6.5 * 3600))

    def vwap(self) -> float:
        prices  = list(self.trade_prices)
        volumes = list(self.trade_volumes)
        if not prices:
            return 0.0
        return float(np.average(prices, weights=volumes))

    def kyle_lambda(self) -> float:
        """
        Simplified Kyle's Lambda — price impact per unit volume.
        Lambda = ΔP / Q. Higher = more illiquid.
        """
        prices  = list(self.price_history)[-20:]
        volumes = list(self.volume_history)[-20:]
        if len(prices) < 2:
            return 0.0
        delta_p = abs(prices[-1] - prices[0])
        total_q = max(sum(volumes), 1)
        return delta_p / total_q

    def to_dict(self) -> dict:
        return {
            "symbol"           : self.symbol,
            "type"             : "microstructure",
            "best_bid"         : round(self.best_bid, 4),
            "best_ask"         : round(self.best_ask, 4),
            "mid_price"        : round((self.best_bid + self.best_ask) / 2, 4),
            "spread_abs"       : round(self.bid_ask_spread(), 4),
            "spread_rel_bps"   : round(self.relative_spread() * 10_000, 2),
            "order_imbalance"  : round(self.order_imbalance(), 4),
            "book_imbalance"   : round(self.book_imbalance(), 4),
            "bid_depth"        : self.bid_depth,
            "ask_depth"        : self.ask_depth,
            "realised_vol"     : round(self.realised_volatility(), 4),
            "vwap"             : round(self.vwap(), 4),
            "kyle_lambda"      : round(self.kyle_lambda(), 8),
            "buy_volume"       : self.buy_volume,
            "sell_volume"      : self.sell_volume,
            "total_volume"     : self.total_volume,
            "tick_count"       : self.tick_count,
            "ts_ns"            : time.time_ns(),
        }


# ─── Analytics Engine ─────────────────────────────────────────────────────────

class AnalyticsEngine:
    def __init__(self, publish_interval: float = 1.0):
        self.metrics: Dict[str, SymbolMetrics] = defaultdict(
            lambda: SymbolMetrics(symbol="unknown"))
        self.publish_interval = publish_interval

    def _sym(self, symbol: str) -> SymbolMetrics:
        m = self.metrics[symbol]
        m.symbol = symbol
        return m

    def on_book_snapshot(self, data: dict):
        sym = data.get("symbol", "")
        self._sym(sym).update_book(data)

    def on_trade(self, data: dict):
        sym  = data.get("symbol", "")
        price= data.get("price", 0.0)
        qty  = data.get("quantity", 0)
        side = data.get("side", "BUY")
        self._sym(sym).update_trade(price, qty, side)

    def all_snapshots(self) -> List[dict]:
        return [m.to_dict() for m in self.metrics.values()
                if m.tick_count > 0 or m.best_bid > 0]


async def run():
    engine = AnalyticsEngine(publish_interval=1.0)

    consumer = AIOKafkaConsumer(
        "market-data", "trades",
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id="analytics-engine",
        auto_offset_reset="latest",
        value_deserializer=lambda m: json.loads(m.decode()),
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode(),
        compression_type="lz4",
    )
    await consumer.start()
    await producer.start()
    print("[Analytics] Running …")

    async def periodic_publish():
        while True:
            await asyncio.sleep(engine.publish_interval)
            for snap in engine.all_snapshots():
                await producer.send("market-data", value=snap,
                                    key=snap["symbol"].encode())

    asyncio.create_task(periodic_publish())

    try:
        async for msg in consumer:
            data  = msg.value
            dtype = data.get("type", "")

            if dtype == "book_snapshot" or "bids" in data:
                engine.on_book_snapshot(data)
            elif msg.topic == "trades" or "trade_id" in data:
                engine.on_trade(data)
    finally:
        await consumer.stop()
        await producer.stop()


if __name__ == "__main__":
    asyncio.run(run())
