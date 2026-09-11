//
// Created by createc on 09/09/2020.
//

#include <socket_comms/tcp_event_reader.hpp>

#include <cmath>
#include <chrono>
#include <cctype>
#include <iomanip>
#include <cstdint>
#include <cstring>
#include <sstream>
#include <limits>
#include <utility>
#include <vector>

namespace phds_gegi_driver::socket_comms {

    namespace {
        bool readExactlyWithTimeout(boost::asio::ip::tcp::socket &socket,
                                    char *destination,
                                    size_t bytes_to_read,
                                    int timeout_ms) {
            size_t total_read = 0;
            const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);

            while (total_read < bytes_to_read) {
                boost::system::error_code error;
                const size_t available = socket.available(error);
                if (error) {
                    std::cerr << "Socket available() failed: " << error.message() << std::endl;
                    return false;
                }

                if (available > 0) {
                    const size_t chunk_size = std::min(available, bytes_to_read - total_read);
                    const size_t bytes_read = socket.read_some(
                            boost::asio::buffer(destination + total_read, chunk_size), error);
                    if (error) {
                        std::cerr << "Socket read_some() failed: " << error.message() << std::endl;
                        return false;
                    }
                    total_read += bytes_read;
                    continue;
                }

                if (std::chrono::steady_clock::now() >= deadline) {
                    return false;
                }

                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }

            return true;
        }

        uint32_t byteSwap32(uint32_t value) {
            return ((value & 0x000000FFu) << 24u) |
                   ((value & 0x0000FF00u) << 8u) |
                   ((value & 0x00FF0000u) >> 8u) |
                   ((value & 0xFF000000u) >> 24u);
        }

        uint64_t byteSwap64(uint64_t value) {
            return ((value & 0x00000000000000FFull) << 56u) |
                   ((value & 0x000000000000FF00ull) << 40u) |
                   ((value & 0x0000000000FF0000ull) << 24u) |
                   ((value & 0x00000000FF000000ull) << 8u) |
                   ((value & 0x000000FF00000000ull) >> 8u) |
                   ((value & 0x0000FF0000000000ull) >> 24u) |
                   ((value & 0x00FF000000000000ull) >> 40u) |
                   ((value & 0xFF00000000000000ull) >> 56u);
        }

        uint32_t readU32(const char *data, bool big_endian) {
            uint32_t value = 0;
            std::memcpy(&value, data, sizeof(uint32_t));
            return big_endian ? byteSwap32(value) : value;
        }

        int32_t readI32(const char *data, bool big_endian) {
            return static_cast<int32_t>(readU32(data, big_endian));
        }

        double readF64(const char *data, bool big_endian) {
            uint64_t value = 0;
            std::memcpy(&value, data, sizeof(uint64_t));
            if (big_endian) {
                value = byteSwap64(value);
            }
            double output = 0.0;
            std::memcpy(&output, &value, sizeof(double));
            return output;
        }

