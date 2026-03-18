"""
High-Frequency Load Testing Engine
Simulates up to 100,000 orders/second from 10,000 traders.
Measures: throughput, matching latency, queue depth, system stress.
"""

from __future__ import annotations
import asyncio
import json
import time
import uuid
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Deque, Optional

from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
import aiohttp

KAFKA_BOOTSTRAP = "kafka:9092"
OMS_URL         = "http://order-service:8001"

# ─── Benchmark Config ─────────────────────────────────────────────────────────

@dataclass
class LoadTestConfig:
    num_traders     : int   = 10_000
    target_ops      : int   = 100_000   # orders/sec
    ramp_up_seconds : int   = 10        # gradually increase load
    duration_seconds: int   = 60        # total test duration
    symbols         : List[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "TSLA", "NVDA", "AMZN",
        "GOOGL", "META", "SPY", "QQQ", "BRK",
    ])

# ─── Metrics Collector ────────────────────────────────────────────────────────

@dataclass
class LatencyBucket:
    ts       : float
    latency_us: float

class MetricsCollector:
    def __init__(self, window_seconds: int = 10):
        self.window = window_seconds
        self._send_ts    : Dict[str, float] = {}   # order_id → send ns
        self._latencies  : Deque[LatencyBucket] = deque(maxlen=200_000)
        self._sent_count : int  = 0
        self._recv_count : int  = 0
        self._errors     : int  = 0
        self._start_ts   : float = time.monotonic()

        # Rolling window
        self._window_sent: Deque[float] = deque(maxlen=1_000_000)  # timestamps

    def record_sent(self, order_id: str):
        now = time.monotonic()
        self._send_ts[order_id] = now
        self._window_sent.append(now)
        self._sent_count += 1

    def record_received(self, order_id: str):
        now = time.monotonic()
        sent = self._send_ts.pop(order_id, None)
        if sent is not None:
            lat_us = (now - sent) * 1_000_000
            self._latencies.append(LatencyBucket(ts=now, latency_us=lat_us))
        self._recv_count += 1

    def record_error(self):
        self._errors += 1

    def current_tps(self) -> float:
        """Orders sent in the last 1 second."""
        now = time.monotonic()
        cutoff = now - 1.0
        recent = sum(1 for ts in self._window_sent if ts >= cutoff)
        return float(recent)

    def snapshot(self) -> dict:
        now    = time.monotonic()
        elapsed = max(now - self._start_ts, 0.001)

        lats = [b.latency_us for b in self._latencies]
        if lats:
            lats_sorted = sorted(lats)
            n = len(lats_sorted)
            p = lambda pct: lats_sorted[int(n * pct / 100)]
            lat_stats = {
                "mean_us"  : statistics.mean(lats),
                "median_us": statistics.median(lats),
                "p95_us"   : p(95),
                "p99_us"   : p(99),
                "p999_us"  : lats_sorted[min(n-1, int(n*999/1000))],
                "max_us"   : lats_sorted[-1],
                "samples"  : n,
            }
        else:
            lat_stats = {k: 0 for k in
                         ["mean_us","median_us","p95_us","p99_us","p999_us","max_us","samples"]}

        return {
            "elapsed_s"      : round(elapsed, 1),
            "total_sent"     : self._sent_count,
            "total_received" : self._recv_count,
            "total_errors"   : self._errors,
            "avg_tps"        : round(self._sent_count / elapsed, 1),
            "current_tps"    : round(self.current_tps(), 1),
            "in_flight"      : len(self._send_ts),
            "latency"        : lat_stats,
        }

    def report(self):
        s = self.snapshot()
        print("=" * 60)
        print("  LOAD TEST RESULTS")
        print("=" * 60)
        print(f"  Duration      : {s['elapsed_s']:.1f}s")
        print(f"  Total Sent    : {s['total_sent']:,}")
        print(f"  Total Received: {s['total_received']:,}")
        print(f"  Errors        : {s['total_errors']:,}")
        print(f"  Avg Throughput: {s['avg_tps']:,.0f} orders/sec")
        print(f"  Peak TPS (1s) : {s['current_tps']:,.0f} orders/sec")
        print("-" * 60)
        lat = s["latency"]
        print(f"  Latency Mean  : {lat['mean_us']:.1f} µs")
        print(f"  Latency P50   : {lat['median_us']:.1f} µs")
        print(f"  Latency P95   : {lat['p95_us']:.1f} µs")
        print(f"  Latency P99   : {lat['p99_us']:.1f} µs")
        print(f"  Latency P99.9 : {lat['p999_us']:.1f} µs")
        print(f"  Latency Max   : {lat['max_us']:.1f} µs")
        print("=" * 60)


# ─── Order Generator ──────────────────────────────────────────────────────────

import random

PRICE_MAP = {s: random.uniform(50, 500)
             for s in ["AAPL","MSFT","TSLA","NVDA","AMZN",
                       "GOOGL","META","SPY","QQQ","BRK"]}

