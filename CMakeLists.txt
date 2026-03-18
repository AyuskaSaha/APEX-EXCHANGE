cmake_minimum_required(VERSION 3.20)
project(matching_engine VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ── Release flags ─────────────────────────────────────────────────────────────
add_compile_options(
  -O3 -march=native -mtune=native
  -Wall -Wextra -Wpedantic
  -fno-exceptions   # disable for hot paths
)

# ── Dependencies ──────────────────────────────────────────────────────────────
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(RDKAFKA REQUIRED rdkafka++)
find_package(nlohmann_json 3.10 QUIET)

include(FetchContent)
if(NOT nlohmann_json_FOUND)
  FetchContent_Declare(json URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz)
  FetchContent_MakeAvailable(json)
endif()

# ── Main server executable ────────────────────────────────────────────────────
add_executable(matching_engine_server
  engine/order_book.cpp
  engine/matching_algorithm.cpp
  engine/server_main.cpp
)

target_include_directories(matching_engine_server PRIVATE
  ${CMAKE_SOURCE_DIR}/engine
  ${RDKAFKA_INCLUDE_DIRS}
)

target_link_libraries(matching_engine_server PRIVATE
  Threads::Threads
  ${RDKAFKA_LIBRARIES}
  nlohmann_json::nlohmann_json
)

# ── Tests ─────────────────────────────────────────────────────────────────────
enable_testing()

add_executable(test_order_book
  tests/test_order_book.cpp
  engine/order_book.cpp
)
target_include_directories(test_order_book PRIVATE ${CMAKE_SOURCE_DIR}/engine)
target_link_libraries(test_order_book PRIVATE Threads::Threads)

add_test(NAME OrderBookTests COMMAND test_order_book)
