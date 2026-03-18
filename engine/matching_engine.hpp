#pragma once
#include "order_book.hpp"
#include <unordered_map>
#include <deque>
#include <atomic>
#include <thread>
#include <chrono>
#include <numeric>
#include <algorithm>

namespace exchange {

// ─── Latency Tracker ─────────────────────────────────────────────────────────
class LatencyTracker {
public:
    void record(std::chrono::nanoseconds ns) {
        std::lock_guard<std::mutex> lock(mtx_);
        samples_.push_back(ns.count());
        if (samples_.size() > MAX_SAMPLES) samples_.pop_front();
        ++total_count_;
        total_ns_ += static_cast<uint64_t>(ns.count());
    }

    struct Stats {
        double mean_us;
        double p50_us;
        double p95_us;
        double p99_us;
        double p999_us;
        double max_us;
        uint64_t count;
        double throughput_per_sec;
    };

    Stats compute() const {
        std::lock_guard<std::mutex> lock(mtx_);
        Stats s{};
        if (samples_.empty()) return s;
        std::vector<int64_t> v(samples_.begin(), samples_.end());
        std::sort(v.begin(), v.end());
        auto ns2us = [](int64_t ns) { return ns / 1000.0; };
        s.mean_us = ns2us(total_ns_ / total_count_);
        s.p50_us  = ns2us(v[v.size() * 50 / 100]);
        s.p95_us  = ns2us(v[v.size() * 95 / 100]);
        s.p99_us  = ns2us(v[v.size() * 99 / 100]);
        s.p999_us = ns2us(v[std::min(v.size()-1, v.size()*999/1000)]);
        s.max_us  = ns2us(v.back());
        s.count   = total_count_;
        return s;
    }

private:
    static constexpr size_t MAX_SAMPLES = 100'000;
    mutable std::mutex   mtx_;
    std::deque<int64_t>  samples_;
    std::atomic<uint64_t> total_count_{0};
    std::atomic<uint64_t> total_ns_{0};
};

// ─── Matching Engine ─────────────────────────────────────────────────────────
class MatchingEngine {
public:
    explicit MatchingEngine(std::vector<std::string> symbols) {
        for (auto& sym : symbols)
            books_[sym] = std::make_unique<OrderBook>(sym);
    }

    struct SubmitResult {
        bool ok;
        std::string error;
        std::vector<Trade> trades;
        std::chrono::nanoseconds latency;
    };

    SubmitResult submit(std::shared_ptr<Order> order) {
        auto t0 = std::chrono::high_resolution_clock::now();

        SubmitResult res;
        auto it = books_.find(order->symbol);
        if (it == books_.end()) {
            res.ok = false;
            res.error = "Unknown symbol: " + order->symbol;
            return res;
        }

        res.trades = it->second->add_order(order);
        res.ok = true;

        auto t1 = std::chrono::high_resolution_clock::now();
        res.latency = std::chrono::duration_cast<std::chrono::nanoseconds>(
                          t1 - t0);
        latency_.record(res.latency);
        ++orders_submitted_;
        return res;
    }

    bool cancel(const std::string& symbol, const OrderId& id) {
        auto it = books_.find(symbol);
        if (it == books_.end()) return false;
        return it->second->cancel_order(id);
    }

    OrderBookSnapshot book_snapshot(const std::string& symbol,
                                    size_t depth = 10) const {
        auto it = books_.find(symbol);
        if (it == books_.end()) return {};
        return it->second->snapshot(depth);
    }

    LatencyTracker::Stats latency_stats() const { return latency_.compute(); }
    uint64_t orders_submitted() const { return orders_submitted_.load(); }

    void set_trade_callback(const std::string& sym, OrderBook::TradeCallback cb) {
        if (auto it = books_.find(sym); it != books_.end())
            it->second->set_trade_callback(std::move(cb));
    }
    void set_book_callback(const std::string& sym, OrderBook::BookCallback cb) {
        if (auto it = books_.find(sym); it != books_.end())
            it->second->set_book_callback(std::move(cb));
    }

private:
    std::unordered_map<std::string, std::unique_ptr<OrderBook>> books_;
    LatencyTracker latency_;
    std::atomic<uint64_t> orders_submitted_{0};
};

} // namespace exchange
