"""
Reinforcement Learning Trading Agent
Uses PPO (via Stable Baselines 3) to learn optimal trading strategy.
Observes: order book depth, price history, volume, position
Actions: BUY / SELL / HOLD
Reward: risk-adjusted PnL
"""

from __future__ import annotations
import asyncio
import json
import time
import uuid
from collections import deque
from typing import Optional, Tuple

import numpy as np
import gym
from gym import spaces
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

try:
    from stable_baselines3 import PPO
    from stable_baselines3.common.env_util import make_vec_env
    SB3_AVAILABLE = True
except ImportError:
    SB3_AVAILABLE = False
    print("[RL Agent] stable-baselines3 not installed; using heuristic fallback")

KAFKA_BOOTSTRAP = "kafka:9092"


# ─── Trading Environment ──────────────────────────────────────────────────────

class TradingEnv(gym.Env):
    """
    OpenAI Gym environment wrapping a single-symbol limit-order book feed.
    Observation space: 40-dimensional vector
      [0:10]  bid prices (normalised)
      [10:20] bid quantities (normalised)
      [20:30] ask prices (normalised)
      [30:40] ask quantities (normalised)
      + price_return[-5:] + volume[-5:] + position + unrealised_pnl
    Action space: Discrete(3) → 0=HOLD, 1=BUY, 2=SELL
    """
    metadata = {"render.modes": []}

    OBS_DIM    = 62
    MAX_POS    = 1_000      # max shares
    TRADE_SIZE = 100
    TICK_FEE   = 0.0001     # 1bps per trade

    def __init__(self, price_stream: deque, book_stream: deque):
        super().__init__()
        self.price_stream = price_stream
        self.book_stream  = book_stream

        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf,
            shape=(self.OBS_DIM,), dtype=np.float32
        )
        self.action_space = spaces.Discrete(3)

        self._reset_state()

    def _reset_state(self):
        self.position      = 0
        self.cash          = 100_000.0
        self.entry_price   = 0.0
        self.price_history = deque(maxlen=50)
        self.vol_history   = deque(maxlen=50)
        self.step_count    = 0

    def reset(self):
        self._reset_state()
        return self._obs()

    def _obs(self) -> np.ndarray:
        obs = np.zeros(self.OBS_DIM, dtype=np.float32)
        mid = self.price_history[-1] if self.price_history else 100.0

        # Price returns (last 5)
        prices = list(self.price_history)[-5:]
        for i, p in enumerate(prices):
            obs[i] = (p - mid) / (mid + 1e-8)

        # Volume normalised (last 5)
        vols = list(self.vol_history)[-5:]
        for i, v in enumerate(vols):
            obs[5 + i] = v / 10_000.0

        # Book from stream
        if self.book_stream:
            book = self.book_stream[-1]
            bids = book.get("bids", [])[:5]
            asks = book.get("asks", [])[:5]
            for j, lvl in enumerate(bids):
                obs[10 + j] = (lvl.get("price", mid) - mid) / (mid + 1e-8)
                obs[15 + j] = lvl.get("total_qty", 0) / 10_000.0
            for j, lvl in enumerate(asks):
                obs[20 + j] = (lvl.get("price", mid) - mid) / (mid + 1e-8)
                obs[25 + j] = lvl.get("total_qty", 0) / 10_000.0

        obs[30] = self.position / self.MAX_POS
        obs[31] = (self.cash - 100_000) / 100_000

        return obs

    def step(self, action: int) -> Tuple[np.ndarray, float, bool, dict]:
        self.step_count += 1
        mid = self.price_history[-1] if self.price_history else 100.0
        reward = 0.0

        if action == 1 and self.position < self.MAX_POS:   # BUY
            cost = mid * self.TRADE_SIZE * (1 + self.TICK_FEE)
            if self.cash >= cost:
                self.cash     -= cost
                self.position += self.TRADE_SIZE
                self.entry_price = mid
                reward -= self.TICK_FEE * mid * self.TRADE_SIZE  # transaction cost

        elif action == 2 and self.position > 0:            # SELL
            proceeds = mid * self.TRADE_SIZE * (1 - self.TICK_FEE)
            self.cash     += proceeds
            self.position -= self.TRADE_SIZE
            pnl = (mid - self.entry_price) * self.TRADE_SIZE
            reward += pnl - abs(pnl) * 0.1   # risk-penalty

        # Mark-to-market reward
        unrealised = (mid - self.entry_price) * self.position
        reward += unrealised * 0.001

        # Inventory penalty
        reward -= (self.position / self.MAX_POS) ** 2 * 0.01

        done = self.step_count >= 2_000 or self.cash < 1_000
        return self._obs(), reward, done, {}