        void flushSocketInputBuffer(boost::asio::ip::tcp::socket &socket, size_t max_bytes = 1 << 20) {
            size_t drained_total = 0;
            for (;;) {
                boost::system::error_code error;
                const size_t available = socket.available(error);
                if (error) {
                    std::cerr << "Socket available() failed during flush: "
                              << error.message() << std::endl;
                    return;
                }
                if (available == 0) {
                    break;
                }

                const size_t to_read = std::min<size_t>(available, 4096);
                std::vector<char> scratch(to_read);
                const size_t n = socket.read_some(boost::asio::buffer(scratch), error);
                if (error) {
                    std::cerr << "Socket read_some() failed during flush: "
                              << error.message() << std::endl;
                    return;
                }

                drained_total += n;
                if (drained_total >= max_bytes) {
                    std::cerr << "Socket flush capped at " << drained_total << " bytes" << std::endl;
                    break;
                }
            }

            if (drained_total > 0) {
                std::cout << "Flushed " << drained_total
                          << " stale bytes before command response read" << std::endl;
            }
        }
    }

    TcpEventReader::TcpEventReader(Callback1Site callback_1_site, Callback2Site callback_2_site)
            : socket_(io_service_),
                            command_socket_(io_service_),
              callback_1_site_(std::move(callback_1_site)),
              callback_2_site_(std::move(callback_2_site)) {}

    void TcpEventReader::connect(const std::string &ip, const std::string &port) {
        using namespace boost::asio::ip;

        std::cout << "Connecting to GeGI at " + ip + ":" + port << "..." << std::endl;

        tcp::resolver resolver(io_service_);
        tcp::resolver::query query(ip, port);
        tcp::resolver::iterator endpoint_iterator = resolver.resolve(query);

        try {
            boost::asio::connect(socket_, endpoint_iterator);

        } catch (const std::exception &ex) {
            throw;
        }

        // NOTE: Do NOT open a second TCP connection for commands.
        //
        // The GeGI streams events back on the SAME connection that issued the
        // start-acquisition command, and only services one streaming session at
        // a time. A dedicated command socket caused start/stop/timed-acquisition
        // commands to be sent on command_socket_ while the monitor thread reads
        // events from socket_ — so events were streamed to a socket nobody read,
        // producing zero counts. Keeping a single socket restores the proven
        // behaviour: commands and the event stream share socket_, serialised via
        // socket_mutex_/response_mutex_. (command_socket_ stays closed; all
        // command/run-info paths fall back to socket_ when it is not open.)

        std::cout << "Connected to GeGI at " + ip + ":" + port << std::endl;
    }

    void TcpEventReader::startListening() {
        running_.store(true);

        if (monitoring_thread_.joinable()) {
            std::cerr << "Error: Trying to start listening on TCP socket when already listening" << std::endl;
            return;
        }

        monitoring_thread_ = std::thread(&TcpEventReader::monitorSocket, this);
    }

    void TcpEventReader::monitorSocket() {
        std::vector<char> stream_buffer;
        stream_buffer.reserve(4096);

        // Packet sizes per event type (from PHDS protocol documentation)
        // Type 0 (Single-Site):  4(type) + 8(time) + 8*4(x,y,z,e) = 44 bytes
        // Type 1 (Two-Site):     4(type) + 8(time) + 8*8(x1,y1,z1,e1,x2,y2,z2,e2) = 76 bytes
        // Type 2 (Compton):      4(type) + 8(time) + 8*10(x1,y1,z1,e1,x2,y2,z2,e2,angle,delta) = 92 bytes
        constexpr size_t SINGLE_SITE_BYTES = 44;
        constexpr size_t TWO_SITE_BYTES = 76;
        constexpr size_t COMPTON_BYTES = 92;

        auto packet_size_for_type = [](uint32_t event_type) -> size_t {
            switch (event_type) {
                case 0: return SINGLE_SITE_BYTES;
                case 1: return TWO_SITE_BYTES;
                case 2: return COMPTON_BYTES;
                default: return 0;
            }
        };

        auto read_u32_buf = [](const std::vector<char>& buf, size_t offset) {
            uint32_t value;
            std::memcpy(&value, buf.data() + offset, sizeof(uint32_t));
            return value;
        };

        auto read_double_buf = [](const std::vector<char>& buf, size_t offset) {
            double value;
            std::memcpy(&value, buf.data() + offset, sizeof(double));
            return value;
        };

        // Validate common fields at given base offset: event_type and first-site spatial/energy
        auto validate_header = [&](size_t base, size_t buf_size) -> bool {
            if (base + 44 > buf_size) return false;

            const uint32_t et = read_u32_buf(stream_buffer, base);
            if (et > 2) return false;

            const double x1 = read_double_buf(stream_buffer, base + 12);
            const double y1 = read_double_buf(stream_buffer, base + 20);
            const double z1 = read_double_buf(stream_buffer, base + 28);
            const double e1 = read_double_buf(stream_buffer, base + 36);

            if (!std::isfinite(x1) || !std::isfinite(y1) || !std::isfinite(z1) || !std::isfinite(e1))
                return false;
            if (std::fabs(x1) > 80.0 || std::fabs(y1) > 80.0 || std::fabs(z1) > 80.0)
                return false;
            if (e1 < 0.05 || e1 > 5000.0)
                return false;

            return true;
        };

        while (running_.load()) {
            std::array<char, 1024> data_in{};
            boost::system::error_code error;
            size_t bytes_read = 0;

            {
                std::unique_lock<std::mutex> resp_lock(response_mutex_, std::try_to_lock);
                if (!resp_lock.owns_lock()) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                    continue;
                }

                const size_t available = socket_.available(error);
                if (error) {
                    std::cerr << "Socket available() failed: " << error.message() << std::endl;
                    running_.store(false);
                    return;
                }
                if (available == 0) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                    continue;
                }

                std::lock_guard<std::mutex> lock(socket_mutex_);
                bytes_read = socket_.read_some(boost::asio::buffer(data_in), error);
            }

            if (error == boost::asio::error::eof) {
                std::cerr << "Connection lost" << std::endl;
                running_.store(false);
                return;
            } else if (error) {
                std::cerr << "Error reading from socket: " << error.message() << std::endl;
                continue;
            }

            if (bytes_read == 0) continue;

            stream_buffer.insert(stream_buffer.end(), data_in.begin(), data_in.begin() + static_cast<long long>(bytes_read));

            // Parse variable-length packets from the stream buffer
            while (stream_buffer.size() >= SINGLE_SITE_BYTES) {
                // Try to find a valid frame header at current position
                if (!validate_header(0, stream_buffer.size())) {
                    // Out of sync — discard one byte and retry
                    stream_buffer.erase(stream_buffer.begin());
                    continue;
                }

                const uint32_t event_type = read_u32_buf(stream_buffer, 0);
                const size_t pkt_size = packet_size_for_type(event_type);

                if (pkt_size == 0) {
                    // Unknown type, discard byte
                    stream_buffer.erase(stream_buffer.begin());
                    continue;
                }

                // Wait for full packet
                if (stream_buffer.size() < pkt_size) break;

                // Extract fields common to all types
                const double global_time = static_cast<double>(
                    *reinterpret_cast<const uint64_t*>(stream_buffer.data() + 4));
                const double x1 = read_double_buf(stream_buffer, 12);
                const double y1 = read_double_buf(stream_buffer, 20);
                const double z1 = read_double_buf(stream_buffer, 28);
                const double e1 = read_double_buf(stream_buffer, 36);

                if (event_type == 0) {
                    // Single-site event
                    compton_events::Event1Site ev{};
                    ev.global_time = global_time;
                    ev.compton_interaction.site.x = x1;
                    ev.compton_interaction.site.y = y1;
                    ev.compton_interaction.site.z = z1;
                    ev.compton_interaction.avg_energy = e1;
                    callback_1_site_(ev);

                } else if (event_type == 1) {
                    // Two-site event (no Compton angles)
                    const double x2 = read_double_buf(stream_buffer, 44);
                    const double y2 = read_double_buf(stream_buffer, 52);
                    const double z2 = read_double_buf(stream_buffer, 60);
                    const double e2 = read_double_buf(stream_buffer, 68);

                    // Treat as 1-site if second interaction is invalid
                    const bool site2_valid = std::isfinite(x2) && std::isfinite(y2) &&
                                             std::isfinite(z2) && std::isfinite(e2) &&
                                             std::fabs(x2) <= 80.0 && std::fabs(y2) <= 80.0 &&
                                             std::fabs(z2) <= 80.0 && e2 > 0.05 && e2 < 5000.0;

                    if (site2_valid) {
                        // Add each interaction energy separately to preserve spectral features
                        compton_events::Event1Site ev1{};
                        ev1.global_time = global_time;
                        ev1.compton_interaction.site.x = x1;
                        ev1.compton_interaction.site.y = y1;
                        ev1.compton_interaction.site.z = z1;
                        ev1.compton_interaction.avg_energy = e1;
                        callback_1_site_(ev1);

                        compton_events::Event1Site ev2{};
                        ev2.global_time = global_time;
                        ev2.compton_interaction.site.x = x2;
                        ev2.compton_interaction.site.y = y2;
                        ev2.compton_interaction.site.z = z2;
                        ev2.compton_interaction.avg_energy = e2;
                        callback_1_site_(ev2);
                    } else {
                        compton_events::Event1Site ev{};
                        ev.global_time = global_time;
                        ev.compton_interaction.site.x = x1;
                        ev.compton_interaction.site.y = y1;
                        ev.compton_interaction.site.z = z1;
                        ev.compton_interaction.avg_energy = e1;
                        callback_1_site_(ev);
                    }

                } else {
                    // Compton event (type 2) — full imaging event
                    const double x2 = read_double_buf(stream_buffer, 44);
                    const double y2 = read_double_buf(stream_buffer, 52);
                    const double z2 = read_double_buf(stream_buffer, 60);
                    const double e2 = read_double_buf(stream_buffer, 68);
                    const double compton_angle = read_double_buf(stream_buffer, 76);
                    const double delta_compton_angle = read_double_buf(stream_buffer, 84);

                    const bool site2_valid = std::isfinite(x2) && std::isfinite(y2) &&
                                             std::isfinite(z2) && std::isfinite(e2) &&
                                             std::fabs(x2) <= 80.0 && std::fabs(y2) <= 80.0 &&
                                             std::fabs(z2) <= 80.0 && e2 > 0.05 && e2 < 5000.0;
                    const bool angles_valid = std::isfinite(compton_angle) && std::isfinite(delta_compton_angle) &&
                                              compton_angle >= 0.0 && compton_angle <= 3.15 &&
                                              delta_compton_angle >= 0.0 && delta_compton_angle <= 1.0;

                    if (site2_valid && angles_valid) {
                        compton_events::Event2Site ev{};
                        ev.global_time = global_time;
                        ev.compton_interaction_1.site.x = x1;
                        ev.compton_interaction_1.site.y = y1;
                        ev.compton_interaction_1.site.z = z1;
                        ev.compton_interaction_1.avg_energy = e1;
                        ev.compton_interaction_2.site.x = x2;
                        ev.compton_interaction_2.site.y = y2;
                        ev.compton_interaction_2.site.z = z2;
                        ev.compton_interaction_2.avg_energy = e2;
                        ev.compton_angle = compton_angle;
                        ev.delta_compton_angle = delta_compton_angle;
                        callback_2_site_(ev);
                    } else {
                        // Demote to 1-site
                        compton_events::Event1Site ev{};
                        ev.global_time = global_time;
                        ev.compton_interaction.site.x = x1;
                        ev.compton_interaction.site.y = y1;
                        ev.compton_interaction.site.z = z1;
                        ev.compton_interaction.avg_energy = e1;
                        callback_1_site_(ev);
                    }
                }

                // Consume the packet from the buffer
                stream_buffer.erase(stream_buffer.begin(), stream_buffer.begin() + static_cast<long long>(pkt_size));
            }
        }
    }

    void TcpEventReader::stopListening() {
        running_.store(false);

        if (monitoring_thread_.joinable()) {
            // Closing the socket unblocks the listener thread's blocking read.
            // This path is used during shutdown, so reconnect is not expected.
            boost::system::error_code ignored_error;
            socket_.close(ignored_error);
            monitoring_thread_.join();
        }
    }

    void TcpEventReader::disconnect() {
        if (monitoring_thread_.joinable()) {
            throw std::runtime_error("Trying to disconnect from socket when monitoring data off it");
        }

        if (!socket_.is_open() && !command_socket_.is_open()) {
            std::cerr << "Trying to disconnect from closed sockets" << std::endl;
            return;
        }

        boost::system::error_code error;

        if (command_socket_.is_open()) {
            command_socket_.shutdown(boost::asio::ip::tcp::socket::shutdown_both, error);
            command_socket_.close(error);
        }

        if (socket_.is_open()) {
            socket_.shutdown(boost::asio::ip::tcp::socket::shutdown_both, error);
            if (error) {
                throw std::runtime_error("Could not shutdown socket");
            }

            socket_.close(error);
            if (error) {
                throw std::runtime_error("Could not close socket");
            }
        }
    }

    bool TcpEventReader::sendCommand(char cmd) {
        std::lock_guard<std::mutex> command_lock(command_mutex_);
        boost::asio::ip::tcp::socket *command_channel = command_socket_.is_open() ? &command_socket_ : &socket_;

        if (!command_channel->is_open()) {
            std::cerr << "Socket not open; cannot send command '" << cmd << "'" << std::endl;
            return false;
        }

        try {
            if (command_channel == &socket_) {
                std::lock_guard<std::mutex> lock(socket_mutex_);
                boost::asio::write(*command_channel, boost::asio::buffer(&cmd, 1));
            } else {
                boost::asio::write(*command_channel, boost::asio::buffer(&cmd, 1));
            }
            std::cout << "Sent command '" << cmd << "' to detector" << std::endl;
            return true;
        } catch (const std::exception &ex) {
            std::cerr << "Failed to send command '" << cmd << "': " << ex.what() << std::endl;
            return false;
        }
    }

    bool TcpEventReader::sendStartAcquisition() {
        return sendCommand('g');  // 'g' = Start Data Acquisition
    }

    bool TcpEventReader::sendStopAcquisition() {
        return sendCommand('s');  // 's' = Stop Data Acquisition
    }

    bool TcpEventReader::sendClearData() {
        return sendCommand('c');  // 'c' = Clear Data
    }

    bool TcpEventReader::sendClearDataAndWindows() {
        return sendCommand('x');  // 'x' = Clear Data and Energy Windows
    }

    bool TcpEventReader::sendToggleBiasMode() {
        return sendCommand('b');  // 'b' = Toggle Detector Mode (bias)
    }

    bool TcpEventReader::sendTimedAcquisition(char preset_cmd) {
        // Presets '1'-'8' map to 5/10/15/20/25/30/45/60-minute (live-time)
        // acquisitions per the GeGI remote-command manual.
        if (preset_cmd < '1' || preset_cmd > '8') {
            std::cerr << "Invalid timed acquisition preset command '" << preset_cmd << "'" << std::endl;
            return false;
        }

        return sendCommand(preset_cmd);
    }

    // ─── Helpers for command response extraction ───────────────────────────
    //
    // The GeGI detector multiplexes event stream data and command responses on
    // a single TCP connection.  We accumulate all received bytes after sending
    // a command and scan for the response frame using strong plausibility
    // checks that exploit the mathematical self-consistency of the response
    // fields (e.g. dead_time ≈ (1 - live/real) × 100).
    // ─────────────────────────────────────────────────────────────────────────

    namespace {
        // Flush, send command, accumulate raw bytes until deadline or max_bytes.
        std::vector<char> sendAndAccumulateRaw(
                boost::asio::ip::tcp::socket &socket,
                char cmd,
                std::chrono::steady_clock::time_point deadline,
                size_t max_bytes = 16384) {

            std::cout << "sendAndAccumulateRaw('" << cmd << "'): socket open="
                      << socket.is_open() << " native=" << socket.native_handle()
                      << std::endl;

            flushSocketInputBuffer(socket);

            boost::system::error_code write_error;
            boost::asio::write(socket, boost::asio::buffer(&cmd, 1), write_error);
            if (write_error) {
                std::cerr << "sendAndAccumulateRaw: write FAILED: " << write_error.message() << std::endl;
                return {};
            }
            std::cout << "sendAndAccumulateRaw('" << cmd << "'): command sent OK" << std::endl;

            std::vector<char> buffer;
            buffer.reserve(max_bytes);
            int poll_count = 0;

            while (std::chrono::steady_clock::now() < deadline && buffer.size() < max_bytes) {
                boost::system::error_code error;
                const size_t available = socket.available(error);
                if (error) {
                    std::cerr << "sendAndAccumulateRaw: available() error: " << error.message() << std::endl;
                    break;
                }

                if (available > 0) {
                    const size_t to_read = std::min<size_t>(available, 4096);
                    std::vector<char> chunk(to_read);
                    const size_t n = socket.read_some(boost::asio::buffer(chunk), error);
                    if (error) {
                        std::cerr << "sendAndAccumulateRaw: read_some() error: " << error.message() << std::endl;
                        break;
                    }
                    buffer.insert(buffer.end(), chunk.begin(), chunk.begin() + static_cast<long long>(n));
                } else {
                    poll_count++;
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                }
            }

            std::cout << "sendAndAccumulateRaw('" << cmd << "'): accumulated "
                      << buffer.size() << " bytes, polled " << poll_count << " times" << std::endl;
            return buffer;
        }
    }

    RunInfo TcpEventReader::getRunInfo() {
        RunInfo info;
        if (!socket_.is_open() && !command_socket_.is_open()) {
            std::cerr << "Socket not open; cannot request run info" << std::endl;
            return info;
        }

        try {
            std::lock_guard<std::mutex> command_lock(command_mutex_);

            // Use command_socket_ if available (it receives events + responses
            // from the detector). Fall back to stream socket if not open.
            boost::asio::ip::tcp::socket &response_socket = command_socket_.is_open() ? command_socket_ : socket_;
            const bool using_event_socket = (&response_socket == &socket_);

            std::unique_lock<std::mutex> resp_lock(response_mutex_, std::defer_lock);
            std::unique_lock<std::mutex> event_lock(socket_mutex_, std::defer_lock);
            if (using_event_socket) {
                resp_lock.lock();
                event_lock.lock();
            }

            // Run-info frame for command 'i'. The manual lists the LOGICAL fields
            //   int32 realTime; double liveTime; double deadTimePercentage; double countRate;
            // but the on-wire byte layout depends on the sender's struct alignment,
            // so we try the plausible layouts and accept the first self-consistent
            // one (the dead ~= (1-live/real)*100 check disambiguates):
            //   A) packed  (28B): u32@0,       f64@4,  f64@12, f64@20
            //   B) aligned (32B): u32@0,+4 pad, f64@8,  f64@16, f64@24  (double 8-byte aligned)
            //   C) all-f64 (32B): f64@0,        f64@8,  f64@16, f64@24  (realTime sent as double)
            auto decode_A = [&](const char *b) {
                RunInfo d;
                d.real_time_sec = static_cast<double>(readU32(b + 0, false));
                d.live_time_sec = readF64(b + 4, false);
                d.dead_time_percent = readF64(b + 12, false);
                d.count_rate_hz = readF64(b + 20, false);
                return d;
            };
            auto decode_B = [&](const char *b) {
                RunInfo d;
                d.real_time_sec = static_cast<double>(readU32(b + 0, false));
                d.live_time_sec = readF64(b + 8, false);
                d.dead_time_percent = readF64(b + 16, false);
                d.count_rate_hz = readF64(b + 24, false);
                return d;
            };
            auto decode_C = [&](const char *b) {
                RunInfo d;
                d.real_time_sec = readF64(b + 0, false);
                d.live_time_sec = readF64(b + 8, false);
                d.dead_time_percent = readF64(b + 16, false);
                d.count_rate_hz = readF64(b + 24, false);
                return d;
            };

            auto plausible_run_info = [](const RunInfo &c) {
                // Basic range checks
                if (!std::isfinite(c.real_time_sec) || !std::isfinite(c.live_time_sec)
                    || !std::isfinite(c.dead_time_percent) || !std::isfinite(c.count_rate_hz))
                    return false;
                if (c.real_time_sec < 0.0 || c.real_time_sec > 86400.0) return false;
                if (c.live_time_sec < 0.0 || c.live_time_sec > 86400.0) return false;
                if (c.live_time_sec > c.real_time_sec + 0.001) return false;
                if (c.dead_time_percent < 0.0 || c.dead_time_percent > 100.0) return false;
                if (c.count_rate_hz < 0.0 || c.count_rate_hz > 1.0e7) return false;

                // Idle state: all zeros is valid
                const bool idle = (c.real_time_sec == 0.0 && c.live_time_sec == 0.0
                                   && c.count_rate_hz == 0.0 && c.dead_time_percent == 0.0);
                if (idle) return true;

                // Active acquisition checks
                if (c.real_time_sec < 1.0) return false;
                if (c.count_rate_hz < 1.0) return false;

                // Live fraction must be reasonable
                const double lf = c.live_time_sec / c.real_time_sec;
                if (lf < 0.01 || lf > 1.001) return false;

                // CRITICAL self-consistency check:
                // dead_time_percent must approximately equal (1 - live/real) * 100
                const double derived_dead = 100.0 * (1.0 - lf);
                if (std::fabs(c.dead_time_percent - derived_dead) > 1.0) return false;

                return true;
            };

            bool found = false;
            std::vector<char> last_buffer;
            std::string found_layout;

            // Command 'i' = "Requests Run Information". (Note: 'r' is the
            // detector's reachback/file-save command — do NOT use it here.)
            // The response shares the event socket, so when acquisition is
            // streaming the 28-byte frame may be surrounded by event packets;
            // we scan offsets and accept the first self-consistent frame. When
            // queried post-stop (the intended path) the reply arrives clean and
            // the first offset matches immediately.
            // While acquisition is streaming, the 'i' reply is buried among event
            // packets on this shared socket, so a single short read often misses it.
            // Retry more times, accumulate longer, and scan a larger buffer to raise
            // the hit-rate under load. Loop breaks immediately once a frame is found,
            // so a clean (idle/post-stop) reply still returns fast.
            for (int attempt = 0; attempt < 6 && !found; ++attempt) {
                const auto deadline = std::chrono::steady_clock::now()
                                      + std::chrono::milliseconds(2000);
                auto buffer = sendAndAccumulateRaw(
                    response_socket, 'i', deadline, 65536);

                const size_t bufsz = buffer.size();
                for (size_t i = 0; i + 28 <= bufsz && !found; ++i) {
                    const char *b = buffer.data() + i;
                    { auto d = decode_A(b); if (plausible_run_info(d)) { info = d; found = true; found_layout = "A(28B packed)"; break; } }
                    if (i + 32 <= bufsz) {
                        { auto d = decode_B(b); if (plausible_run_info(d)) { info = d; found = true; found_layout = "B(32B aligned)"; break; } }
                        { auto d = decode_C(b); if (plausible_run_info(d)) { info = d; found = true; found_layout = "C(32B 4xf64)"; break; } }
                    }
                }
                last_buffer = buffer;
            }

            if (!found) {
                std::cerr << "Timed out waiting for valid run info response" << std::endl;
                if (!last_buffer.empty()) {
                    const size_t n = std::min<size_t>(last_buffer.size(), 96);
                    std::ostringstream oss;
                    oss << std::hex << std::setfill('0');
                    for (size_t i = 0; i < n; ++i) {
                        oss << std::setw(2)
                            << (static_cast<unsigned int>(
                                static_cast<unsigned char>(last_buffer[i])));
                    }
                    std::cerr << "Run info debug: captured " << last_buffer.size()
                              << " bytes, first " << n << " hex=" << oss.str() << std::endl;
                }
                info.real_time_sec = std::numeric_limits<double>::quiet_NaN();
                info.live_time_sec = std::numeric_limits<double>::quiet_NaN();
                info.dead_time_percent = std::numeric_limits<double>::quiet_NaN();
                info.count_rate_hz = std::numeric_limits<double>::quiet_NaN();
            }

            std::cout << "Run Info [" << (found ? found_layout : "FAIL") << "]: realTime=" << info.real_time_sec << "s, "
                      << "liveTime=" << info.live_time_sec << "s, "
                      << "deadTime=" << info.dead_time_percent << "%, "
                      << "countRate=" << info.count_rate_hz << " Hz" << std::endl;

        } catch (const std::exception &ex) {
            std::cerr << "Failed to get run info: " << ex.what() << std::endl;
        }

        return info;
    }

    DetectorInfo TcpEventReader::getDetectorInfo() {
        DetectorInfo info;
        if (!socket_.is_open() && !command_socket_.is_open()) {
            std::cerr << "Socket not open; cannot request detector info" << std::endl;
            return info;
        }

        try {
            std::lock_guard<std::mutex> command_lock(command_mutex_);

            // Detector-info frame layout (28 bytes, little-endian):
            //   [0..3]   char[4] serial_number (e.g. "G596")
            //   [4..11]  float64 temperature
            //   [12..15] int32   bias_status (0 or 1)
            //   [16..19] int32   line_power_status (0 or 1)
            //   [20..23] int32   batt1_percent
            //   [24..27] int32   batt2_percent
            constexpr size_t FRAME_BYTES = 28;

            auto decode_detector_info = [&](const char *base) {
                DetectorInfo decoded;
                std::string serial(base, 4);
                serial.erase(std::find(serial.begin(), serial.end(), '\0'), serial.end());
                decoded.serial_number = serial;
                decoded.detector_temp_kelvin = readF64(base + 4, false);
                decoded.detector_bias_status = readI32(base + 12, false);
                decoded.line_power_status = readI32(base + 16, false);
                decoded.batt1_percent = readI32(base + 20, false);
                decoded.batt2_percent = readI32(base + 24, false);
                return decoded;
            };

            auto plausible_detector_info = [](const DetectorInfo &c) {
                // Serial must be printable alphanumeric (e.g. "G596")
                if (c.serial_number.empty() || c.serial_number.size() > 4) return false;
                for (char ch : c.serial_number) {
                    if (!std::isalnum(static_cast<unsigned char>(ch)) && ch != '-')
                        return false;
                }
                // First char should be a letter (detector model prefix)
                if (!std::isalpha(static_cast<unsigned char>(c.serial_number[0])))
                    return false;
                if (!std::isfinite(c.detector_temp_kelvin)) return false;
                if (c.detector_temp_kelvin < -50.0 || c.detector_temp_kelvin > 200.0) return false;
                if (c.detector_bias_status != 0 && c.detector_bias_status != 1) return false;
                if (c.line_power_status != 0 && c.line_power_status != 1) return false;
                if (c.batt1_percent < 0 || c.batt1_percent > 100) return false;
                if (c.batt2_percent < 0 || c.batt2_percent > 100) return false;
                return true;
            };

            bool found = false;
            std::vector<char> last_buffer;

            // Use command socket first (receives events + responses), then stream
            std::vector<std::pair<boost::asio::ip::tcp::socket *, std::string>> channels;
            if (command_socket_.is_open()) {
                channels.emplace_back(&command_socket_, "command");
            }
            if (socket_.is_open()) {
                channels.emplace_back(&socket_, "stream");
            }

            // Command 'd' = "Requests Detector Status Info" (the only valid
            // form; 'D' is undefined in the GeGI protocol).
            const std::array<char, 1> cmds{{'d'}};
            for (const auto &channel : channels) {
                boost::asio::ip::tcp::socket &response_socket = *channel.first;
                const bool using_stream_socket = (&response_socket == &socket_);

                std::unique_lock<std::mutex> resp_lock(response_mutex_, std::defer_lock);
                std::unique_lock<std::mutex> event_lock(socket_mutex_, std::defer_lock);
                if (using_stream_socket) {
                    resp_lock.lock();
                    event_lock.lock();
                }

                for (char cmd : cmds) {
                    for (int attempt = 0; attempt < 3 && !found; ++attempt) {
                        const auto deadline = std::chrono::steady_clock::now()
                                              + std::chrono::milliseconds(2000);
                        auto buffer = sendAndAccumulateRaw(
                            response_socket, cmd, deadline, 16384);

                        // Scan buffer for a valid detector-info frame
                        if (buffer.size() >= FRAME_BYTES) {
                            for (size_t i = 0; i + FRAME_BYTES <= buffer.size(); ++i) {
                                auto candidate = decode_detector_info(buffer.data() + i);
                                if (plausible_detector_info(candidate)) {
                                    info = candidate;
                                    found = true;
                                    std::cout << "Found detector-info at offset " << i
                                              << " in " << buffer.size() << " bytes ("
                                              << channel.second << ")" << std::endl;
                                    break;
                                }
                            }
                        }
                        last_buffer = buffer;
                    }
                    if (found) break;
                }
                if (found) break;
            }

            if (!found) {
                std::cerr << "Timed out waiting for valid detector info response" << std::endl;
                if (!last_buffer.empty()) {
                    const size_t n = std::min<size_t>(last_buffer.size(), 64);
                    std::ostringstream oss;
                    oss << std::hex << std::setfill('0');
                    for (size_t i = 0; i < n; ++i) {
                        oss << std::setw(2)
                            << (static_cast<unsigned int>(
                                static_cast<unsigned char>(last_buffer[i])));
                    }
                    std::cerr << "Detector info debug: captured "
                              << last_buffer.size() << " bytes, first " << n
                              << " hex=" << oss.str() << std::endl;
                }
            }

            std::cout << "Detector Info: serial=" << info.serial_number
                      << ", temp=" << info.detector_temp_kelvin << " K"
                      << ", biasStatus=" << info.detector_bias_status
                      << ", linePower=" << info.line_power_status
                      << ", batt1=" << info.batt1_percent << "%"
                      << ", batt2=" << info.batt2_percent << "%" << std::endl;
        } catch (const std::exception &ex) {
            std::cerr << "Failed to get detector info: " << ex.what() << std::endl;
        }

        return info;
    }

    TcpEventReader::~TcpEventReader() {
        stopListening();
        disconnect();
    }

} // namespace phds_gegi_driver::socket_comms
