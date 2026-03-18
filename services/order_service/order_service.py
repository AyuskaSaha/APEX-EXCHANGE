"""
Order Management Service (OMS)
REST + WebSocket API — FastAPI
Publishes to Kafka, exposes order lifecycle endpoints.
"""

from __future__ import annotations
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import AsyncGenerator, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from aiokafka import AIOKafkaProducer
import uvicorn

# ─── Domain Models ────────────────────────────────────────────────────────────

class OrderSide(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"

class OrderType(str, Enum):
    LIMIT  = "LIMIT"
    MARKET = "MARKET"

class OrderStatus(str, Enum):
    PENDING   = "PENDING"
    PARTIAL   = "PARTIAL"
    FILLED    = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED  = "REJECTED"

class OrderRequest(BaseModel):
    trader_id : str
    symbol    : str = Field(..., min_length=1, max_length=10)
    side      : OrderSide
    order_type: OrderType
    price     : Optional[float] = None   # required for LIMIT
    quantity  : int = Field(..., gt=0, le=1_000_000)

    @validator("price", always=True)
    def price_required_for_limit(cls, v, values):
        if values.get("order_type") == OrderType.LIMIT and (v is None or v <= 0):
            raise ValueError("LIMIT orders require a positive price")
        return v

class OrderResponse(BaseModel):
    order_id  : str
    trader_id : str
    symbol    : str
    side      : OrderSide
    order_type: OrderType
    price     : Optional[float]
    quantity  : int
    filled_qty: int = 0
    status    : OrderStatus
    created_at: str
    message   : str = "Order accepted"

class ModifyRequest(BaseModel):
    price    : Optional[float]
    quantity : Optional[int] = Field(None, gt=0)

# ─── Risk Validator ───────────────────────────────────────────────────────────

class RiskConfig(BaseModel):
    max_order_size   : int   = 100_000
    max_position     : int   = 1_000_000
    max_daily_loss   : float = 500_000.0
    min_price        : float = 0.01
    max_price        : float = 1_000_000.0

RISK_CONFIG = RiskConfig()

def validate_risk(req: OrderRequest) -> Optional[str]:
    if req.quantity > RISK_CONFIG.max_order_size:
        return f"Order size {req.quantity} exceeds max {RISK_CONFIG.max_order_size}"
    if req.order_type == OrderType.LIMIT:
        if req.price < RISK_CONFIG.min_price:
            return f"Price {req.price} below minimum"
        if req.price > RISK_CONFIG.max_price:
            return f"Price {req.price} above maximum"
    return None

# ─── In-Memory Store (production: replace with Redis/DB) ──────────────────────

orders_store: Dict[str, OrderResponse] = {}

# ─── Kafka Producer ───────────────────────────────────────────────────────────

KAFKA_BOOTSTRAP = "kafka:9092"
kafka_producer: Optional[AIOKafkaProducer] = None

async def get_producer() -> AIOKafkaProducer:
    global kafka_producer
    if kafka_producer is None:
        kafka_producer = AIOKafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP,
            value_serializer=lambda v: json.dumps(v).encode(),
            compression_type="lz4",
            acks="all",
            enable_idempotence=True,
        )
        await kafka_producer.start()
    return kafka_producer

async def publish(topic: str, key: str, payload: dict):
    try:
        producer = await get_producer()
        await producer.send_and_wait(
            topic,
            value=payload,
            key=key.encode(),
        )
    except Exception as exc:
        print(f"[Kafka] publish error topic={topic}: {exc}")

# ─── WebSocket Manager ────────────────────────────────────────────────────────

class WSManager:
    def __init__(self):
        self._clients: Dict[str, List[WebSocket]] = {}  # symbol → connections

    async def connect(self, ws: WebSocket, symbol: str):
        await ws.accept()
        self._clients.setdefault(symbol, []).append(ws)

    def disconnect(self, ws: WebSocket, symbol: str):
        if symbol in self._clients:
            self._clients[symbol] = [c for c in self._clients[symbol] if c != ws]

    async def broadcast(self, symbol: str, data: dict):
        dead = []
        for ws in self._clients.get(symbol, []):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, symbol)

ws_manager = WSManager()

# ─── Application ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="Exchange Order Management Service",
    version="1.0.0",
    description="Production-grade OMS for AI-Powered Matching Engine",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown():
    global kafka_producer
    if kafka_producer:
        await kafka_producer.stop()

# ─── REST Endpoints ───────────────────────────────────────────────────────────

@app.post("/orders", response_model=OrderResponse, status_code=201)
async def submit_order(req: OrderRequest):
    # Risk check
    risk_err = validate_risk(req)
    if risk_err:
        raise HTTPException(status_code=400, detail=risk_err)

    order_id = f"ORD-{uuid.uuid4().hex[:12].upper()}"
    now_iso  = datetime.now(timezone.utc).isoformat()

    resp = OrderResponse(
        order_id   = order_id,
        trader_id  = req.trader_id,
        symbol     = req.symbol,
        side       = req.side,
        order_type = req.order_type,
        price      = req.price,
        quantity   = req.quantity,
        status     = OrderStatus.PENDING,
        created_at = now_iso,
    )
    orders_store[order_id] = resp

    payload = resp.dict()
    payload["event"] = "ORDER_SUBMITTED"
    payload["ts_ns"] = time.time_ns()

    await publish("orders", order_id, payload)
    await ws_manager.broadcast(req.symbol, payload)

    return resp


@app.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(order_id: str):
    order = orders_store.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.delete("/orders/{order_id}")
async def cancel_order(order_id: str):
    order = orders_store.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
        raise HTTPException(status_code=409, detail=f"Cannot cancel {order.status} order")

    order.status = OrderStatus.CANCELLED
    payload = {"event": "ORDER_CANCELLED", "order_id": order_id,
               "ts_ns": time.time_ns()}
    await publish("orders", order_id, payload)
    await ws_manager.broadcast(order.symbol, payload)
    return {"message": "Order cancelled", "order_id": order_id}


@app.put("/orders/{order_id}", response_model=OrderResponse)
async def modify_order(order_id: str, mod: ModifyRequest):
    order = orders_store.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.PENDING:
        raise HTTPException(status_code=409, detail="Can only modify PENDING orders")

    if mod.price is not None:
        order.price = mod.price
    if mod.quantity is not None:
        order.quantity = mod.quantity

    payload = {"event": "ORDER_MODIFIED", "order_id": order_id,
               "price": order.price, "quantity": order.quantity,
               "ts_ns": time.time_ns()}
    await publish("orders", order_id, payload)
    return order


@app.get("/orders", response_model=List[OrderResponse])
async def list_orders(trader_id: Optional[str] = None, symbol: Optional[str] = None):
    result = list(orders_store.values())
    if trader_id:
        result = [o for o in result if o.trader_id == trader_id]
    if symbol:
        result = [o for o in result if o.symbol == symbol]
    return result[-500:]  # last 500


@app.get("/health")
async def health():
    return {"status": "ok", "service": "order-management", "ts": time.time()}

# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/{symbol}")
async def ws_feed(websocket: WebSocket, symbol: str):
    await ws_manager.connect(websocket, symbol)
    try:
        while True:
            await websocket.receive_text()  # keep-alive
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, symbol)


if __name__ == "__main__":
    uvicorn.run("order_service:app", host="0.0.0.0", port=8001,
                workers=4, loop="uvloop")
