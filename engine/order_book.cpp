#include "order_book.hpp"
#include <stdexcept>
#include <sstream>
#include <iomanip>

namespace exchange {

static std::string gen_trade_id(uint64_t seq) {
    std::ostringstream oss;
    oss << "T" << std::setw(12) << std::setfill('0') << seq;
    return oss.str();
}

OrderBook::OrderBook(std::string symbol) : symbol_(std::move(symbol)) {}

// ─── Public: add_order ────────────────────────────────────────────────────────
std::vector<Trade> OrderBook::add_order(std::shared_ptr<Order> order) {
    std::lock_guard<std::mutex> lock(mtx_);

    order->created_at = std::chrono::high_resolution_clock::now()
                            .time_since_epoch();
    order->updated_at = order->created_at;
    ++total_orders_;

    // Market orders get a sentinel price so they always match
    if (order->type == OrderType::MARKET) {
        order->price = (order->side == OrderSide::BUY)
                       ? std::numeric_limits<Price>::max()
                       : 0.0;
    }

    auto trades = match(order);

    // If there's remaining quantity, rest it in the book
    if (order->is_active() && order->type != OrderType::MARKET) {
        order_map_[order->id] = order;
        update_price_level(order->price, order->remaining(), +1,
                           order->side == OrderSide::BUY);
        if (order->side == OrderSide::BUY)
            buy_heap_.push(order);
        else
            sell_heap_.push(order);
    }

    if (!trades.empty() && on_book_) {
        on_book_(snapshot(10));
    }

    return trades;
}

// ─── Private: match ───────────────────────────────────────────────────────────
std::vector<Trade> OrderBook::match(std::shared_ptr<Order> incoming) {
    std::vector<Trade> trades;

    auto& resting_heap = (incoming->side == OrderSide::BUY)
                         ? static_cast<void*>(&sell_heap_)
                         : static_cast<void*>(&buy_heap_);
    (void)resting_heap; // We use the actual heaps below

    while (incoming->remaining() > 0) {
        if (incoming->side == OrderSide::BUY) {
            if (sell_heap_.empty()) break;
            auto& top = sell_heap_.top();
            if (!top->is_active()) { sell_heap_.pop(); continue; }
            if (incoming->price < top->price) break;  // No match

            Price    exec_price = top->price;
            Quantity exec_qty   = std::min(incoming->remaining(),
                                           top->remaining());
            auto t = execute(incoming, const_cast<std::shared_ptr<Order>&>(
                             const_cast<const SellHeap&>(sell_heap_).top()),
                             exec_price, exec_qty);
            trades.push_back(t);

            update_price_level(top->price, -static_cast<Quantity>(exec_qty),
                               top->is_active() ? 0 : -1, false);
            if (!top->is_active()) sell_heap_.pop();

        } else { // SELL
            if (buy_heap_.empty()) break;
            auto& top = buy_heap_.top();
            if (!top->is_active()) { buy_heap_.pop(); continue; }
            if (incoming->price > top->price) break;

            Price    exec_price = top->price;
            Quantity exec_qty   = std::min(incoming->remaining(),
                                           top->remaining());
            auto t = execute(const_cast<std::shared_ptr<Order>&>(
                             const_cast<const BuyHeap&>(buy_heap_).top()),
                             incoming, exec_price, exec_qty);
            trades.push_back(t);

            update_price_level(top->price, -static_cast<Quantity>(exec_qty),
                               top->is_active() ? 0 : -1, true);
            if (!top->is_active()) buy_heap_.pop();
        }
    }

    return trades;
}

Trade OrderBook::execute(std::shared_ptr<Order> buy,
                         std::shared_ptr<Order> sell,
                         Price exec_price, Quantity exec_qty) {
    buy->filled_qty  += exec_qty;
    sell->filled_qty += exec_qty;

    buy->status  = (buy->remaining()  == 0) ? OrderStatus::FILLED
                                             : OrderStatus::PARTIAL;
    sell->status = (sell->remaining() == 0) ? OrderStatus::FILLED
                                             : OrderStatus::PARTIAL;

    auto now = std::chrono::high_resolution_clock::now().time_since_epoch();
    buy->updated_at = sell->updated_at = now;

    ++total_trades_;
    total_volume_ += exec_qty;

    Trade t;
    t.trade_id     = gen_trade_id(sequence_num_.fetch_add(1));
    t.buy_order_id  = buy->id;
    t.sell_order_id = sell->id;
    t.symbol        = symbol_;
    t.price         = exec_price;
    t.quantity      = exec_qty;
    t.executed_at   = now;
    t.sequence_num  = sequence_num_.load();

    if (on_trade_) on_trade_(t);
    return t;
}

// ─── Public: cancel_order ────────────────────────────────────────────────────
bool OrderBook::cancel_order(const OrderId& id) {
    std::lock_guard<std::mutex> lock(mtx_);
    auto it = order_map_.find(id);
    if (it == order_map_.end()) return false;

    auto& order = it->second;
    if (!order->is_active()) return false;

    update_price_level(order->price, -order->remaining(), -1,
                       order->side == OrderSide::BUY);
    order->status = OrderStatus::CANCELLED;
    order_map_.erase(it);
    return true;
}

// ─── Public: modify_order ────────────────────────────────────────────────────
bool OrderBook::modify_order(const OrderId& id, Price new_price,
                              Quantity new_qty) {
    // Cancel + re-insert (loses time priority — acceptable for simulation)
    if (!cancel_order(id)) return false;
    auto it = order_map_.find(id);
    // Note: cancel_order erases, so we can't find it. We rely on caller to
    // reconstruct. Return true indicates the old order was removed.
    (void)it; (void)new_price; (void)new_qty;
    return true;
}

// ─── Public: snapshot ────────────────────────────────────────────────────────
OrderBookSnapshot OrderBook::snapshot(size_t depth) const {
    std::lock_guard<std::mutex> lock(mtx_);
    OrderBookSnapshot snap;
    snap.symbol       = symbol_;
    snap.sequence_num = sequence_num_.load();
    snap.timestamp    = std::chrono::high_resolution_clock::now()
                            .time_since_epoch();

    size_t cnt = 0;
    for (auto& [price, lvl] : bid_levels_) {
        if (lvl.total_qty == 0) continue;
        snap.bids.push_back(lvl);
        if (++cnt >= depth) break;
    }
    cnt = 0;
    for (auto& [price, lvl] : ask_levels_) {
        if (lvl.total_qty == 0) continue;
        snap.asks.push_back(lvl);
        if (++cnt >= depth) break;
    }

    if (!snap.bids.empty()) snap.best_bid = snap.bids.front().price;
    if (!snap.asks.empty()) snap.best_ask = snap.asks.front().price;
    if (snap.best_bid > 0 && snap.best_ask > 0) {
        snap.mid_price = (snap.best_bid + snap.best_ask) / 2.0;
        snap.spread    = snap.best_ask - snap.best_bid;
    }
    return snap;
}

std::optional<Price> OrderBook::best_bid() const {
    std::lock_guard<std::mutex> lock(mtx_);
    for (auto& [price, lvl] : bid_levels_)
        if (lvl.total_qty > 0) return price;
    return std::nullopt;
}

std::optional<Price> OrderBook::best_ask() const {
    std::lock_guard<std::mutex> lock(mtx_);
    for (auto& [price, lvl] : ask_levels_)
        if (lvl.total_qty > 0) return price;
    return std::nullopt;
}

// ─── Private helpers ─────────────────────────────────────────────────────────
void OrderBook::update_price_level(Price price, Quantity delta_qty,
                                   int delta_count, bool is_buy) {
    auto& levels = is_buy ? reinterpret_cast<std::map<Price,PriceLevel>&>(
                                bid_levels_)
                          : reinterpret_cast<std::map<Price,PriceLevel>&>(
                                ask_levels_);

    if (is_buy) {
        auto& lvl = bid_levels_[price];
        lvl.price = price;
        lvl.total_qty    = static_cast<Quantity>(
                               static_cast<int64_t>(lvl.total_qty) +
                               static_cast<int64_t>(delta_qty));
        lvl.order_count  = static_cast<uint32_t>(
                               static_cast<int32_t>(lvl.order_count) +
                               delta_count);
        if (lvl.total_qty == 0) bid_levels_.erase(price);
    } else {
        auto& lvl = ask_levels_[price];
        lvl.price = price;
        lvl.total_qty    = static_cast<Quantity>(
                               static_cast<int64_t>(lvl.total_qty) +
                               static_cast<int64_t>(delta_qty));
        lvl.order_count  = static_cast<uint32_t>(
                               static_cast<int32_t>(lvl.order_count) +
                               delta_count);
        if (lvl.total_qty == 0) ask_levels_.erase(price);
    }
    (void)levels;
}

} // namespace exchange
