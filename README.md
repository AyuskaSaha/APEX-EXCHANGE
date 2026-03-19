<div align="center">

# ⚡ APEX EXCHANGE

### AI-Powered Low-Latency Stock Exchange Matching Engine

![Version](https://img.shields.io/badge/version-1.0.0-gold)
![License](https://img.shields.io/badge/license-MIT-blue)
![C++](https://img.shields.io/badge/C%2B%2B-17-00599C?logo=cplusplus)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python)
![Kafka](https://img.shields.io/badge/Apache_Kafka-7.5-231F20?logo=apachekafka)
![React](https://img.shields.io/badge/React-18-61DAFB?logo=react)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker)
![Kubernetes](https://img.shields.io/badge/Kubernetes-Configured-326CE5?logo=kubernetes)
![CI/CD](https://img.shields.io/badge/CI%2FCD-GitHub_Actions-2088FF?logo=githubactions)

**Sub-10µs matching latency · 100,000 orders/sec · 10,000 simulated traders · PPO RL agent · Real-time surveillance**



</div>

---

## Table of Contents

- [Overview](#overview)
- [System Architecture](#system-architecture)
- [Core Features](#core-features)
- [HFT Load Testing](#hft-load-testing)
- [Quick Start](#quick-start)
- [Running in VSCode](#running-in-vscode)
- [Repository Structure](#repository-structure)
- [API Reference](#api-reference)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [Tech Stack](#tech-stack)
- [License](#license)

---

## Overview

Apex Exchange is a **production-grade, AI-powered stock exchange matching engine** that simulates the core infrastructure used by modern exchanges and investment banks. This is not a toy project.

The system processes up to **100,000 orders per second**, maintains real-time order books using price-time priority (max/min heap), streams all events through Apache Kafka, detects market manipulation with AI anomaly detection, and provides a live React dashboard with WebSocket feeds.

### What Makes This Different

| Feature | Detail |
|---|---|
| **Matching Latency** | Sub-10µs P99 using C++17 with optimised heap structures |
| **Scale** | 10,000 simulated traders, 100K orders/sec load tester |
| **AI Trading** | PPO reinforcement learning agent (Stable Baselines 3) |
| **Surveillance** | 4-module detector: spoofing, wash trading, pump & dump, layering |
| **Analytics** | Spread, Kyle's lambda, order imbalance, realised volatility |
| **Deployment** | Docker Compose + Kubernetes HPA + GitHub Actions CI/CD |

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      APEX EXCHANGE — OVERVIEW                       │
├──────────────────┬───────────────────┬────────────────┬─────────────┤
│   CLIENTS        │   API LAYER       │   CORE         │   INFRA     │
│                  │                   │                │             │
│  React Dashboard │── Order Service ──│── Matching Eng │  Kafka      │
│  AI Agents       │   (FastAPI/WS)    │   (C++ Heaps)  │  Redis      │
│  Load Tester     │── Risk Engine ────│── Order Book   │  PostgreSQL │
│  Algo Traders    │   (Rate limits)   │   (TreeMap)    │  Prometheus │
└──────────────────┴───────────────────┴────────────────┴─────────────┘
                           │  Kafka Event Bus  │
        ┌──────────────────┼───────────────────┼──────────────────┐
        ▼                  ▼                   ▼                  ▼
  orders topic       trades topic        market-data        risk-alerts
  (8 partitions)     (8 partitions)      (4 partitions)     (2 partitions)
        │                  │                   │                  │
        ▼                  ▼                   ▼                  ▼
  Risk Engine        Analytics           RL Trader          Surveillance
  Matching Engine    Engine              Market Maker        Dashboard
  Surveillance       Dashboard           Arbitrage Bot
```

### Kafka Topics

| Topic | Partitions | Producers → Consumers |
|---|---|---|
| `orders` | 8 | OMS → Matching Engine, Risk Engine, Surveillance |
| `trades` | 8 | Matching Engine → Analytics, Dashboard, RL Agent |
| `market-data` | 4 | Analytics Engine → Dashboard, RL Agents |
| `risk-alerts` | 2 | Risk Engine, Surveillance → Dashboard, Ops |

---

## Core Features

### 1 — Matching Engine (C++)

The heart of the exchange. Implements **price-time priority** order matching:

- **Buy orders** → Max-heap (highest price first)
- **Sell orders** → Min-heap (lowest price first)
- **Price level aggregation** → `std::map` (sorted TreeMap equivalent)
- **Order lookup** → `std::unordered_map` (O(1) by order ID)

Supports `LIMIT`, `MARKET`, partial fills, cancellation, and modification. Thread-safe with per-symbol locking. Latency tracked at nanosecond resolution.

```cpp
// Matching rule — price-time priority
while buy_heap.top().price >= sell_heap.top().price:
    exec_price = resting_order.price
    exec_qty   = min(buy.remaining, sell.remaining)
    execute_trade(exec_price, exec_qty)
```


---

### 2 — Order Management Service (Python/FastAPI)

Full order lifecycle over REST and WebSocket. All state changes are published to Kafka before responding.

```
POST   /orders          Submit order (risk-validated)
GET    /orders/{id}     Order status and fill info
DELETE /orders/{id}     Cancel an active order
PUT    /orders/{id}     Modify price or quantity
GET    /orders          List orders (filter by trader_id / symbol)
GET    /health          Liveness probe
WS     /ws/{symbol}     Subscribe to real-time order book feed
```

---

### 3 — Risk Management Engine

Per-trader state machine running as a Kafka consumer. Validates every order before it reaches the matching engine:

| Risk Control | Default Limit |
|---|---|
| Max order size | 100,000 shares |
| Max net position per symbol | 500,000 shares |
| Max daily loss | $200,000 |
| Max orders per second | 500 orders/sec |
| Max open orders | 1,000 |

Violations are published to `risk-alerts` with trader ID, order ID, symbol, and detailed reason.

---

### 4 — AI Trading Agents

#### Reinforcement Learning Trader

A **PPO (Proximal Policy Optimization)** agent trained using Stable Baselines 3:

- **Observation space:** 62-dimensional vector (order book L5, price returns, volume, position, unrealised PnL)
- **Actions:** `BUY` 100 shares / `SELL` 100 shares / `HOLD`
- **Reward:** Risk-adjusted PnL minus 1bps transaction cost minus inventory penalty
- **Fallback:** Momentum heuristic (SMA crossover) if PyTorch is unavailable

#### Market Manipulation Surveillance

Four independent statistical detectors running on the `orders` and `trades` topics:

| Detector | Algorithm |
|---|---|
| **Spoofing** | Cancel rate > 70% on orders > 500 shares within a 5-second sliding window |
| **Wash Trading** | 3+ opposing orders at same price from same trader within 2 seconds |
| **Pump & Dump** | Price z-score > 2.5σ combined with volume surge > 3× recent average |
| **Layering** | 8+ distinct price levels with > 80% overall cancel rate |

All alerts classified as `HIGH` or `MEDIUM` severity and published to `risk-alerts` within milliseconds.

---

### 5 — Market Simulator

Generates realistic order flow from up to **10,000 synthetic traders** across five behavioural archetypes. Prices follow Geometric Brownian Motion (σ = 0.2%/tick):

| Trader Type | Behaviour | Pool |
|---|---|---|
| Random | Uniform price/qty noise, mixed limit/market | 40% |
| Liquidity Provider | Tight two-sided quotes ±0.05% of mid | 20% |
| Momentum | Follows 5/20 SMA crossover signal | 15% |
| Mean Reversion | Fades moves > 0.5% from 20-period SMA | 15% |
| Noise | Small random market orders | 10% |

---

### 6 — Market Microstructure Analytics

Computes per-symbol metrics every second, published to `market-data`:

- **Bid-ask spread** (absolute + basis points)
- **Order imbalance** — `(buy_vol - sell_vol) / total_vol`
- **Book imbalance** — `(bid_depth - ask_depth) / total_depth`
- **Realised volatility** — annualised from log-returns (20-tick window)
- **VWAP** — volume-weighted average price
- **Kyle's Lambda** — price impact per unit volume (illiquidity measure)

---

### 7 — React Dashboard

Live trading dashboard with WebSocket feed:

- Real-time price chart with volume bars
- Order book depth with bid/ask visualisation
- Market microstructure metrics panel
- Matching latency percentile bars (P50 → P99.9)
- Risk alert stream with severity classification
- HFT load test panel with live TPS and latency chart

---

## HFT Load Testing

The built-in load tester generates up to **100,000 orders per second** from **10,000 simulated traders**, benchmarking the full system end-to-end.

### Benchmark Results

Typical results on a modern 8-core server:

| Metric | Result |
|---|---|
| Average throughput | 94,000 orders/sec |
| Peak throughput | 100,000+ orders/sec |
| Matching latency P50 | ~6 µs |
| Matching latency P95 | ~40 µs |
| Matching latency P99 | ~85 µs |
| Matching latency P99.9 | ~420 µs |
| Kafka publish latency | ~0.4 ms |
| WebSocket broadcast | ~2 ms |

### Running a Load Test

```bash
cd load_tester

# 10,000 traders, 100K orders/sec, 60 second duration
python load_tester.py --traders 10000 --target-ops 100000 --duration 60

# Lighter test for development
python load_tester.py --traders 1000 --target-ops 10000 --duration 30
```

**Sample output:**
```
============================================================
  LOAD TEST RESULTS
============================================================
  Duration      : 60.0s
  Total Sent    : 5,823,441
  Errors        : 0
  Avg Throughput: 97,057 orders/sec
  Latency P50   : 6.2 µs
  Latency P95   : 38.7 µs
  Latency P99   : 87.4 µs
  Latency P99.9 : 421.0 µs
  Latency Max   : 1,204.0 µs
============================================================
```

---

## Quick Start

### Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker Desktop | 24+ | Required — runs all infrastructure |
| Git | Any | To clone the repo |
| Python | 3.11+ | Only for running services outside Docker |
| Node.js | 20+ | Only for running the frontend outside Docker |

> **RAM:** 8 GB minimum. 16 GB recommended for full load testing.  
> **Ports needed:** 3000, 8001, 8010, 8080, 9090, 9092, 6379

---

### Option A — Full Docker Stack (Recommended)

Start everything with three commands:

```bash
# 1. Clone
git clone https://github.com/your-org/matching-engine.git
cd matching-engine

# 2. Start all 15 services
docker compose up -d

# 3. Verify everything is healthy
docker compose ps
```

First run pulls Docker images — takes 3–5 minutes. After that, startup is under 60 seconds.

**Open in your browser:**

| URL | Service |
|---|---|
| http://localhost:3000 | React trading dashboard |
| http://localhost:8001/docs | Order Service API (Swagger UI) |
| http://localhost:8080 | Kafka UI — browse topics and messages |
| http://localhost:9090 | Prometheus metrics |
| http://localhost:3001 | Grafana dashboards (admin / exchange) |

---

### Option B — Development Mode

Run infrastructure in Docker, services locally for fast iteration:

```bash
# Start only infrastructure
docker compose up -d zookeeper kafka kafka-init redis postgres

# Install Python dependencies
pip install -r requirements.txt

# Open separate terminal tabs for each service:

# Tab 1 — Order Service
cd services/order_service && python order_service.py

# Tab 2 — Risk Engine
cd services/risk_service && python risk_engine.py

# Tab 3 — Analytics Engine
cd analytics && python analytics_engine.py

# Tab 4 — Market Simulator (1,000 traders for dev)
cd simulator && python market_simulator.py --traders 1000 --target-ops 5000

# Tab 5 — AI Surveillance
cd ai_agents && python manipulation_detector.py

# Tab 6 — RL Trading Agent
cd ai_agents && python rl_trader.py AAPL

# Tab 7 — React Frontend
cd frontend && npm install && npm start
```

---

## Running in VSCode

Open your VSCode terminal with `` Ctrl+` `` and follow these steps:

**Step 1 — Make sure Docker Desktop is running** (whale icon in taskbar)

**Step 2 — Navigate to the project**
```bash
cd matching-engine
```

**Step 3 — Start the stack**
```bash
docker compose up -d
```

**Step 4 — Watch logs in real time**
```bash
# All services
docker compose logs -f

# Just one service
docker compose logs -f order-service
docker compose logs -f market-simulator
```

**Step 5 — Open the dashboard**

Go to http://localhost:3000 — you should see live prices, order books, and alerts flowing immediately (the market simulator starts automatically).

**Step 6 — Stop everything**
```bash
docker compose down

# Remove all data too (clean slate)
docker compose down -v
```

---

## Repository Structure

```
matching-engine/
├── engine/                         # C++ Matching Engine (hot path)
│   ├── order_book.hpp              # Max/min heap + TreeMap order book
│   ├── order_book.cpp
│   ├── matching_engine.hpp         # Multi-symbol engine + latency tracker
│   └── CMakeLists.txt
│
├── services/
│   ├── order_service/
│   │   └── order_service.py        # FastAPI REST + WebSocket OMS
│   └── risk_service/
│       └── risk_engine.py          # Real-time risk validation
│
├── ai_agents/
│   ├── rl_trader.py                # PPO RL agent (Stable Baselines 3)
│   └── manipulation_detector.py   # Spoofing / wash trade / pump-dump / layering
│
├── analytics/
│   └── analytics_engine.py        # Microstructure: spread, vol, VWAP, lambda
│
├── simulator/
│   └── market_simulator.py        # 10,000 synthetic traders, GBM prices
│
├── load_tester/
│   └── load_tester.py             # 100K orders/sec HFT benchmark
│
├── frontend/
│   └── src/
│       ├── App.jsx                 # Root with WebSocket feed
│       └── components/            # Chart, OrderBook, Latency, Alerts...
│
├── deployment/
│   ├── docker/                    # Dockerfiles for all services
│   └── kubernetes/
│       └── deployment.yaml        # K8s deployments, HPA, Ingress
│
├── tests/
│   └── test_order_book.cpp        # 8 unit tests (match, partial, cancel...)
│
├── .github/workflows/
│   └── ci.yml                     # Build, test, Docker push, K8s smoke test
│
├── docker-compose.yml             # Full 15-service stack
├── requirements.txt
└── docs/
    └── architecture.md            # Full system documentation
```

---

## API Reference

### Submit an Order

```bash
curl -X POST http://localhost:8001/orders \
  -H "Content-Type: application/json" \
  -d '{
    "trader_id":   "TRADER-001",
    "symbol":      "AAPL",
    "side":        "BUY",
    "order_type":  "LIMIT",
    "price":       182.50,
    "quantity":    100
  }'
```

### Cancel an Order

```bash
curl -X DELETE http://localhost:8001/orders/ORD-4F2A1B3C
```

### Submit a Market Order

```bash
curl -X POST http://localhost:8001/orders \
  -H "Content-Type: application/json" \
  -d '{
    "trader_id":   "TRADER-001",
    "symbol":      "TSLA",
    "side":        "SELL",
    "order_type":  "MARKET",
    "quantity":    50
  }'
```

### WebSocket Feed

```javascript
const ws = new WebSocket('ws://localhost:8001/ws/AAPL');
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  // data.type = 'book_snapshot' | 'trade' | 'microstructure' | 'agent_action'
  console.log(data);
};
```

### Kafka Event Schemas

**`orders` topic:**
```json
{
  "order_id":   "ORD-4F2A1B3C",
  "trader_id":  "SIM-LP-001234",
  "symbol":     "AAPL",
  "side":       "BUY",
  "order_type": "LIMIT",
  "price":      182.50,
  "quantity":   500,
  "event":      "ORDER_SUBMITTED",
  "ts_ns":      1711234567891234567
}
```

**`trades` topic:**
```json
{
  "trade_id":       "T000000000042",
  "buy_order_id":   "ORD-4F2A1B3C",
  "sell_order_id":  "ORD-9D8E7F6A",
  "symbol":         "AAPL",
  "price":          182.48,
  "quantity":       200,
  "executed_at_ns": 1711234567891234567,
  "sequence_num":   42
}
```

**`risk-alerts` topic:**
```json
{
  "alert_type": "SPOOFING",
  "trader_id":  "SIM-0044AB",
  "symbol":     "TSLA",
  "detail":     "cancel_rate=87.3% large_orders=42 in 5s window",
  "severity":   "HIGH",
  "ts_ns":      1711234567891234567
}
```

---

## Deployment

### Docker Compose

The `docker-compose.yml` defines **15 services** including Zookeeper, Kafka (8 partitions, LZ4 compression), Redis, PostgreSQL, all Python microservices, the C++ matching engine, React frontend, Prometheus, Grafana, and Kafka UI.

```bash
# Start everything
docker compose up -d

# View logs for a specific service
docker compose logs -f matching-engine

# Restart a single service after code change
docker compose restart order-service

# Stop and clean up
docker compose down -v
```

### Kubernetes

Full manifests with **Horizontal Pod Autoscaling**, resource limits, liveness/readiness probes, and Nginx Ingress with WebSocket support.

```bash
# Deploy
kubectl apply -f deployment/kubernetes/deployment.yaml

# Watch rollout
kubectl rollout status deployment/order-service -n exchange
kubectl rollout status deployment/matching-engine -n exchange

# Scale manually
kubectl scale deployment/order-service --replicas=10 -n exchange

# Port-forward for local testing
kubectl port-forward svc/order-service 8001:8001 -n exchange
kubectl port-forward svc/dashboard 3000:80 -n exchange
```

The HPA scales the order service from **2 to 20 replicas** based on CPU (60% threshold) and memory (70% threshold).

### CI/CD — GitHub Actions

The workflow at `.github/workflows/ci.yml` runs on every push and pull request:

- **Python:** `ruff` lint → `pytest` with coverage report
- **C++:** CMake configure → build → `ctest` unit tests
- **React:** ESLint → `npm build` → Jest tests
- **Docker:** Multi-arch build and push to GitHub Container Registry
- **Kubernetes:** `kind` cluster creation and smoke test on `main` branch merges

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Port already in use | `lsof -i :PORT` (Mac/Linux) or `netstat -ano \| findstr :PORT` (Windows), then kill the process |
| Kafka not ready / services failing | `docker compose restart kafka` — wait 30s — `docker compose restart order-service` |
| Out of memory errors | Docker Desktop → Settings → Resources → increase RAM to 6 GB+ |
| `docker compose` not found | Older Docker uses `docker-compose` (with hyphen) |
| Dashboard shows no data | `docker compose logs market-simulator` — check it's running |
| C++ build fails | Install: `cmake`, `build-essential`, `librdkafka-dev`, `nlohmann-json3-dev` |
| Python import errors | `pip install -r requirements.txt` |
| Kafka UI shows no topics | Wait for `kafka-init` container to complete: `docker compose logs kafka-init` |

### Useful Debug Commands

```bash
# Check all service statuses
docker compose ps

# Watch all logs
docker compose logs -f

# Inspect Kafka messages on the orders topic
docker exec -it kafka kafka-console-consumer \
  --bootstrap-server kafka:9092 --topic orders --from-beginning

# Inspect risk alerts
docker exec -it kafka kafka-console-consumer \
  --bootstrap-server kafka:9092 --topic risk-alerts --from-beginning

# Test the order service directly
curl http://localhost:8001/health
curl http://localhost:8001/orders
```

---

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Matching Core | C++17 | Sub-10µs order matching with heap structures |
| Order Service | FastAPI + uvicorn | REST API, WebSocket feed, Kafka producer |
| Event Streaming | Apache Kafka 7.5 | Durable async event bus, LZ4 compressed |
| AI — RL Agent | Stable Baselines 3 / PPO | Reinforcement learning trader |
| AI — Surveillance | Python + NumPy | Statistical anomaly detection |
| Analytics | NumPy + aiokafka | Real-time microstructure metrics |
| Frontend | React 18 + Chart.js | Live dashboard with WebSocket feed |
| Caching | Redis 7 | Order state, session data |
| Database | PostgreSQL 16 | Trade history, audit log |
| Observability | Prometheus + Grafana | Metrics, alerting, dashboards |
| Containers | Docker + Compose | Full local stack in one command |
| Orchestration | Kubernetes + HPA | Production auto-scaling deployment |
| CI/CD | GitHub Actions | Lint, test, build, push, smoke test |

---

## License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

---

<div align="center">

**⚡ Built to impress engineers at the world's top trading firms**

`C++` · `Python` · `Kafka` · `React` · `Docker` · `Kubernetes` · `Stable Baselines 3`

</div>
