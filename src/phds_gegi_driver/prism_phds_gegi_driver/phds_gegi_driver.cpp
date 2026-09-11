//
// Created by createc on 10/09/2020.
// Migrated from ROS 1 to Prism messaging.
//

#include <phds_gegi_driver/phds_gegi_driver.hpp>

#include <prism/types/values.hpp>

#include <chrono>
#include <cmath>
#include <limits>

namespace {
    using phds_gegi_driver::messages::nowSeconds;

    psm::TextSender::Config makeSenderConfig(psm::Application &app,
                                              const std::string &id,
                                              const std::string &defaultTopic) {
        psm::TextSender::Config config;
        config.id = id;
        config.destination = defaultTopic;
        if (auto override = app.getConfig().getEndpointParam(id, "name")) {
            config.destination = *override;
        }
        return config;
    }

    psm::TextReceiver::Config makeReceiverConfig(psm::Application &app,
                                                  const std::string &id,
                                                  const std::string &defaultTopic) {
        psm::TextReceiver::Config config;
        config.id = id;
        config.source = defaultTopic;
        if (auto override = app.getConfig().getEndpointParam(id, "name")) {
            config.source = *override;
        }
        return config;
    }
}

namespace phds_gegi_driver {
    PhdsGegiDriver::PhdsGegiDriver(psm::Application &app,
                                   const cxxopts::ParseResult &args,
                                   std::shared_ptr<psm::Connection> connection)
            : tcp_event_reader_(std::bind(&PhdsGegiDriver::process1SiteEvent, this, std::placeholders::_1),
                                std::bind(&PhdsGegiDriver::process2SiteEvent, this, std::placeholders::_1)) {
        PSM_INFO("Starting Prism PHDS GeGi driver");

        const std::string gegi_ip = args["gegi-ip"].as<std::string>();
        const std::string gegi_port = args["gegi-port"].as<std::string>();
        detector_frame_ = args["detector-frame"].as<std::string>();
        detector_info_cache_max_age_sec_ = args["detector-info-cache-max-age-sec"].as<double>();
        idle_poll_interval_sec_ = args["idle-poll-interval-sec"].as<double>();
        busy_poll_interval_sec_ = args["busy-poll-interval-sec"].as<double>();

        // Energy-scale correction applied to every parsed site energy (keV):
        // corrected = c0 + c1*E + c2*E^2. Defaults 0.0/1.0/0.0 = no correction
        // (ported from upstream's energy_cal_c0/c1/c2 ROS params). CLI flags
        // take precedence; config/gegi_driver.yaml's top-level energy_cal_c0/
        // c1/c2 keys (read via Configuration::get(), the same mechanism used
        // for endpoint overrides) act as the config-file default, following
        // the same override pattern as detector-frame above.
        double energy_cal_c0 = args["energy-cal-c0"].as<double>();
        double energy_cal_c1 = args["energy-cal-c1"].as<double>();
        double energy_cal_c2 = args["energy-cal-c2"].as<double>();
        if (auto v = app.getConfig().get("energy_cal_c0")) energy_cal_c0 = std::stod(*v);
        if (auto v = app.getConfig().get("energy_cal_c1")) energy_cal_c1 = std::stod(*v);
        if (auto v = app.getConfig().get("energy_cal_c2")) energy_cal_c2 = std::stod(*v);
        tcp_event_reader_.setEnergyCorrection(energy_cal_c0, energy_cal_c1, energy_cal_c2);
        PSM_INFO("Energy calibration correction: c0={} c1={} c2={}",
                 energy_cal_c0, energy_cal_c1, energy_cal_c2);

        // Senders (decorated: void send(), PrismCore integration built-in).
        event_sender_ = app.createTextSender(
            args, connection, makeSenderConfig(app, "compton_event", "gegi.driver.compton_event"));
        energy_sender_ = app.createTextSender(
            args, connection, makeSenderConfig(app, "energy_deposit", "gegi.driver.energy_deposit"));
        singles_energy_sender_ = app.createTextSender(
            args, connection, makeSenderConfig(app, "energy_deposit_singles", "gegi.driver.energy_deposit_singles"));
        run_info_sender_ = app.createTextSender(
            args, connection, makeSenderConfig(app, "run_info", "gegi.detector.run_info"));
        detector_info_sender_ = app.createTextSender(
            args, connection, makeSenderConfig(app, "detector_info", "gegi.detector.detector_info"));
        dead_time_sender_ = app.createTextSender(
            args, connection, makeSenderConfig(app, "dead_time_percent", "gegi.detector.dead_time_percent"));

        try {
            tcp_event_reader_.connect(gegi_ip, gegi_port);
            tcp_event_reader_.startListening();
        } catch (std::exception &error) {
            PSM_ERROR("PhdsGegiDriver: Could not start TCP IP socket: {}", error.what());
            exit(1);
        }

        // Command channel (replaces the 5 detector/* ROS services).
        auto commandReceiverConfig = makeReceiverConfig(app, "command", "gegi.detector.command");
        auto commandResultSenderConfig = makeSenderConfig(
            app, "command_result", "gegi.detector.command_result");
        auto commandReceiver = app.createRawTextReceiver(args, connection, commandReceiverConfig);
        auto commandResultSender = app.createTextSender(
            args, connection, commandResultSenderConfig);

        command_server_ = std::make_unique<command_channel::CommandServer>(commandReceiver, commandResultSender);
        command_server_->on("start_acquisition",
                             [this](const nlohmann::json &p) { return handleStartAcquisition(p); });
        command_server_->on("stop_acquisition",
                             [this](const nlohmann::json &p) { return handleStopAcquisition(p); });
        command_server_->on("clear_data",
                             [this](const nlohmann::json &p) { return handleClearData(p); });
        command_server_->on("clear_data_and_windows",
                             [this](const nlohmann::json &p) { return handleClearDataAndWindows(p); });
        command_server_->on("toggle_bias_mode",
                             [this](const nlohmann::json &p) { return handleToggleBiasMode(p); });
        command_server_->on("start_timed_acquisition",
                             [this](const nlohmann::json &p) { return handleStartTimedAcquisition(p); });
        command_server_->start();
        PSM_INFO("Detector command channel ready on '{}' / '{}'",
                 commandReceiverConfig.source, commandResultSenderConfig.destination);

        // Background state-publishing loop (run_info / detector_info).
        state_thread_running_ = true;
        state_thread_ = std::thread(&PhdsGegiDriver::stateLoop, this);

        PSM_INFO("Prism PHDS GeGi driver initialised");
    }

