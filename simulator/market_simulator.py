"""
Market Simulation Engine
Generates realistic order flow from 10,000+ synthetic traders.
Trader types: random, liquidity-provider, momentum-bot, mean-reversion-bot
"""

from __future__ import annotations
import asyncio
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
from aiokafka import AIOKafkaProducer

KAFKA_BOOTSTRAP = "kafka:9092"
SYMBOLS = ["AAPL", "MSFT", "TSLA", "NVDA", "AMZN", "GOOGL", "META", "SPY"]

class TraderType(Enum):
    RANDOM          = "random"
    LIQUIDITY_PROVIDER = "lp"
    MOMENTUM        = "momentum"
    MEAN_REVERSION  = "mean_rev"
    NOISE           = "noise"

@dataclass
class MarketState:
    """Shared market state per symbol."""
    symbol    : str
    mid_price : float
    volatility: float = 0.002  # 0.2% per tick
    last_trade_price: float = 0.0
    price_history: List[float] = field(default_factory=list)

    def tick(self):
        """GBM-style price walk."""
        shock = random.gauss(0, self.volatility)
        self.mid_price *= (1 + shock)
        self.mid_price = max(0.01, self.mid_price)
        self.price_history.append(self.mid_price)
        if len(self.price_history) > 200:
            self.price_history.pop(0)

    @property
    def sma20(self):
        h = self.price_history[-20:]
        return sum(h) / len(h) if h else self.mid_price

@dataclass
class SimTrader:
    trader_id  : str
    trader_type: TraderType
    symbols    : List[str]

    def generate_order(self, state: MarketState) -> Optional[dict]:
        if self.trader_type == TraderType.RANDOM:
            return self._random_order(state)
        elif self.trader_type == TraderType.LIQUIDITY_PROVIDER:
            return self._lp_order(state)
        elif self.trader_type == TraderType.MOMENTUM:
            return self._momentum_order(state)
        elif self.trader_type == TraderType.MEAN_REVERSION:
            return self._mean_rev_order(state)
        elif self.trader_type == TraderType.NOISE:
            return self._noise_order(state)
        return None

    def _base(self, state: MarketState, side: str,
              price: float, qty: int, otype: str = "LIMIT") -> dict:
        return {
            "order_id"  : f"SIM-{uuid.uuid4().hex[:10].upper()}",
            "trader_id" : self.trader_id,
            "symbol"    : state.symbol,
            "side"      : side,
            "order_type": otype,
            "price"     : round(price, 2),
            "quantity"  : qty,
            "event"     : "ORDER_SUBMITTED",
            "ts_ns"     : time.time_ns(),
        }

    def _random_order(self, state: MarketState) -> dict:
        side = random.choice(["BUY", "SELL"])
        spread = state.mid_price * 0.001
        price = state.mid_price + random.uniform(-spread*5, spread*5)
        qty   = random.randint(1, 500)
        otype = random.choices(["LIMIT","MARKET"], weights=[0.85, 0.15])[0]
        return self._base(state, side, price, qty, otype)

    def _lp_order(self, state: MarketState) -> dict:
        """Place tight two-sided quotes."""
        half_spread = state.mid_price * 0.0005
        side  = random.choice(["BUY", "SELL"])
        price = (state.mid_price - half_spread if side == "BUY"
                 else state.mid_price + half_spread)
        qty   = random.randint(100, 2000)
        return self._base(state, side, price, qty)

    def _momentum_order(self, state: MarketState) -> Optional[dict]:
        if len(state.price_history) < 20:
            return None
        trend = state.mid_price - state.price_history[-20]
        if abs(trend) < state.mid_price * 0.003:
            return None
        side  = "BUY" if trend > 0 else "SELL"
        price = state.mid_price * (1.001 if side == "BUY" else 0.999)
        qty   = random.randint(50, 300)
        return self._base(state, side, price, qty)

    def _mean_rev_order(self, state: MarketState) -> Optional[dict]:
        if len(state.price_history) < 20:
            return None
        dev = state.mid_price - state.sma20
        if abs(dev) < state.mid_price * 0.005:
            return None
        side  = "SELL" if dev > 0 else "BUY"
        price = state.mid_price
        qty   = random.randint(50, 200)
        return self._base(state, side, price, qty)

    def _noise_order(self, state: MarketState) -> dict:
        side  = random.choice(["BUY", "SELL"])
        price = state.mid_price * random.uniform(0.99, 1.01)
        qty   = random.randint(1, 50)
        return self._base(state, side, price, qty, "MARKET")