# ─── Heuristic Fallback Agent ─────────────────────────────────────────────────

class HeuristicAgent:
    """Simple momentum agent when SB3 is unavailable."""
    def __init__(self):
        self.price_history = deque(maxlen=30)
        self.position = 0

    def act(self, mid: float) -> int:
        self.price_history.append(mid)
        if len(self.price_history) < 10:
            return 0  # HOLD
        recent = list(self.price_history)
        sma_fast = sum(recent[-5:]) / 5
        sma_slow = sum(recent[-20:]) / 20 if len(recent) >= 20 else sma_fast
        if sma_fast > sma_slow * 1.001 and self.position <= 0:
            return 1  # BUY
        elif sma_fast < sma_slow * 0.999 and self.position >= 0:
            return 2  # SELL
        return 0  # HOLD


# ─── Kafka Agent Loop ─────────────────────────────────────────────────────────

class RLTradingAgent:
    def __init__(self, symbol: str = "AAPL", model_path: Optional[str] = None):
        self.symbol       = symbol
        self.agent_id     = f"RL-AGENT-{uuid.uuid4().hex[:8].upper()}"
        self.price_stream = deque(maxlen=500)
        self.book_stream  = deque(maxlen=100)

        self.env   = TradingEnv(self.price_stream, self.book_stream)
        self.obs   = self.env.reset()

        if SB3_AVAILABLE:
            if model_path:
                self.model = PPO.load(model_path, env=self.env)
                print(f"[RL Agent] Loaded model from {model_path}")
            else:
                print("[RL Agent] Training new PPO model …")
                vec_env = make_vec_env(lambda: TradingEnv(
                    self.price_stream, self.book_stream), n_envs=1)
                self.model = PPO(
                    "MlpPolicy", vec_env,
                    learning_rate=3e-4,
                    n_steps=512,
                    batch_size=64,
                    n_epochs=5,
                    gamma=0.99,
                    gae_lambda=0.95,
                    clip_range=0.2,
                    verbose=0,
                )
            self.use_rl = True
        else:
            self.heuristic = HeuristicAgent()
            self.use_rl    = False

        self.total_orders = 0
        self.pnl_history  = deque(maxlen=1000)

    async def run(self):
        consumer = AIOKafkaConsumer(
            "market-data",
            bootstrap_servers=KAFKA_BOOTSTRAP,
            group_id=f"rl-agent-{self.agent_id}",
            auto_offset_reset="latest",
            value_deserializer=lambda m: json.loads(m.decode()),
        )
        producer = AIOKafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP,
            value_serializer=lambda v: json.dumps(v).encode(),
        )
        await consumer.start()
        await producer.start()
        print(f"[RL Agent] {self.agent_id} running on {self.symbol}")

        try:
            async for msg in consumer:
                data = msg.value
                if data.get("symbol") != self.symbol:
                    continue

                mid = data.get("mid_price", 0)
                if mid <= 0:
                    continue

                self.price_stream.append(mid)
                if "bids" in data:
                    self.book_stream.append(data)

                # Get action
                if self.use_rl:
                    action, _ = self.model.predict(self.obs,
                                                    deterministic=False)
                    self.obs, reward, done, _ = self.env.step(int(action))
                    if done:
                        self.obs = self.env.reset()
                    self.pnl_history.append(reward)
                else:
                    action = self.heuristic.act(mid)

                action_map = {0: "HOLD", 1: "BUY", 2: "SELL"}
                action_str = action_map[int(action)]

                if action_str != "HOLD":
                    order = {
                        "order_id"  : f"RL-{uuid.uuid4().hex[:10].upper()}",
                        "trader_id" : self.agent_id,
                        "symbol"    : self.symbol,
                        "side"      : action_str,
                        "order_type": "LIMIT",
                        "price"     : round(mid * (1.0005 if action_str=="BUY"
                                                   else 0.9995), 2),
                        "quantity"  : 100,
                        "event"     : "ORDER_SUBMITTED",
                        "ts_ns"     : time.time_ns(),
                        "agent_type": "RL",
                    }
                    await producer.send("orders", value=order,
                                        key=order["order_id"].encode())
                    self.total_orders += 1

                    # Broadcast agent action to dashboard
                    await producer.send("market-data", value={
                        "type"     : "agent_action",
                        "agent_id" : self.agent_id,
                        "action"   : action_str,
                        "symbol"   : self.symbol,
                        "price"    : mid,
                        "ts_ns"    : time.time_ns(),
                    })

        finally:
            await consumer.stop()
            await producer.stop()


if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    agent  = RLTradingAgent(symbol=symbol)
    asyncio.run(agent.run())
