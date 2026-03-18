"""
Market Manipulation Detection Engine
Detects: spoofing, wash trading, pump-and-dump, layering.
Uses statistical models + pattern matching on order flow.
Publishes alerts to risk-alerts Kafka topic.
"""

from __future__ import annotations
import asyncio
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

KAFKA_BOOTSTRAP = "kafka:9092"


# ─── Order Flow Tracker ───────────────────────────────────────────────────────

@dataclass
class OrderFlowRecord:
    order_id : str
    trader_id: str
    symbol   : str
    side     : str
    price    : float
    quantity : int
    status   : str = "PENDING"
    submitted_at: float = field(default_factory=time.time)
    cancelled_at: Optional[float] = None

@dataclass
class TraderProfile:
    trader_id      : str
    orders         : deque = field(default_factory=lambda: deque(maxlen=500))
    cancel_rate_5s : float = 0.0
    large_cancel_streak: int = 0

    # Wash trade tracking
    known_counterparts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))

    # Stats
    submit_count  : int = 0
    cancel_count  : int = 0
    fill_count    : int = 0


# ─── Detectors ────────────────────────────────────────────────────────────────

class SpoofingDetector:
    """
    Spoofing: large orders placed and quickly cancelled to move price.
    Heuristic: >70% cancellation rate for large orders within 5 seconds.
    """
    CANCEL_WINDOW_S  = 5.0
    MIN_ORDER_SIZE   = 500
    ALERT_CANCEL_RATE= 0.70
    MIN_SAMPLES      = 10

    def check(self, profile: TraderProfile,
              new_cancel: OrderFlowRecord) -> Optional[str]:
        if new_cancel.quantity < self.MIN_ORDER_SIZE:
            return None

        now = time.time()
        recent = [o for o in profile.orders
                  if o.cancelled_at
                  and (now - o.submitted_at) <= self.CANCEL_WINDOW_S
                  and o.quantity >= self.MIN_ORDER_SIZE]

        submitted_large = [o for o in profile.orders
                           if (now - o.submitted_at) <= self.CANCEL_WINDOW_S
                           and o.quantity >= self.MIN_ORDER_SIZE]

        if len(submitted_large) < self.MIN_SAMPLES:
            return None

        cancel_rate = len(recent) / len(submitted_large)
        if cancel_rate >= self.ALERT_CANCEL_RATE:
            return (f"SPOOFING: cancel_rate={cancel_rate:.1%} "
                    f"large_orders={len(submitted_large)} "
                    f"in {self.CANCEL_WINDOW_S}s window")
        return None


class WashTradingDetector:
    """
    Wash trading: trader buys and sells with known affiliated entities.
    Simplified: same trader_id, opposite sides within 2 seconds.
    """
    WINDOW_S = 2.0

    def __init__(self):
        # (trader_id, symbol) → deque of recent orders
        self._recent: Dict[Tuple[str,str], deque] = defaultdict(
            lambda: deque(maxlen=200)
        )

    def record(self, order: OrderFlowRecord):
        key = (order.trader_id, order.symbol)
        self._recent[key].append(order)

    def check(self, order: OrderFlowRecord) -> Optional[str]:
        key = (order.trader_id, order.symbol)
        now = time.time()
        opposite_side = "SELL" if order.side == "BUY" else "BUY"
        matches = [o for o in self._recent[key]
                   if o.side == opposite_side
                   and abs(o.price - order.price) < order.price * 0.001
                   and (now - o.submitted_at) <= self.WINDOW_S]
        if len(matches) >= 3:
            return (f"WASH_TRADING: {len(matches)} opposing orders "
                    f"at ~same price within {self.WINDOW_S}s")
        return None


class PumpDumpDetector:
    """
    Pump & Dump: rapid coordinated buy orders causing spike, followed by dump.
    Detect price spike > 2σ combined with surge in buy-side order flow.
    """
    PRICE_SPIKE_SIGMA = 2.5
    VOLUME_MULT       = 3.0

    def __init__(self):
        self._price_hist : Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self._vol_hist   : Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self._buy_vol    : Dict[str, deque] = defaultdict(lambda: deque(maxlen=20))

    def record_trade(self, symbol: str, price: float, volume: int, side: str):
        self._price_hist[symbol].append(price)
        self._vol_hist[symbol].append(volume)
        if side == "BUY":
            self._buy_vol[symbol].append(volume)

    def check(self, symbol: str, price: float,
              volume: int, side: str) -> Optional[str]:
        ph = self._price_hist[symbol]
        vh = self._vol_hist[symbol]
        if len(ph) < 20:
            return None

        prices  = np.array(ph)
        returns = np.diff(prices) / prices[:-1]
        mean_r, std_r = returns.mean(), returns.std()

        if std_r < 1e-9:
            return None

        last_ret = (price - prices[-1]) / prices[-1]
        z_score  = (last_ret - mean_r) / std_r

        avg_vol  = np.mean(list(vh)[-20:]) if vh else 1
        vol_mult = volume / (avg_vol + 1)

        if z_score > self.PRICE_SPIKE_SIGMA and vol_mult > self.VOLUME_MULT:
            return (f"PUMP_DUMP: price_zscore={z_score:.2f} "
                    f"volume_multiplier={vol_mult:.1f}x")
        return None