class MarketSimulator:
    def __init__(self,
                 num_traders      : int   = 10_000,
                 target_ops       : int   = 100_000,   # orders/sec
                 symbols          : List[str] = None,
                 kafka_bootstrap  : str   = KAFKA_BOOTSTRAP):

        self.symbols    = symbols or SYMBOLS
        self.target_ops = target_ops
        self.kafka_url  = kafka_bootstrap

        # Price state per symbol
        self.market_states: Dict[str, MarketState] = {
            s: MarketState(symbol=s,
                           mid_price=random.uniform(50, 500))
            for s in self.symbols
        }

        # Build trader pool
        weights = [0.40, 0.20, 0.15, 0.15, 0.10]
        types   = [TraderType.RANDOM, TraderType.LIQUIDITY_PROVIDER,
                   TraderType.MOMENTUM, TraderType.MEAN_REVERSION,
                   TraderType.NOISE]
        self.traders: List[SimTrader] = []
        for i in range(num_traders):
            ttype = random.choices(types, weights=weights)[0]
            syms  = random.sample(self.symbols, k=random.randint(1, 3))
            self.traders.append(
                SimTrader(trader_id=f"SIM-{ttype.value}-{i:06d}",
                          trader_type=ttype, symbols=syms)
            )

        self.total_sent = 0
        self.start_time = 0.0

    async def run(self):
        producer = AIOKafkaProducer(
            bootstrap_servers=self.kafka_url,
            value_serializer=lambda v: json.dumps(v).encode(),
            compression_type="lz4",
            max_batch_size=512 * 1024,
            linger_ms=5,
        )
        await producer.start()
        self.start_time = time.monotonic()

        print(f"[Simulator] Starting: {len(self.traders)} traders, "
              f"target {self.target_ops:,} orders/sec")

        batch_size = max(100, self.target_ops // 100)
        interval   = batch_size / self.target_ops   # seconds between batches

        try:
            while True:
                t_batch_start = time.monotonic()

                # Evolve prices
                for state in self.market_states.values():
                    state.tick()

                # Generate a batch of orders
                batch = []
                selected = random.sample(self.traders,
                                         min(batch_size, len(self.traders)))
                for trader in selected:
                    sym   = random.choice(trader.symbols)
                    state = self.market_states[sym]
                    order = trader.generate_order(state)
                    if order:
                        batch.append(order)

                # Publish
                tasks = [
                    producer.send(
                        "orders",
                        value=o,
                        key=o["order_id"].encode(),
                    )
                    for o in batch
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
                self.total_sent += len(batch)

                # Rate control
                elapsed  = time.monotonic() - t_batch_start
                sleep_ms = max(0, interval - elapsed)
                await asyncio.sleep(sleep_ms)

                if self.total_sent % 50_000 == 0:
                    dur = time.monotonic() - self.start_time
                    tps = self.total_sent / max(dur, 0.001)
                    print(f"[Simulator] sent={self.total_sent:,} "
                          f"tps={tps:,.0f}")
        finally:
            await producer.stop()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--traders",    type=int, default=10_000)
    parser.add_argument("--target-ops", type=int, default=100_000)
    args = parser.parse_args()

    sim = MarketSimulator(num_traders=args.traders,
                          target_ops=args.target_ops)
    asyncio.run(sim.run())