    PhdsGegiDriver::~PhdsGegiDriver() {
        state_thread_running_ = false;
        if (state_thread_.joinable()) {
            state_thread_.join();
        }
        if (command_server_) {
            command_server_->stop();
        }
    }

    void PhdsGegiDriver::process1SiteEvent(const compton_events::Event1Site &event) {
        double energy = event.compton_interaction.avg_energy;
        publishEnergy(energy);
        publishSinglesEnergy(energy);
    }

    void PhdsGegiDriver::process2SiteEvent(const compton_events::Event2Site &event) {
        messages::ComptonEvent compton_event;

        compton_event.stamp = nowSeconds();
        compton_event.frame_id = detector_frame_;
        compton_event.seq = event_sequence_id_;
        ++event_sequence_id_;

        constexpr static double MM_TO_M = 0.001;
        constexpr static double MEV_TO_KEV = 1.0;

        compton_event.energy_kev_1 = event.compton_interaction_1.avg_energy * MEV_TO_KEV;
        compton_event.energy_kev_2 = event.compton_interaction_2.avg_energy * MEV_TO_KEV;

        // Keep the same right-handed frame convention the ROS driver used
        // (previously described as ROS REP-105): x=z, y=x, z=-y (mm -> m).
        compton_event.reading_location_1.x = event.compton_interaction_1.site.z * MM_TO_M;
        compton_event.reading_location_1.y = event.compton_interaction_1.site.x * MM_TO_M;
        compton_event.reading_location_1.z = -event.compton_interaction_1.site.y * MM_TO_M;

        compton_event.reading_location_2.x = event.compton_interaction_2.site.z * MM_TO_M;
        compton_event.reading_location_2.y = event.compton_interaction_2.site.x * MM_TO_M;
        compton_event.reading_location_2.z = -event.compton_interaction_2.site.y * MM_TO_M;

        compton_event.cone_angle = event.compton_angle;
        compton_event.cone_angle_uncertainty = event.delta_compton_angle;

        nlohmann::json j = compton_event;
        event_sender_->send(j.dump());

        // Publish summed energy (full-energy deposition) for spectrum
        publishEnergy(compton_event.energy_kev_1 + compton_event.energy_kev_2);
    }

