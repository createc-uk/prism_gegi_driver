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
#if defined(__linux__)
#include <cerrno>
#include <poll.h>
#include <sys/socket.h>
#endif

namespace phds_gegi_driver::socket_comms {

    namespace {
        // On Linux, available()==0 is also true after a clean TCP FIN.
        // A nonblocking peek distinguishes EOF from an idle but live detector
        // without consuming bytes from the single streaming socket.
        bool peerHasClosed(boost::asio::ip::tcp::socket &socket) {
#if defined(__linux__)
            if (!socket.is_open()) return true;
            pollfd fd{};
            fd.fd = socket.native_handle();
            fd.events = POLLIN | POLLERR | POLLHUP;
#ifdef POLLRDHUP
            fd.events |= POLLRDHUP;
#endif
            if (::poll(&fd, 1, 0) <= 0) return false;
            if (fd.revents & (POLLERR | POLLHUP | POLLNVAL)) return true;
#ifdef POLLRDHUP
            if (fd.revents & POLLRDHUP) return true;
#endif
            if (fd.revents & POLLIN) {
                char peek = 0;
                return ::recv(fd.fd, &peek, 1, MSG_PEEK | MSG_DONTWAIT) == 0;
            }
#else
            (void)socket;
#endif
            return false;
        }

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

        // Try to decode a detector run-info frame at the front of `b` (with `avail`
        // bytes available). The detector interleaves these 28/32-byte frames into
        // the event stream in reply to the 'i' command; the monitor thread calls
        // this at packet boundaries so the frame is CAPTURED inline instead of
        // discarded during resync. dead_time is RECOMPUTED from (1 - live/real)
        // rather than trusting the detector's dead field (observed self-inconsistent).
        // The checks are kept strict so stray event-packet bytes are very unlikely
        // to pass. On success sets `out` and `consumed` (28 or 32) and returns true.
        bool tryDecodeRunInfo(const char *b, size_t avail, RunInfo &out, size_t &consumed) {
            auto plausible = [](double real, double live, double rate) -> bool {
                if (!std::isfinite(real) || !std::isfinite(live) || !std::isfinite(rate)) return false;
                if (real == 0.0 && live == 0.0 && rate == 0.0) return true;   // idle frame
                if (real < 1.0 || real > 86400.0) return false;
                if (live < 0.0 || live > real + 0.001) return false;
                if (rate < 1.0 || rate > 1.0e7) return false;
                const double lf = live / real;
                if (lf < 0.5 || lf > 1.001) return false;   // active dead-time < 50%
                return true;
            };
            auto fill = [&](double real, double live, double rate, size_t used) {
                out.real_time_sec = real;
                out.live_time_sec = live;
                out.count_rate_hz = rate;
                out.dead_time_percent = (real > 0.0) ? 100.0 * (1.0 - live / real) : 0.0;
                consumed = used;
            };
            // Layout A: packed 28B  (u32 real@0, f64 live@4,  f64 dead@12, f64 rate@20)
            if (avail >= 28) {
                const double real = static_cast<double>(readU32(b + 0, false));
                const double live = readF64(b + 4, false);
                const double rate = readF64(b + 20, false);
                if (plausible(real, live, rate)) { fill(real, live, rate, 28); return true; }
            }
            // Layout B: aligned 32B (u32 real@0, pad, f64 live@8, f64 dead@16, f64 rate@24)
            if (avail >= 32) {
                const double real = static_cast<double>(readU32(b + 0, false));
                const double live = readF64(b + 8, false);
                const double rate = readF64(b + 24, false);
                if (plausible(real, live, rate)) { fill(real, live, rate, 32); return true; }
            }
            // Layout C: all-f64 32B (f64 real@0, f64 live@8, f64 dead@16, f64 rate@24)
            if (avail >= 32) {
                const double real = readF64(b + 0, false);
                const double live = readF64(b + 8, false);
                const double rate = readF64(b + 24, false);
                if (plausible(real, live, rate)) { fill(real, live, rate, 32); return true; }
            }
            return false;
        }

