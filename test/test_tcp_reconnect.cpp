// Hardware-free GeGi TCP reconnect regression test.
// Creates a short-lived loopback fake event stream, drops the connection,
// and checks that the same driver can receive a second event after reconnect.
// NEVER sends detector-control commands.
#include <socket_comms/tcp_event_reader.hpp>
#include <boost/asio.hpp>
#include <array>
#include <atomic>
#include <chrono>
#include <cstring>
#include <exception>
#include <iostream>
#include <stdexcept>
#include <thread>

using namespace phds_gegi_driver::socket_comms;
using tcp = boost::asio::ip::tcp;

static std::array<char, 44> makeEvent() {
    std::array<char, 44> bytes{};
    auto set = [&](std::size_t pos, const auto &value) {
        std::memcpy(bytes.data() + pos, &value, sizeof(value));
    };
    const std::uint32_t type = 0;
    const std::uint64_t time = 10;
    const double x = 1.0, y = 2.0, z = 3.0, energy = 661.7;
    set(0, type); set(4, time); set(12, x); set(20, y);
    set(28, z); set(36, energy);
    return bytes;
}

int main() {
    boost::asio::io_context io;
    tcp::acceptor acceptor(io, tcp::endpoint(boost::asio::ip::address_v4::loopback(), 0));
    const unsigned short port = acceptor.local_endpoint().port();
    std::atomic<int> callbacks{0};
    std::atomic<int> accepts{0};
    std::thread fake([&] {
        try {
            for (int round = 0; round < 2; ++round) {
                tcp::socket socket(io);
                acceptor.accept(socket);
                accepts++;
                const auto bytes = makeEvent();
                boost::asio::write(socket, boost::asio::buffer(bytes));
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
                boost::system::error_code ignored;
                socket.shutdown(tcp::socket::shutdown_both, ignored);
                socket.close(ignored);
            }
        } catch (const std::exception &e) {
            std::cerr << "Fake stream failed: " << e.what() << "\n";
        }
    });
    bool passed = false;
    {
        TcpEventReader reader([&](const phds_gegi_driver::compton_events::Event1Site &) {
            callbacks++;
        }, [&](const phds_gegi_driver::compton_events::Event2Site &) { throw std::runtime_error("unexpected 2-site event"); });
        reader.connect("127.0.0.1", std::to_string(port));
        reader.startListening();
        auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(8);
        while (std::chrono::steady_clock::now() < deadline) {
            if (callbacks.load() >= 2 && reader.reconnectCount() >= 1) {
                passed = true;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(25));
        }
        reader.stopListening();
        std::cout << "callbacks=" << callbacks.load()
                  << " events_total=" << reader.eventsTotal()
                  << " reconnect_count=" << reader.reconnectCount()
                  << " accepts=" << accepts.load() << "\n";
        if (reader.eventsTotal() != static_cast<unsigned>(callbacks.load())) passed = false;
    }
    acceptor.close();
    fake.join();
    if (!passed) {
        std::cerr << "FAILED: expected 2 parsed event frames across reconnection\n";
        return 1;
    }
    std::cout << "PASS: GeGi TCP reconnect after simulated connection drop\n";
    return 0;
}