    void PhdsGegiDriver::publishEnergy(double energy_kev) {
        nlohmann::json j = psm::types::DoubleValue{energy_kev};
        energy_sender_->send(j.dump());
    }

    void PhdsGegiDriver::publishSinglesEnergy(double energy_kev) {
        nlohmann::json j = psm::types::DoubleValue{energy_kev};
        singles_energy_sender_->send(j.dump());
    }

    command_channel::CommandResult PhdsGegiDriver::handleStartAcquisition(const nlohmann::json & /*params*/) {
        PSM_INFO("Received start_acquisition command");
        command_channel::CommandResult result;
        result.success = tcp_event_reader_.sendStartAcquisition();
        result.message = result.success ? "Start acquisition command sent" : "Failed to send start acquisition command";
        if (result.success) acquisition_active_ = true;
        return result;
    }

    command_channel::CommandResult PhdsGegiDriver::handleStopAcquisition(const nlohmann::json & /*params*/) {
        PSM_INFO("Received stop_acquisition command");
        command_channel::CommandResult result;
        result.success = tcp_event_reader_.sendStopAcquisition();
        result.message = result.success ? "Stop acquisition command sent" : "Failed to send stop acquisition command";
        if (result.success) acquisition_active_ = false;
        return result;
    }

    command_channel::CommandResult PhdsGegiDriver::handleClearData(const nlohmann::json & /*params*/) {
        PSM_INFO("Received clear_data command");
        command_channel::CommandResult result;
        result.success = tcp_event_reader_.sendClearData();
        result.message = result.success ? "Clear data command sent" : "Failed to send clear data command";
        return result;
    }

    command_channel::CommandResult PhdsGegiDriver::handleClearDataAndWindows(const nlohmann::json & /*params*/) {
        PSM_INFO("Received clear_data_and_windows command");
        command_channel::CommandResult result;
        result.success = tcp_event_reader_.sendClearDataAndWindows();
        result.message = result.success
            ? "Clear data and windows command sent"
            : "Failed to send clear data and windows command";
        return result;
    }

    command_channel::CommandResult PhdsGegiDriver::handleToggleBiasMode(const nlohmann::json & /*params*/) {
        PSM_INFO("Received toggle_bias_mode command");
        command_channel::CommandResult result;
        result.success = tcp_event_reader_.sendToggleBiasMode();
        result.message = result.success ? "Bias toggle command sent" : "Failed to send bias toggle command";
        return result;
    }

    command_channel::CommandResult PhdsGegiDriver::handleStartTimedAcquisition(const nlohmann::json &params) {
        command_channel::CommandResult result;

        if (!params.contains("duration_minutes")) {
            result.success = false;
            result.message = "Missing required parameter: duration_minutes";
            return result;
        }
        const int duration_minutes = params.at("duration_minutes").get<int>();
        PSM_INFO("Received start_timed_acquisition command: {} minutes", duration_minutes);

        // Map duration to GeGI preset command character (manual: '1'=5min,
        // '2'=10, '3'=15, '4'=20, '5'=25, '6'=30, '7'=45, '8'=60 min).
        char cmd;
        switch (duration_minutes) {
            case 5:  cmd = '1'; break;
            case 10: cmd = '2'; break;
            case 15: cmd = '3'; break;
            case 20: cmd = '4'; break;
            case 25: cmd = '5'; break;
            case 30: cmd = '6'; break;
            case 45: cmd = '7'; break;
            case 60: cmd = '8'; break;
            default:
                result.success = false;
                result.message = "Unsupported duration. Valid values: 5, 10, 15, 20, 25, 30, 45, 60 minutes";
                return result;
        }

        result.success = tcp_event_reader_.sendTimedAcquisition(cmd);
        result.message = result.success
            ? std::to_string(duration_minutes) + "-minute acquisition started"
            : "Failed to send timed acquisition command";
        if (result.success) acquisition_active_ = true;
        return result;
    }