        // The 'd' status reply is a 28-byte frame multiplexed among 44/76/92
        // byte detector events and 28/32-byte run-info frames. Decode on the
        // monitor thread ONLY, so status polling never discards detector events.
        bool tryDecodeDetectorInfo(const char *b, size_t available, DetectorInfo &out) {
            if (available < 28) return false;
            DetectorInfo candidate;
            std::string serial(b, 4);
            serial.erase(std::find(serial.begin(), serial.end(), '\0'), serial.end());
            if (serial.empty() || serial.size() > 4
                || !std::isalpha(static_cast<unsigned char>(serial.front()))) {
                return false;
            }
            for (char ch : serial) {
                if (!std::isalnum(static_cast<unsigned char>(ch)) && ch != '-') return false;
            }
            candidate.serial_number = serial;
            candidate.detector_temp_kelvin = readF64(b + 4, false);
            candidate.detector_bias_status = readI32(b + 12, false);
            candidate.line_power_status = readI32(b + 16, false);
            candidate.batt1_percent = readI32(b + 20, false);
            candidate.batt2_percent = readI32(b + 24, false);
            if (!std::isfinite(candidate.detector_temp_kelvin)
                || candidate.detector_temp_kelvin < -50.0
                || candidate.detector_temp_kelvin > 200.0
                || (candidate.detector_bias_status != 0 && candidate.detector_bias_status != 1)
                || (candidate.line_power_status != 0 && candidate.line_power_status != 1)
                || candidate.batt1_percent < 0 || candidate.batt1_percent > 100
                || candidate.batt2_percent < 0 || candidate.batt2_percent > 100) {
                return false;
            }
            out = candidate;
            return true;
        }

    }

    TcpEventReader::TcpEventReader(Callback1Site callback_1_site, Callback2Site callback_2_site)
            : callback_1_site_(std::move(callback_1_site)),
              callback_2_site_(std::move(callback_2_site)),
              socket_(io_service_),
              command_socket_(io_service_) {}

