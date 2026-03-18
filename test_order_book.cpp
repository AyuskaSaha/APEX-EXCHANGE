/**
 * Order Book Unit Tests
 * Tests: matching, partial fills, cancellation, price-time priority,
 *        market orders, spread calculation, order book depth.
 */

#include "order_book.hpp"
#include <cassert>
#include <iostream>
#include <chrono>

using namespace exchange;

static int pass_count = 0;
static int fail_count = 0;

#define ASSERT(cond, msg) \
    if(!(cond)){ std::cerr << "FAIL: " << msg << "\n"; fail_count++; } \
    else { std::cout << "PASS: " << msg << "\n"; pass_count++; }

static std::shared_ptr<Order> make_order(
    const std::string& id,
    OrderSide side,
    OrderType type,
    double price,
    uint64_t qty,
    const std::string& sym = "AAPL")
{
    auto o = std::make_shared<Order>();
    o->id        = id;
    o->trader_id = "T1";
    o->symbol    = sym;
    o->side      = side;
    o->type      = type;
    o->price     = price;
    o->quantity  = qty;
    return o;
}

// ── Test 1: Basic limit order match ──────────────────────────────────────────
void test_basic_match() {
    OrderBook book("AAPL");
    auto sell = make_order("S1", OrderSide::SELL, OrderType::LIMIT, 100.0, 100);
    auto buy  = make_order("B1", OrderSide::BUY,  OrderType::LIMIT, 101.0, 100);

    book.add_order(sell);
    auto trades = book.add_order(buy);

    ASSERT(trades.size() == 1, "basic match generates 1 trade");
    ASSERT(trades[0].price == 100.0, "trade at resting price (sell)");
    ASSERT(trades[0].quantity == 100, "full fill quantity");
    ASSERT(buy->status == OrderStatus::FILLED, "buy order filled");
    ASSERT(sell->status == OrderStatus::FILLED, "sell order filled");
}

// ── Test 2: Partial fill ──────────────────────────────────────────────────────
void test_partial_fill() {
    OrderBook book("AAPL");
    auto sell = make_order("S2", OrderSide::SELL, OrderType::LIMIT, 100.0, 50);
    auto buy  = make_order("B2", OrderSide::BUY,  OrderType::LIMIT, 100.0, 100);

    book.add_order(sell);
    auto trades = book.add_order(buy);

    ASSERT(trades.size() == 1, "partial: 1 trade");
    ASSERT(trades[0].quantity == 50, "partial fill qty=50");
    ASSERT(sell->status == OrderStatus::FILLED, "sell fully filled");
    ASSERT(buy->status == OrderStatus::PARTIAL, "buy partially filled");
    ASSERT(buy->filled_qty == 50, "buy filled_qty == 50");
    ASSERT(buy->remaining() == 50, "buy remaining == 50");
}

// ── Test 3: No match (price cross doesn't satisfy) ───────────────────────────
void test_no_match() {
    OrderBook book("AAPL");
    auto sell = make_order("S3", OrderSide::SELL, OrderType::LIMIT, 102.0, 100);
    auto buy  = make_order("B3", OrderSide::BUY,  OrderType::LIMIT,  99.0, 100);

    book.add_order(sell);
    auto trades = book.add_order(buy);

    ASSERT(trades.empty(), "no match when buy < sell");

    auto snap = book.snapshot();
    ASSERT(snap.bids.size() == 1, "buy rests in book");
    ASSERT(snap.asks.size() == 1, "sell rests in book");
}

// ── Test 4: Cancel order ─────────────────────────────────────────────────────
void test_cancel() {
    OrderBook book("AAPL");
    auto buy = make_order("B4", OrderSide::BUY, OrderType::LIMIT, 100.0, 200);
    book.add_order(buy);

    bool ok = book.cancel_order("B4");
    ASSERT(ok, "cancel returns true");
    ASSERT(buy->status == OrderStatus::CANCELLED, "order status is CANCELLED");

    auto snap = book.snapshot();
    ASSERT(snap.bids.empty(), "book empty after cancel");
}