    messages::RunInfo PhdsGegiDriver::queryRunInfo() {
        messages::RunInfo out;
        auto run_info = tcp_event_reader_.getRunInfo();
        out.real_time_sec = run_info.real_time_sec;
        out.live_time_sec = run_info.live_time_sec;
        const double raw_dead_time_percent = run_info.dead_time_percent;
        out.dead_time_percent = raw_dead_time_percent;
        out.count_rate_hz = run_info.count_rate_hz;

        const bool finite = std::isfinite(out.real_time_sec)
                    && std::isfinite(out.live_time_sec)
                && std::isfinite(raw_dead_time_percent)
                    && std::isfinite(out.count_rate_hz);
        const bool non_negative = out.real_time_sec >= 0.0
                      && out.live_time_sec >= 0.0
                  && raw_dead_time_percent >= 0.0
                  && raw_dead_time_percent <= 100.0
                      && out.count_rate_hz >= 0.0;
        const bool idle_zero = out.real_time_sec == 0.0
                       && out.live_time_sec == 0.0
                   && raw_dead_time_percent == 0.0
                       && out.count_rate_hz == 0.0;
        const bool timing_consistent = out.real_time_sec > 0.0
                           && out.live_time_sec <= out.real_time_sec + 1e-6;
        const bool can_derive_dt = timing_consistent && out.real_time_sec > 1e-9;
        const double derived_dead_time_percent = can_derive_dt
            ? (100.0 * (1.0 - out.live_time_sec / out.real_time_sec))
            : std::numeric_limits<double>::quiet_NaN();
        const bool derived_dead_time_valid = std::isfinite(derived_dead_time_percent)
            && derived_dead_time_percent >= 0.0
            && derived_dead_time_percent <= 100.0;
        // Guard against denormal/garbage live-time values observed from mixed socket traffic.
        const double live_fraction = timing_consistent && out.real_time_sec > 1e-9
            ? (out.live_time_sec / out.real_time_sec)
            : 0.0;
        const bool live_time_plausible = !timing_consistent
            || (live_fraction >= 0.05);
        const bool raw_dead_time_consistent = !can_derive_dt
            || (std::fabs(raw_dead_time_percent - derived_dead_time_percent) <= 2.0);

        bool used_derived_dead_time = false;
        if (derived_dead_time_valid && !raw_dead_time_consistent) {
            out.dead_time_percent = derived_dead_time_percent;
            used_derived_dead_time = true;
            PSM_WARN("Run-info dead time sanitized from real/live times. raw={} derived={}",
                     raw_dead_time_percent, derived_dead_time_percent);
        }

        const bool valid = finite && non_negative
            && (idle_zero || (timing_consistent
                      && live_time_plausible
                      && (raw_dead_time_consistent || derived_dead_time_valid)));

        bool temporally_consistent = true;
        if (valid && !idle_zero) {
            const double now = nowSeconds();
            std::lock_guard<std::mutex> history_lock(run_info_history_mutex_);
            if (has_last_valid_run_info_) {
                const double wall_dt = now - last_run_info_stamp_;
                if (wall_dt > 0.0) {
                    const double delta_real = out.real_time_sec - last_run_info_real_time_sec_;
                    const double delta_live = out.live_time_sec - last_run_info_live_time_sec_;
                    const bool monotonic = delta_real >= -0.5 && delta_live >= -0.5;
                    const double max_growth = wall_dt * 3.0 + 5.0;
                    const bool growth_reasonable = delta_real <= max_growth && delta_live <= max_growth;
                    // NOTE: a stalled live-time while real-time advances is NOT a stale
                    // frame - a truly latched frame would show delta_real ~ 0 too. Real
                    // advancing while live stalls is exactly what GENUINE high dead-time
                    // looks like, so we accept it. Monotonicity + bounded growth still
                    // reject garbage/replayed frames.
                    temporally_consistent = monotonic && growth_reasonable;
                }
            }

            if (temporally_consistent) {
                has_last_valid_run_info_ = true;
                last_run_info_real_time_sec_ = out.real_time_sec;
                last_run_info_live_time_sec_ = out.live_time_sec;
                last_run_info_stamp_ = now;
            }
        }

        const bool final_valid = valid && temporally_consistent;
        out.valid = final_valid;
        out.stamp = nowSeconds();
        if (!final_valid) {
            out.real_time_sec = std::numeric_limits<double>::quiet_NaN();
            out.live_time_sec = std::numeric_limits<double>::quiet_NaN();
            out.dead_time_percent = std::numeric_limits<double>::quiet_NaN();
            out.count_rate_hz = std::numeric_limits<double>::quiet_NaN();
            out.message = "Run info response invalid or timed out";
        } else if (used_derived_dead_time) {
            out.message = "Run info retrieved (dead time sanitized from real/live)";
        } else {
            out.message = "Run info retrieved";
        }
        return out;
    }