    void TcpEventReader::connect(const std::string &ip, const std::string &port) {
        using namespace boost::asio::ip;

        // Retain the endpoint even when the initial connect fails, so the
        // monitor thread can reconnect without restarting Prism.
        ip_ = ip;
        port_ = port;
        connected_.store(false);
        std::cout << "Connecting to GeGI at " + ip + ":" + port << "..." << std::endl;

        tcp::resolver resolver(io_service_);
        boost::asio::connect(socket_, resolver.resolve(ip, port));
        connected_.store(true);

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

    bool TcpEventReader::tryReconnectOnce() {
        if (ip_.empty()) return false;
        std::lock_guard<std::mutex> lock(socket_mutex_);
        boost::system::error_code ignored;
        socket_.close(ignored);
        try {
            boost::asio::ip::tcp::resolver resolver(io_service_);
            boost::asio::connect(socket_, resolver.resolve(ip_, port_));
            // Old status frames must never leak into a new connection epoch.
            have_run_info_.store(false);
            return true;
        } catch (const std::exception &ex) {
            std::cerr << "GeGI reconnect attempt failed: " << ex.what() << std::endl;
            socket_.close(ignored);
            return false;
        }
    }

    double TcpEventReader::secondsSinceLastEvent() const {
        const auto last = last_event_steady_ms_.load();
        if (last < 0) return -1.0;
        const auto now = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
        return static_cast<double>(now - last) / 1000.0;
    }

    void TcpEventReader::startListening() {
        if (monitoring_thread_.joinable()) {
            std::cerr << "Error: Trying to start listening on TCP socket when already listening" << std::endl;
            return;
        }
        running_.store(true);
        monitoring_thread_ = std::thread(&TcpEventReader::monitorSocket, this);
    }

    void TcpEventReader::setEnergyCorrection(double c0, double c1, double c2) {
        ecal_c0_ = c0;
        ecal_c1_ = c1;
        ecal_c2_ = c2;
        if (c0 != 0.0 || c1 != 1.0 || c2 != 0.0) {
            std::cout << "Energy-scale correction active: E_true = " << c0
                      << " + " << c1 << "*E + " << c2 << "*E^2" << std::endl;
        }
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

        // Only the monitor reconnects. No acquisition commands are replayed.
        constexpr int FIRST_BACKOFF_MS = 1000;
        constexpr int MAX_BACKOFF_MS = 30000;
        int backoff_ms = FIRST_BACKOFF_MS;

        while (running_.load()) {
            if (!connected_.load()) {
                // Keep shutdown responsive throughout the backoff interval.
                for (int waited = 0; running_.load() && waited < backoff_ms; waited += 100)
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                if (!running_.load()) break;
                if (tryReconnectOnce()) {
                    connected_.store(true);
                    reconnect_count_.fetch_add(1);
                    backoff_ms = FIRST_BACKOFF_MS;
                    stream_buffer.clear();
                    std::cout << "Reconnected to GeGI at " << ip_ << ":" << port_
                              << " (reconnect #" << reconnect_count_.load() << ")" << std::endl;
                } else {
                    backoff_ms = std::min(backoff_ms * 2, MAX_BACKOFF_MS);
                }
                continue;
            }

            std::array<char, 1024> data_in{};
            boost::system::error_code error;
            size_t bytes_read = 0;
            bool idle = false;
            {
                std::unique_lock<std::mutex> resp_lock(response_mutex_, std::try_to_lock);
                if (!resp_lock.owns_lock()) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                    continue;
                }
                // Serialize access with command sends and reconnect attempts.
                std::lock_guard<std::mutex> lock(socket_mutex_);
                if (!connected_.load()) continue;
                const size_t available = socket_.available(error);
                if (!error && available == 0) {
                    if (peerHasClosed(socket_)) error = boost::asio::error::eof;
                    else idle = true;
                } else if (!error) {
                    bytes_read = socket_.read_some(boost::asio::buffer(data_in), error);
                }
            }

            if (error) {
                std::cerr << "GeGI link lost (" << error.message()
                          << "); reconnecting without restarting Prism" << std::endl;
                connected_.store(false);
                have_run_info_.store(false);
                stream_buffer.clear();
                continue;
            }
            if (idle || bytes_read == 0) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
                continue;
            }

            stream_buffer.insert(stream_buffer.end(), data_in.begin(), data_in.begin() + static_cast<long long>(bytes_read));

            // Parse variable-length packets from the stream buffer. A run-info
            // reply can be only 28 or 32 bytes long. Requiring a 44-byte event
            // before parsing used to strand idle status replies indefinitely
            // (until another event happened to arrive).
            constexpr size_t MIN_RUN_INFO_BYTES = 28;
            while (stream_buffer.size() >= MIN_RUN_INFO_BYTES) {
                // With <44 bytes, first try a complete run-info reply. A
                // partially-received event must remain buffered for the next
                // read rather than having its initial bytes discarded.
                if (stream_buffer.size() < SINGLE_SITE_BYTES) {
                    DetectorInfo di;
                    if (tryDecodeDetectorInfo(stream_buffer.data(), stream_buffer.size(), di)) {
                        {
                            std::lock_guard<std::mutex> lk(detector_info_mutex_);
                            latest_detector_info_ = di;
                        }
                        detector_info_seq_.fetch_add(1);
                        stream_buffer.erase(stream_buffer.begin(), stream_buffer.begin() + 28);
                        continue;
                    }
                    RunInfo ri;
                    size_t ri_bytes = 0;
                    if (!tryDecodeRunInfo(stream_buffer.data(), stream_buffer.size(),
                                          ri, ri_bytes)) {
                        break;
                    }
                    {
                        std::lock_guard<std::mutex> lk(run_info_mutex_);
                        latest_run_info_ = ri;
                    }
                    have_run_info_.store(true);
                    run_info_seq_.fetch_add(1);
                    stream_buffer.erase(stream_buffer.begin(),
                                        stream_buffer.begin() + static_cast<long long>(ri_bytes));
                    continue;
                }
                // Try to find a valid event frame header at current position
                if (!validate_header(0, stream_buffer.size())) {
                    DetectorInfo di;
                    if (tryDecodeDetectorInfo(stream_buffer.data(), stream_buffer.size(), di)) {
                        {
                            std::lock_guard<std::mutex> lk(detector_info_mutex_);
                            latest_detector_info_ = di;
                        }
                        detector_info_seq_.fetch_add(1);
                        stream_buffer.erase(stream_buffer.begin(), stream_buffer.begin() + 28);
                        continue;
                    }
                    // Not an event header. Before discarding, check for a detector
                    // run-info frame (the 'i' reply) interleaved in the stream and
                    // CAPTURE it inline — so getRunInfo() reads it from here rather
                    // than a competing socket read that starves and drops events.
                    RunInfo ri;
                    size_t ri_bytes = 0;
                    if (tryDecodeRunInfo(stream_buffer.data(), stream_buffer.size(), ri, ri_bytes)) {
                        {
                            std::lock_guard<std::mutex> lk(run_info_mutex_);
                            latest_run_info_ = ri;
                        }
                        have_run_info_.store(true);
                        run_info_seq_.fetch_add(1);
                        stream_buffer.erase(stream_buffer.begin(),
                                            stream_buffer.begin() + static_cast<long long>(ri_bytes));
                        continue;
                    }
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
                const double e1 = correctEnergy(read_double_buf(stream_buffer, 36));

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
                    const double e2 = correctEnergy(read_double_buf(stream_buffer, 68));

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
                    const double e2 = correctEnergy(read_double_buf(stream_buffer, 68));
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

                // Count fully-decoded detector packets for health supervision.
                events_total_.fetch_add(1);
                last_event_steady_ms_.store(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        std::chrono::steady_clock::now().time_since_epoch()).count());

                // Consume the packet from the buffer
                stream_buffer.erase(stream_buffer.begin(), stream_buffer.begin() + static_cast<long long>(pkt_size));
            }
        }
    }

    void TcpEventReader::stopListening() {
        running_.store(false);
        connected_.store(false);

        if (monitoring_thread_.joinable()) {
            // Closing the socket unblocks the listener thread's blocking read.
            // This path is used during shutdown, so reconnect is not expected.
            boost::system::error_code ignored_error;
            socket_.close(ignored_error);
            monitoring_thread_.join();
        }
    }

    void TcpEventReader::disconnect() {
        connected_.store(false);
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

        if (!connected_.load() || !command_channel->is_open()) {
            std::cerr << "GeGI TCP link disconnected; rejecting command '" << cmd << "'" << std::endl;
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
            connected_.store(false);
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

    RunInfo TcpEventReader::getRunInfo() {
        // The monitor thread is the SOLE socket reader; it parses run-info frames
        // INLINE as they stream past (see monitorSocket + tryDecodeRunInfo). Here we
        // only PROMPT the detector with 'i' (a 1-byte write, no read) and wait for
        // the monitor to publish a fresh frame. A status query therefore never races
        // the event stream or discards event data, at any rate. If no fresh frame
        // arrives within the timeout we return NaN so the caller sees an invalid
        // read (success=false) rather than a stale or wrong-run value.
        RunInfo info;
        if (!connected_.load() || !socket_.is_open()) {
            std::cerr << "GeGI link disconnected; run info unavailable" << std::endl;
            info.real_time_sec = std::numeric_limits<double>::quiet_NaN();
            info.live_time_sec = std::numeric_limits<double>::quiet_NaN();
            info.dead_time_percent = std::numeric_limits<double>::quiet_NaN();
            info.count_rate_hz = std::numeric_limits<double>::quiet_NaN();
            return info;
        }

        const std::uint64_t seq_before = run_info_seq_.load();
        if (!sendCommand('i')) {
            info.real_time_sec = std::numeric_limits<double>::quiet_NaN();
            info.live_time_sec = std::numeric_limits<double>::quiet_NaN();
            info.dead_time_percent = std::numeric_limits<double>::quiet_NaN();
            info.count_rate_hz = std::numeric_limits<double>::quiet_NaN();
            return info;
        }

        // Wait (bounded) for the monitor to parse the reply frame inline.
        const auto deadline = std::chrono::steady_clock::now()
                              + std::chrono::milliseconds(1500);
        bool fresh = false;
        while (std::chrono::steady_clock::now() < deadline) {
            if (run_info_seq_.load() != seq_before) { fresh = true; break; }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }

        if (fresh) {
            std::lock_guard<std::mutex> lk(run_info_mutex_);
            info = latest_run_info_;
        } else {
            std::cerr << "Run info: no fresh frame within timeout "
                         "(is an acquisition running?)" << std::endl;
            info.real_time_sec = std::numeric_limits<double>::quiet_NaN();
            info.live_time_sec = std::numeric_limits<double>::quiet_NaN();
            info.dead_time_percent = std::numeric_limits<double>::quiet_NaN();
            info.count_rate_hz = std::numeric_limits<double>::quiet_NaN();
        }

        std::cout << "Run Info [" << (fresh ? "inline" : "FAIL") << "]: realTime="
                  << info.real_time_sec << "s, liveTime=" << info.live_time_sec
                  << "s, deadTime=" << info.dead_time_percent << "%, countRate="
                  << info.count_rate_hz << " Hz" << std::endl;
        return info;
    }

    DetectorInfo TcpEventReader::getDetectorInfo() {
        // One socket reader: monitorSocket() extracts status frames inline.
        // Never flush or consume from the event socket here, and never hold
        // socket_mutex_ while waiting for the status reply. The previous
        // implementation held it for 2-6 seconds, delaying clear/start/stop
        // commands beyond their Prism timeouts and losing event bytes.
        DetectorInfo info;
        if (!connected_.load() || !socket_.is_open()) return info;

        const std::uint64_t seq_before = detector_info_seq_.load();
        if (!sendCommand('d')) return info;

        const auto deadline = std::chrono::steady_clock::now()
                              + std::chrono::milliseconds(1500);
        while (std::chrono::steady_clock::now() < deadline) {
            if (detector_info_seq_.load() != seq_before) {
                std::lock_guard<std::mutex> lk(detector_info_mutex_);
                info = latest_detector_info_;
                std::cout << "Detector Info [inline]: serial=" << info.serial_number
                          << ", temp=" << info.detector_temp_kelvin << " K"
                          << ", biasStatus=" << info.detector_bias_status
                          << ", linePower=" << info.line_power_status
                          << ", batt1=" << info.batt1_percent << "%"
                          << ", batt2=" << info.batt2_percent << "%" << std::endl;
                return info;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        std::cerr << "Detector info: no fresh frame within timeout" << std::endl;
        return info;
    }

    TcpEventReader::~TcpEventReader() {
        stopListening();
        disconnect();
    }

} // namespace phds_gegi_driver::socket_comms