def make_order(trader_idx: int, symbol: str) -> dict:
    mid   = PRICE_MAP.get(symbol, 100.0)
    PRICE_MAP[symbol] = mid * (1 + random.gauss(0, 0.0001))
    side  = random.choice(["BUY", "SELL"])
    price = round(mid * random.uniform(0.998, 1.002), 2)
    qty   = random.randint(1, 1000)
    otype = random.choices(["LIMIT", "MARKET"], weights=[0.9, 0.1])[0]
    oid   = f"LT-{trader_idx:06d}-{uuid.uuid4().hex[:8].upper()}"
    return {
        "order_id"  : oid,
        "trader_id" : f"LT-TRADER-{trader_idx:06d}",
        "symbol"    : symbol,
        "side"      : side,
        "order_type": otype,
        "price"     : price if otype == "LIMIT" else None,
        "quantity"  : qty,
        "event"     : "ORDER_SUBMITTED",
        "ts_ns"     : time.time_ns(),
    }


# ─── Load Tester ─────────────────────────────────────────────────────────────

class LoadTester:
    def __init__(self, config: LoadTestConfig = None):
        self.cfg     = config or LoadTestConfig()
        self.metrics = MetricsCollector()
        self._running = False

    async def _producer_worker(self, producer: AIOKafkaProducer,
                               worker_id: int, orders_per_sec: float,
                               symbols: List[str]):
        """Each worker owns a slice of traders and pumps at its share of TPS."""
        traders_per_worker = max(1, self.cfg.num_traders // max(1, worker_id+1))
        interval = 1.0 / max(orders_per_sec, 1)
        trader_offset = worker_id * traders_per_worker

        while self._running:
            t_start = time.monotonic()
            trader_idx = trader_offset + random.randint(0, traders_per_worker - 1)
            symbol     = random.choice(symbols)
            order      = make_order(trader_idx, symbol)

            try:
                await producer.send(
                    "orders",
                    value=json.dumps(order).encode(),
                    key=order["order_id"].encode(),
                )
                self.metrics.record_sent(order["order_id"])
            except Exception:
                self.metrics.record_error()

            elapsed = time.monotonic() - t_start
            await asyncio.sleep(max(0, interval - elapsed))

    async def _consumer_worker(self, consumer: AIOKafkaConsumer):
        """Listens to trades topic to measure end-to-end latency."""
        async for msg in consumer:
            if not self._running:
                break
            try:
                event = json.loads(msg.value.decode())
                oid   = event.get("buy_order_id") or event.get("order_id","")
                if oid:
                    self.metrics.record_received(oid)
            except Exception:
                pass

    async def _stats_reporter(self, interval: float = 5.0):
        while self._running:
            await asyncio.sleep(interval)
            s = self.metrics.snapshot()
            print(f"[LoadTest] elapsed={s['elapsed_s']}s "
                  f"sent={s['total_sent']:,} "
                  f"tps_avg={s['avg_tps']:,.0f} "
                  f"tps_now={s['current_tps']:,.0f} "
                  f"p99={s['latency']['p99_us']:.1f}µs "
                  f"in_flight={s['in_flight']:,}")

    async def run(self):
        cfg = self.cfg
        self._running = True

        # Producers — one per 1000 target TPS
        num_workers = max(1, cfg.target_ops // 1_000)
        tps_per_worker = cfg.target_ops / num_workers

        producer = AIOKafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP,
            value_serializer=None,  # raw bytes
            compression_type="lz4",
            max_batch_size=1024*1024,
            linger_ms=2,
        )
        consumer = AIOKafkaConsumer(
            "trades",
            bootstrap_servers=KAFKA_BOOTSTRAP,
            group_id=f"load-tester-{uuid.uuid4().hex[:6]}",
            auto_offset_reset="latest",
        )

        await producer.start()
        await consumer.start()
        print(f"[LoadTest] Starting: workers={num_workers} "
              f"target={cfg.target_ops:,}/s "
              f"duration={cfg.duration_seconds}s")

        # Ramp-up: start at 10% load
        ramp_factor = 0.1
        ramp_increment = (1.0 - ramp_factor) / max(cfg.ramp_up_seconds, 1)

        workers = []
        for i in range(num_workers):
            w = asyncio.create_task(
                self._producer_worker(producer, i,
                                      tps_per_worker * ramp_factor,
                                      cfg.symbols)
            )
            workers.append(w)

        reporter  = asyncio.create_task(self._stats_reporter())
        cons_task = asyncio.create_task(self._consumer_worker(consumer))

        # Ramp loop
        for _ in range(cfg.ramp_up_seconds):
            await asyncio.sleep(1.0)
            ramp_factor = min(1.0, ramp_factor + ramp_increment)

        # Full load
        remaining = cfg.duration_seconds - cfg.ramp_up_seconds
        await asyncio.sleep(max(0, remaining))

        self._running = False
        for w in workers:
            w.cancel()
        reporter.cancel()
        cons_task.cancel()

        await producer.stop()
        await consumer.stop()

        self.metrics.report()
        return self.metrics.snapshot()


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="HFT Load Tester")
    parser.add_argument("--traders",    type=int, default=10_000)
    parser.add_argument("--target-ops", type=int, default=100_000)
    parser.add_argument("--duration",   type=int, default=60)
    parser.add_argument("--ramp-up",    type=int, default=10)
    args = parser.parse_args()

    cfg = LoadTestConfig(
        num_traders      = args.traders,
        target_ops       = args.target_ops,
        duration_seconds = args.duration,
        ramp_up_seconds  = args.ramp_up,
    )
    tester = LoadTester(cfg)
    asyncio.run(tester.run())