// ── Test 5: Market order ─────────────────────────────────────────────────────
void test_market_order() {
    OrderBook book("AAPL");
    auto sell = make_order("S5", OrderSide::SELL, OrderType::LIMIT, 105.0, 300);
    book.add_order(sell);

    auto mkt = make_order("B5", OrderSide::BUY, OrderType::MARKET, 0.0, 100);
    auto trades = book.add_order(mkt);

    ASSERT(!trades.empty(), "market order matches");
    ASSERT(mkt->status == OrderStatus::FILLED, "market order filled");
    ASSERT(trades[0].price == 105.0, "market order fills at best ask");
}

// ── Test 6: Price-time priority ───────────────────────────────────────────────
void test_price_time_priority() {
    OrderBook book("AAPL");
    // Two sells at same price — earlier one should fill first
    auto sell1 = make_order("S6A", OrderSide::SELL, OrderType::LIMIT, 100.0, 50);
    auto sell2 = make_order("S6B", OrderSide::SELL, OrderType::LIMIT, 100.0, 50);
    book.add_order(sell1);
    // Small delay to ensure time ordering
    std::this_thread::sleep_for(std::chrono::microseconds(10));
    book.add_order(sell2);

    auto buy = make_order("B6", OrderSide::BUY, OrderType::LIMIT, 100.0, 50);
    auto trades = book.add_order(buy);

    ASSERT(trades.size() == 1, "price-time: 1 trade");
    ASSERT(trades[0].sell_order_id == "S6A", "earlier sell fills first");
    ASSERT(sell1->status == OrderStatus::FILLED, "S6A filled");
    ASSERT(sell2->status == OrderStatus::PENDING, "S6B still pending");
}

// ── Test 7: Multi-level sweep ─────────────────────────────────────────────────
void test_multi_level_sweep() {
    OrderBook book("AAPL");
    book.add_order(make_order("SA", OrderSide::SELL, OrderType::LIMIT, 100.0, 100));
    book.add_order(make_order("SB", OrderSide::SELL, OrderType::LIMIT, 101.0, 100));
    book.add_order(make_order("SC", OrderSide::SELL, OrderType::LIMIT, 102.0, 100));

    auto buy = make_order("BIG", OrderSide::BUY, OrderType::LIMIT, 105.0, 300);
    auto trades = book.add_order(buy);

    ASSERT(trades.size() == 3, "sweeps 3 levels");
    ASSERT(buy->status == OrderStatus::FILLED, "large buy fully filled");
    ASSERT(book.best_ask() == std::nullopt, "ask side empty after sweep");
}

// ── Test 8: Snapshot accuracy ─────────────────────────────────────────────────
void test_snapshot() {
    OrderBook book("AAPL");
    book.add_order(make_order("B1", OrderSide::BUY, OrderType::LIMIT, 99.0, 200));
    book.add_order(make_order("B2", OrderSide::BUY, OrderType::LIMIT, 98.0, 100));
    book.add_order(make_order("S1", OrderSide::SELL, OrderType::LIMIT, 101.0, 150));

    auto snap = book.snapshot(5);
    ASSERT(snap.best_bid == 99.0, "best bid correct");
    ASSERT(snap.best_ask == 101.0, "best ask correct");
    ASSERT(snap.spread == 2.0, "spread == 2.0");
    ASSERT(snap.bids.size() == 2, "2 bid levels");
    ASSERT(snap.asks.size() == 1, "1 ask level");
    ASSERT(snap.bids[0].total_qty == 200, "top bid qty correct");
}

// ── Main ──────────────────────────────────────────────────────────────────────
int main() {
    std::cout << "=== Order Book Unit Tests ===\n\n";
    test_basic_match();
    test_partial_fill();
    test_no_match();
    test_cancel();
    test_market_order();
    test_price_time_priority();
    test_multi_level_sweep();
    test_snapshot();

    std::cout << "\n=========================\n";
    std::cout << "PASSED: " << pass_count << "\n";
    std::cout << "FAILED: " << fail_count << "\n";
    return fail_count > 0 ? 1 : 0;
}