    messages::DetectorInfo PhdsGegiDriver::queryDetectorInfo() {
        messages::DetectorInfo out;
        auto detector_info = tcp_event_reader_.getDetectorInfo();
        out.serial_number = detector_info.serial_number;
        out.detector_temp_kelvin = detector_info.detector_temp_kelvin;
        out.detector_bias_status = detector_info.detector_bias_status;
        out.line_power_status = detector_info.line_power_status;
        out.batt1_percent = detector_info.batt1_percent;
        out.batt2_percent = detector_info.batt2_percent;

        const bool valid = !out.serial_number.empty()
                           && std::isfinite(out.detector_temp_kelvin)
                           && out.detector_temp_kelvin >= -50.0
                           && out.detector_temp_kelvin <= 200.0
                           && (out.detector_bias_status == 0 || out.detector_bias_status == 1)
                           && (out.line_power_status == 0 || out.line_power_status == 1)
                           && out.batt1_percent >= 0
                           && out.batt1_percent <= 100
                           && out.batt2_percent >= 0
                           && out.batt2_percent <= 100;

        if (valid) {
            {
                std::lock_guard<std::mutex> lock(detector_info_cache_mutex_);
                has_last_valid_detector_info_ = true;
                last_detector_info_ = detector_info;
                last_detector_info_stamp_ = nowSeconds();
            }
            out.valid = true;
            out.cached = false;
            out.message = "Detector info retrieved";
            out.stamp = nowSeconds();
            return out;
        }

        bool used_cached = false;
        {
            std::lock_guard<std::mutex> lock(detector_info_cache_mutex_);
            if (has_last_valid_detector_info_) {
                const double age_s = nowSeconds() - last_detector_info_stamp_;
                if (age_s >= 0.0 && age_s <= detector_info_cache_max_age_sec_) {
                    out.serial_number = last_detector_info_.serial_number;
                    out.detector_temp_kelvin = last_detector_info_.detector_temp_kelvin;
                    out.detector_bias_status = last_detector_info_.detector_bias_status;
                    out.line_power_status = last_detector_info_.line_power_status;
                    out.batt1_percent = last_detector_info_.batt1_percent;
                    out.batt2_percent = last_detector_info_.batt2_percent;
                    used_cached = true;
                }
            }
        }

        out.stamp = nowSeconds();
        if (used_cached) {
            out.valid = true;
            out.cached = true;
            out.message = "Detector info retrieved (cached last valid sample)";
        } else {
            out.valid = false;
            out.cached = false;
            out.message = "Detector info response invalid or timed out";
        }
        return out;
    }

    void PhdsGegiDriver::stateLoop() {
        while (state_thread_running_) {
            const messages::RunInfo run_info = queryRunInfo();
            nlohmann::json run_info_json = run_info;
            run_info_sender_->send(run_info_json.dump());

            if (run_info.valid) {
                nlohmann::json dead_time_json = psm::types::DoubleValue{run_info.dead_time_percent};
                dead_time_sender_->send(dead_time_json.dump());
            }

            const messages::DetectorInfo detector_info = queryDetectorInfo();
            nlohmann::json detector_info_json = detector_info;
            detector_info_sender_->send(detector_info_json.dump());

            const double interval = acquisition_active_ ? busy_poll_interval_sec_ : idle_poll_interval_sec_;
            const auto sleep_until = std::chrono::steady_clock::now()
                + std::chrono::milliseconds(static_cast<int64_t>(interval * 1000.0));
            while (state_thread_running_ && std::chrono::steady_clock::now() < sleep_until) {
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
        }
    }
}
