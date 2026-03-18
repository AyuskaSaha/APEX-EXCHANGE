"""
Risk Management Engine
Consumes orders from Kafka, validates against risk limits,
publishes risk-alerts for violations.
"""

from __future__ import annotations
import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

KAFKA_BOOTSTRAP = "kafka:9092"

# ─── Per-Trader Risk State ────────────────────────────────────────────────────

@dataclass
class TraderRiskState:
    trader_id      : str
    net_position   : Dict[str, int]   = field(default_factory=dict)
    daily_pnl      : float            = 0.0
    order_count_1s : int              = 0
    last_reset_ts  : float            = field(default_factory=time.time)
    open_orders    : int              = 0

    # ── Limits ──────────────────────────────
    MAX_POSITION_PER_SYMBOL: int   = 500_000
    MAX_DAILY_LOSS         : float = 200_000.0
    MAX_ORDERS_PER_SEC     : int   = 500
    MAX_OPEN_ORDERS        : int   = 1_000

    def reset_rate_if_needed(self):
        now = time.time()
        if now - self.last_reset_ts >= 1.0:
            self.order_count_1s = 0
            self.last_reset_ts  = now

    def check_order(self, symbol: str, side: str, qty: int,
                    price: float) -> Optional[str]:
        self.reset_rate_if_needed()

        # Rate limit
        self.order_count_1s += 1
        if self.order_count_1s > self.MAX_ORDERS_PER_SEC:
            return f"Rate limit exceeded: {self.order_count_1s}/s"

        # Open order limit
        if self.open_orders >= self.MAX_OPEN_ORDERS:
            return f"Too many open orders: {self.open_orders}"

        # Prospective position
        delta = qty if side == "BUY" else -qty
        curr  = self.net_position.get(symbol, 0)
        if abs(curr + delta) > self.MAX_POSITION_PER_SYMBOL:
            return (f"Position limit breach: current={curr} "
                    f"delta={delta} limit={self.MAX_POSITION_PER_SYMBOL}")

        # Daily loss
        if self.daily_pnl < -self.MAX_DAILY_LOSS:
            return f"Daily loss limit: pnl={self.daily_pnl:.2f}"

        return None

    def on_fill(self, symbol: str, side: str, qty: int, price: float,
                is_full: bool):
        delta = qty if side == "BUY" else -qty
        self.net_position[symbol] = self.net_position.get(symbol, 0) + delta
        # Simplified PnL: cost basis not tracked here — extend for production
        if is_full:
            self.open_orders = max(0, self.open_orders - 1)

    def on_open(self):
        self.open_orders += 1

    def on_cancel(self):
        self.open_orders = max(0, self.open_orders - 1)


# ─── Engine ───────────────────────────────────────────────────────────────────

class RiskEngine:
    def __init__(self):
        self.traders: Dict[str, TraderRiskState] = defaultdict(
            lambda: TraderRiskState(trader_id="unknown")
        )
        self.alert_count = 0
        self.checked_count = 0

    def get_state(self, trader_id: str) -> TraderRiskState:
        if trader_id not in self.traders:
            s = TraderRiskState(trader_id=trader_id)
            self.traders[trader_id] = s
        return self.traders[trader_id]

    def process_order_event(self, event: dict) -> Optional[dict]:
        """Returns a risk alert dict if violation detected."""
        evt_type  = event.get("event", "")
        trader_id = event.get("trader_id", "")
        symbol    = event.get("symbol", "")
        side      = event.get("side", "")
        qty       = event.get("quantity", 0)
        price     = event.get("price") or 0.0

        state = self.get_state(trader_id)
        self.checked_count += 1

        if evt_type == "ORDER_SUBMITTED":
            err = state.check_order(symbol, side, qty, price)
            state.on_open()
            if err:
                self.alert_count += 1
                return {
                    "alert_type" : "RISK_VIOLATION",
                    "trader_id"  : trader_id,
                    "order_id"   : event.get("order_id"),
                    "symbol"     : symbol,
                    "reason"     : err,
                    "ts_ns"      : time.time_ns(),
                }

        elif evt_type == "ORDER_CANCELLED":
            state.on_cancel()

        elif evt_type == "TRADE_EXECUTED":
            is_full = event.get("order_status") == "FILLED"
            state.on_fill(symbol, side, qty, price, is_full)

        return None


# ─── Kafka Loop ───────────────────────────────────────────────────────────────

async def run():
    engine = RiskEngine()

    consumer = AIOKafkaConsumer(
        "orders", "trades",
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id="risk-engine",
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

    print("[RiskEngine] Running …")
    try:
        async for msg in consumer:
            event = msg.value
            alert = engine.process_order_event(event)
            if alert:
                print(f"[RISK ALERT] {alert}")
                await producer.send_and_wait(
                    "risk-alerts",
                    value=alert,
                    key=alert["trader_id"].encode(),
                )
            # Periodic stats
            if engine.checked_count % 10_000 == 0:
                print(f"[RiskEngine] checked={engine.checked_count} "
                      f"alerts={engine.alert_count}")
    finally:
        await consumer.stop()
        await producer.stop()


if __name__ == "__main__":
    asyncio.run(run())