class LayeringDetector:
    """
    Layering: placing many limit orders at multiple price levels to create
    false depth impression, then cancelling them.
    """
    LEVEL_THRESHOLD   = 8
    CANCEL_RATE_THRESH= 0.80

    def check(self, profile: TraderProfile) -> Optional[str]:
        recent = list(profile.orders)[-50:]
        if len(recent) < 15:
            return None

        # Count distinct price levels
        prices = set(round(o.price, 2) for o in recent)
        cancelled = sum(1 for o in recent if o.status == "CANCELLED")
        c_rate = cancelled / len(recent)

        if len(prices) >= self.LEVEL_THRESHOLD and c_rate >= self.CANCEL_RATE_THRESH:
            return (f"LAYERING: {len(prices)} price levels "
                    f"cancel_rate={c_rate:.1%}")
        return None


# ─── Surveillance Engine ──────────────────────────────────────────────────────

class MarketSurveillanceEngine:
    def __init__(self):
        self.profiles        : Dict[str, TraderProfile] = defaultdict(
            lambda: TraderProfile(trader_id="unknown"))
        self.order_registry  : Dict[str, OrderFlowRecord] = {}

        self.spoof_detector  = SpoofingDetector()
        self.wash_detector   = WashTradingDetector()
        self.pump_detector   = PumpDumpDetector()
        self.layer_detector  = LayeringDetector()

        self.alert_count = 0

    def _profile(self, trader_id: str) -> TraderProfile:
        p = self.profiles[trader_id]
        p.trader_id = trader_id
        return p

    def on_order_submitted(self, event: dict) -> List[dict]:
        rec = OrderFlowRecord(
            order_id  = event["order_id"],
            trader_id = event["trader_id"],
            symbol    = event["symbol"],
            side      = event["side"],
            price     = event.get("price") or 0.0,
            quantity  = event.get("quantity", 0),
        )
        self.order_registry[rec.order_id] = rec
        profile = self._profile(rec.trader_id)
        profile.orders.append(rec)
        profile.submit_count += 1
        self.wash_detector.record(rec)

        alerts = []
        # Wash trade check
        wash = self.wash_detector.check(rec)
        if wash:
            alerts.append(self._make_alert("WASH_TRADE", rec, wash))

        # Layering check (every 20 orders)
        if profile.submit_count % 20 == 0:
            layer = self.layer_detector.check(profile)
            if layer:
                alerts.append(self._make_alert("LAYERING", rec, layer))

        return alerts

    def on_order_cancelled(self, event: dict) -> List[dict]:
        order_id  = event.get("order_id", "")
        rec = self.order_registry.get(order_id)
        if not rec:
            return []

        rec.status      = "CANCELLED"
        rec.cancelled_at = time.time()
        profile = self._profile(rec.trader_id)
        profile.cancel_count += 1

        alerts = []
        spoof = self.spoof_detector.check(profile, rec)
        if spoof:
            alerts.append(self._make_alert("SPOOFING", rec, spoof))
        return alerts

    def on_trade_executed(self, event: dict) -> List[dict]:
        symbol = event.get("symbol", "")
        price  = event.get("price", 0.0)
        qty    = event.get("quantity", 0)
        side   = event.get("side", "BUY")

        self.pump_detector.record_trade(symbol, price, qty, side)
        pump = self.pump_detector.check(symbol, price, qty, side)
        if pump:
            return [self._make_alert("PUMP_DUMP",
                                     OrderFlowRecord("", "MARKET", symbol,
                                                     side, price, qty),
                                     pump)]
        return []

    def _make_alert(self, alert_type: str, rec: OrderFlowRecord,
                    detail: str) -> dict:
        self.alert_count += 1
        return {
            "alert_type" : alert_type,
            "trader_id"  : rec.trader_id,
            "order_id"   : rec.order_id,
            "symbol"     : rec.symbol,
            "detail"     : detail,
            "severity"   : "HIGH" if alert_type in ("SPOOFING", "WASH_TRADE")
                           else "MEDIUM",
            "ts_ns"      : time.time_ns(),
        }


# ─── Kafka Consumer Loop ──────────────────────────────────────────────────────

async def run():
    engine = MarketSurveillanceEngine()

    consumer = AIOKafkaConsumer(
        "orders", "trades",
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id="surveillance-engine",
        auto_offset_reset="latest",
        value_deserializer=lambda m: json.loads(m.decode()),
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode(),
    )
    await consumer.start()
    await producer.start()
    print("[Surveillance] Running …")

    try:
        async for msg in consumer:
            event  = msg.value
            topic  = msg.topic
            alerts = []

            if topic == "orders":
                etype = event.get("event", "")
                if etype == "ORDER_SUBMITTED":
                    alerts = engine.on_order_submitted(event)
                elif etype == "ORDER_CANCELLED":
                    alerts = engine.on_order_cancelled(event)
            elif topic == "trades":
                alerts = engine.on_trade_executed(event)

            for alert in alerts:
                print(f"[ALERT] {alert['alert_type']} trader={alert['trader_id']} "
                      f"detail={alert['detail']}")
                await producer.send_and_wait(
                    "risk-alerts",
                    value=alert,
                    key=alert["trader_id"].encode(),
                )

            if (engine.alert_count > 0 and
                    engine.alert_count % 100 == 0):
                print(f"[Surveillance] Total alerts: {engine.alert_count}")

    finally:
        await consumer.stop()
        await producer.stop()


if __name__ == "__main__":
    asyncio.run(run())
