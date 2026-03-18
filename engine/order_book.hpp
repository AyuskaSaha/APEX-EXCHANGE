#pragma once

#include <map>
#include <unordered_map>
#include <queue>
#include <vector>
#include <string>
#include <chrono>
#include <atomic>
#include <mutex>
#include <functional>
#include <optional>
#include <memory>

namespace exchange {

using OrderId    = std::string;
using Price      = double;
using Quantity   = uint64_t;
using Timestamp  = std::chrono::nanoseconds;

enum class OrderSide   { BUY, SELL };
enum class OrderType   { LIMIT, MARKET };
enum class OrderStatus { PENDING, PARTIAL, FILLED, CANCELLED };

struct Order {
    OrderId    id;
    std::string trader_id;
    std::string symbol;
    OrderSide  side;
    OrderType  type;
    Price      price;
    Quantity   quantity;
    Quantity   filled_qty{0};
    OrderStatus status{OrderStatus::PENDING};
    Timestamp  created_at;
    Timestamp  updated_at;

    Quantity remaining() const { return quantity - filled_qty; }
    bool     is_active()  const {
        return status == OrderStatus::PENDING || status == OrderStatus::PARTIAL;
    }
};

struct Trade {
    std::string trade_id;
    OrderId     buy_order_id;
    OrderId     sell_order_id;
    std::string symbol;
    Price       price;
    Quantity    quantity;
    Timestamp   executed_at;
    uint64_t    sequence_num;
};

struct PriceLevel {
    Price    price;
    Quantity total_qty{0};
    uint32_t order_count{0};
};

struct OrderBookSnapshot {
    std::string symbol;
    std::vector<PriceLevel> bids;  // Sorted descending
    std::vector<PriceLevel> asks;  // Sorted ascending
    Price    best_bid{0.0};
    Price    best_ask{0.0};
    Price    mid_price{0.0};
    Price    spread{0.0};
    Timestamp timestamp;
    uint64_t  sequence_num;
};

// ─── Comparators ──────────────────────────────────────────────────────────────
struct BuyComparator {
    // Max-heap: highest price first; ties broken by earlier time
    bool operator()(const std::shared_ptr<Order>& a,
                    const std::shared_ptr<Order>& b) const {
        if (a->price != b->price) return a->price < b->price;
        return a->created_at > b->created_at;
    }
};

struct SellComparator {
    // Min-heap: lowest price first; ties broken by earlier time
    bool operator()(const std::shared_ptr<Order>& a,
                    const std::shared_ptr<Order>& b) const {
        if (a->price != b->price) return a->price > b->price;
        return a->created_at > b->created_at;
    }
};

// ─── Order Book ───────────────────────────────────────────────────────────────
class OrderBook {
public:
    explicit OrderBook(std::string symbol);

    // Returns trades generated
    std::vector<Trade> add_order(std::shared_ptr<Order> order);

    bool cancel_order(const OrderId& id);
    bool modify_order(const OrderId& id, Price new_price, Quantity new_qty);

    OrderBookSnapshot snapshot(size_t depth = 10) const;
    std::optional<Price> best_bid() const;
    std::optional<Price> best_ask() const;

    uint64_t sequence() const { return sequence_num_.load(); }

    // Callbacks
    using TradeCallback = std::function<void(const Trade&)>;
    using BookCallback  = std::function<void(const OrderBookSnapshot&)>;

    void set_trade_callback(TradeCallback cb) { on_trade_ = std::move(cb); }
    void set_book_callback (BookCallback  cb) { on_book_  = std::move(cb); }

    // Metrics
    uint64_t total_orders()  const { return total_orders_.load();  }
    uint64_t total_trades()  const { return total_trades_.load();  }
    uint64_t total_volume()  const { return total_volume_.load();  }

private:
    std::vector<Trade> match(std::shared_ptr<Order> incoming);
    Trade              execute(std::shared_ptr<Order> buy,
                               std::shared_ptr<Order> sell,
                               Price exec_price, Quantity exec_qty);
    void               remove_top_if_filled(bool is_buy);
    void               update_price_level(Price price, Quantity delta_qty,
                                          int delta_count, bool is_buy);

    std::string symbol_;

    using BuyHeap  = std::priority_queue<std::shared_ptr<Order>,
                        std::vector<std::shared_ptr<Order>>, BuyComparator>;
    using SellHeap = std::priority_queue<std::shared_ptr<Order>,
                        std::vector<std::shared_ptr<Order>>, SellComparator>;

    BuyHeap  buy_heap_;
    SellHeap sell_heap_;

    // Price-level aggregation (TreeMap equivalent)
    std::map<Price, PriceLevel, std::greater<Price>> bid_levels_;  // desc
    std::map<Price, PriceLevel>                      ask_levels_;  // asc

    // O(1) lookup by ID
    std::unordered_map<OrderId, std::shared_ptr<Order>> order_map_;

    mutable std::mutex mtx_;

    std::atomic<uint64_t> sequence_num_{0};
    std::atomic<uint64_t> total_orders_{0};
    std::atomic<uint64_t> total_trades_{0};
    std::atomic<uint64_t> total_volume_{0};

    TradeCallback on_trade_;
    BookCallback  on_book_;
};

} // namespace exchange
