//
// Created by createc on 10/09/2020.
//

#include <phds_gegi_driver/phds_gegi_driver.hpp>

#include <cmath>
#include <iomanip>
#include <limits>
#include <sstream>

namespace { // Anon
    template<typename T>
    void getParam(const ros::NodeHandle& node, const std::string& name, T& value)
    {
        if(!node.getParam(name, value))
            ROS_WARN_STREAM("PhdsGegiDriver: Failed to get \"" << name << "\" parameter, using default value: " << value);
    }
}

namespace phds_gegi_driver {
    PhdsGegiDriver::PhdsGegiDriver(ros::NodeHandle& node, ros::NodeHandle& node_private)
            : tcp_event_reader_(std::bind(&PhdsGegiDriver::process1SiteEvent, this, std::placeholders::_1),
                                std::bind(&PhdsGegiDriver::process2SiteEvent, this, std::placeholders::_1))
    {
        ROS_INFO("Starting ROS PHDS GeGI driver");

        ros::NodeHandle& n = node;
        ros::NodeHandle& pn = node_private;

        std::string gegi_ip;
        std::string gegi_port = "27015";
        std::string event_topic = "/compton_event";
        std::string energy_topic = "/energy_deposit";
        std::string singles_energy_topic = "/energy_deposit_singles";
        std::string health_topic = "/gegi/health";
        double health_period_sec = 1.0;

        getParam(pn, "gegi_ip", gegi_ip);
        getParam(pn, "gegi_port", gegi_port);
        getParam(pn, "event_topic", event_topic);
        getParam(pn, "energy_topic", energy_topic);
        getParam(pn, "singles_energy_topic", singles_energy_topic);
        getParam(pn, "health_topic", health_topic);
        getParam(pn, "health_period_sec", health_period_sec);
        getParam(pn, "detector_frame", detector_frame_);
        getParam(pn, "detector_info_cache_max_age_sec", detector_info_cache_max_age_sec_);

        // Energy-scale correction (E_true = c0 + c1*E + c2*E^2) applied to
        // every parsed site energy. The detector's onboard event-energy
        // calibration reads ~0.057% high (+1.0 keV at 1332 keV); coefficients
        // fitted from a 10-line Cs/Co/Eu run (tools/fit_energy_cal.py).
        // Defaults are identity - the launch file supplies the fitted values.
        double energy_cal_c0 = 0.0;
        double energy_cal_c1 = 1.0;
        double energy_cal_c2 = 0.0;
        getParam(pn, "energy_cal_c0", energy_cal_c0);
        getParam(pn, "energy_cal_c1", energy_cal_c1);
        getParam(pn, "energy_cal_c2", energy_cal_c2);
        tcp_event_reader_.setEnergyCorrection(energy_cal_c0, energy_cal_c1,
                                              energy_cal_c2);

        // Deep publisher queues so bursts of events are never dropped between
        // the reader thread and slower subscribers (spectrum/heatmap/recorder).
        // Small Float64/ComptonEvent messages make a large queue cheap.
        constexpr static int OUTPUT_BUFFER_SIZE = 10000;
        event_publisher_ = n.advertise<radiation_detector_msgs::ComptonEvent>(event_topic, OUTPUT_BUFFER_SIZE);
        energy_publisher_ = n.advertise<std_msgs::Float64>(energy_topic, OUTPUT_BUFFER_SIZE);
        singles_energy_publisher_ = n.advertise<std_msgs::Float64>(singles_energy_topic, OUTPUT_BUFFER_SIZE);

        // Latched so a late subscriber (e.g. PRISM attaching after launch)
        // immediately receives the most recent health sample.
        health_publisher_ = n.advertise<std_msgs::String>(health_topic, 2, true);

        // Initial connection failure is no longer fatal: the reader's monitor
        // thread keeps retrying with bounded backoff, and the health heartbeat
        // reports connected=false in the meantime. Services stay available (they
        // return failure while disconnected).
        try {
            tcp_event_reader_.connect(gegi_ip, gegi_port);
        } catch (std::exception &error) {
            ROS_ERROR_STREAM("PhdsGegiDriver: initial GeGI connection failed ("
                             << error.what() << ") - retrying in background");
        }
        tcp_event_reader_.startListening();

        health_timer_ = n.createTimer(ros::Duration(health_period_sec),
                                      &PhdsGegiDriver::publishHealth, this);

        // Advertise remote control services
        start_acq_service_ = n.advertiseService("detector/start_acquisition",
                                                 &PhdsGegiDriver::handleStartAcquisition, this);
        stop_acq_service_ = n.advertiseService("detector/stop_acquisition",
                                                &PhdsGegiDriver::handleStopAcquisition, this);
        clear_data_service_ = n.advertiseService("detector/clear_data",
                                                  &PhdsGegiDriver::handleClearData, this);
        clear_data_windows_service_ = n.advertiseService("detector/clear_data_and_windows",
                                &PhdsGegiDriver::handleClearDataAndWindows, this);
        run_info_service_ = n.advertiseService("detector/get_run_info",
                                                &PhdsGegiDriver::handleGetRunInfo, this);
        detector_info_service_ = n.advertiseService("detector/get_detector_info",
                                &PhdsGegiDriver::handleGetDetectorInfo, this);
        toggle_bias_service_ = n.advertiseService("detector/toggle_bias_mode",
                              &PhdsGegiDriver::handleToggleBiasMode, this);
        timed_acq_service_ = n.advertiseService("detector/start_timed_acquisition",
                             &PhdsGegiDriver::handleStartTimedAcquisition, this);
        ROS_INFO("Detector remote control services advertised");

        ROS_INFO("ROS PHDS GeGI driver initialised");
    }

    void PhdsGegiDriver::process1SiteEvent(const compton_events::Event1Site &event) {
        double energy = event.compton_interaction.avg_energy;
        publishEnergy(energy);
        publishSinglesEnergy(energy);
    }

    void PhdsGegiDriver::process2SiteEvent(const compton_events::Event2Site &event) {

        radiation_detector_msgs::ComptonEvent ros_event;

        ros_event.header.stamp = ros::Time::now();
        ros_event.header.frame_id = detector_frame_;
        ros_event.header.seq = event_sequence_id_;
        ++event_sequence_id_;

        constexpr static double MM_TO_M = 0.001;
        constexpr static double MEV_TO_KEV = 1.0;

        ros_event.energy_kev_1 = event.compton_interaction_1.avg_energy * MEV_TO_KEV;
        ros_event.energy_kev_2 = event.compton_interaction_2.avg_energy * MEV_TO_KEV;

        // Switching coordinates to ROS frame (REP-105)
        ros_event.reading_location_1.x = event.compton_interaction_1.site.z * MM_TO_M;
        ros_event.reading_location_1.y = event.compton_interaction_1.site.x * MM_TO_M;
        ros_event.reading_location_1.z = -event.compton_interaction_1.site.y * MM_TO_M;

        ros_event.reading_location_2.x = event.compton_interaction_2.site.z * MM_TO_M;
        ros_event.reading_location_2.y = event.compton_interaction_2.site.x * MM_TO_M;
        ros_event.reading_location_2.z = -event.compton_interaction_2.site.y * MM_TO_M;

        ros_event.cone_angle = event.compton_angle;
        ros_event.cone_angle_uncertainty = event.delta_compton_angle;

        event_publisher_.publish(ros_event);

        // Publish summed energy (full-energy deposition) for spectrum
        publishEnergy(ros_event.energy_kev_1 + ros_event.energy_kev_2);
    }

    void PhdsGegiDriver::publishEnergy(double energy_kev) {
        std_msgs::Float64 msg;
        msg.data = energy_kev;
        energy_publisher_.publish(msg);
    }

    void PhdsGegiDriver::publishSinglesEnergy(double energy_kev) {
        std_msgs::Float64 msg;
        msg.data = energy_kev;
        singles_energy_publisher_.publish(msg);
    }

    void PhdsGegiDriver::publishHealth(const ros::TimerEvent &) {
        const ros::Time now = ros::Time::now();
        const std::uint64_t events_total = tcp_event_reader_.eventsTotal();

        double event_rate_hz = 0.0;
        if (!last_health_stamp_.isZero()) {
            const double dt = (now - last_health_stamp_).toSec();
            if (dt > 0.0) {
                event_rate_hz =
                        static_cast<double>(events_total - last_health_events_total_) / dt;
            }
        }
        last_health_stamp_ = now;
        last_health_events_total_ = events_total;

        const double last_event_age = tcp_event_reader_.secondsSinceLastEvent();

        std::ostringstream json;
        json.setf(std::ios::fixed);
        json << "{"
             << "\"schema_version\":1,"
             << "\"stamp\":" << std::setprecision(3) << now.toSec() << ","
             << "\"connected\":" << (tcp_event_reader_.isConnected() ? "true" : "false") << ","
             << "\"events_total\":" << events_total << ","
             << "\"event_rate_hz\":" << std::setprecision(2) << event_rate_hz << ","
             << "\"last_event_age_sec\":" << std::setprecision(2) << last_event_age << ","
             << "\"reconnect_count\":" << tcp_event_reader_.reconnectCount()
             << "}";

        std_msgs::String msg;
        msg.data = json.str();
        health_publisher_.publish(msg);
    }

    bool PhdsGegiDriver::handleStartAcquisition(phds_gegi_driver::StartAcquisition::Request &req,
                                                phds_gegi_driver::StartAcquisition::Response &res) {
        ROS_INFO("Received start_acquisition request");
        res.success = tcp_event_reader_.sendStartAcquisition();
        res.message = res.success ? "Start acquisition command sent" : "Failed to send start acquisition command";
        return true;
    }

    bool PhdsGegiDriver::handleStopAcquisition(phds_gegi_driver::StopAcquisition::Request &req,
                                               phds_gegi_driver::StopAcquisition::Response &res) {
        ROS_INFO("Received stop_acquisition request");
        res.success = tcp_event_reader_.sendStopAcquisition();
        res.message = res.success ? "Stop acquisition command sent" : "Failed to send stop acquisition command";
        return true;
    }

    bool PhdsGegiDriver::handleClearData(phds_gegi_driver::ClearData::Request &req,
                                        phds_gegi_driver::ClearData::Response &res) {
        ROS_INFO("Received clear_data request");
        res.success = tcp_event_reader_.sendClearData();
        res.message = res.success ? "Clear data command sent" : "Failed to send clear data command";
        return true;
    }

    bool PhdsGegiDriver::handleClearDataAndWindows(phds_gegi_driver::ClearData::Request &req,
                                                   phds_gegi_driver::ClearData::Response &res) {
        ROS_INFO("Received clear_data_and_windows request (full onboard clear)");
        res.success = tcp_event_reader_.sendClearDataAndWindows();
        res.message = res.success ? "Clear data+windows command sent"
                                  : "Failed to send clear data+windows command";
        return true;
    }

    bool PhdsGegiDriver::handleGetRunInfo(phds_gegi_driver::GetRunInfo::Request &req,
                                         phds_gegi_driver::GetRunInfo::Response &res) {
        ROS_INFO("Received get_run_info request");
        auto run_info = tcp_event_reader_.getRunInfo();
        res.real_time_sec = run_info.real_time_sec;
        res.live_time_sec = run_info.live_time_sec;
        const double raw_dead_time_percent = run_info.dead_time_percent;
        res.dead_time_percent = raw_dead_time_percent;
        res.count_rate_hz = run_info.count_rate_hz;
        const bool finite = std::isfinite(res.real_time_sec)
                    && std::isfinite(res.live_time_sec)
                && std::isfinite(raw_dead_time_percent)
                    && std::isfinite(res.count_rate_hz);
        const bool non_negative = res.real_time_sec >= 0.0
                      && res.live_time_sec >= 0.0
                  && raw_dead_time_percent >= 0.0
                  && raw_dead_time_percent <= 100.0
                      && res.count_rate_hz >= 0.0;
        const bool idle_zero = res.real_time_sec == 0.0
                       && res.live_time_sec == 0.0
                   && raw_dead_time_percent == 0.0
                       && res.count_rate_hz == 0.0;
        const bool timing_consistent = res.real_time_sec > 0.0
                           && res.live_time_sec <= res.real_time_sec + 1e-6;
        const bool can_derive_dt = timing_consistent && res.real_time_sec > 1e-9;
        const double derived_dead_time_percent = can_derive_dt
            ? (100.0 * (1.0 - res.live_time_sec / res.real_time_sec))
            : std::numeric_limits<double>::quiet_NaN();
        const bool derived_dead_time_valid = std::isfinite(derived_dead_time_percent)
            && derived_dead_time_percent >= 0.0
            && derived_dead_time_percent <= 100.0;
        // Guard against denormal/garbage live-time values observed from mixed socket traffic.
        const double live_fraction = timing_consistent && res.real_time_sec > 1e-9
            ? (res.live_time_sec / res.real_time_sec)
            : 0.0;
        const bool live_time_plausible = !timing_consistent
            || (live_fraction >= 0.05);
        const bool raw_dead_time_consistent = !can_derive_dt
            || (std::fabs(raw_dead_time_percent - derived_dead_time_percent) <= 2.0);

        bool used_derived_dead_time = false;
        if (derived_dead_time_valid && !raw_dead_time_consistent) {
            res.dead_time_percent = derived_dead_time_percent;
            used_derived_dead_time = true;
            ROS_WARN_STREAM("Run-info dead time sanitized from real/live times. raw="
                    << raw_dead_time_percent << " derived=" << derived_dead_time_percent);
        }

        const bool valid = finite && non_negative
            && (idle_zero || (timing_consistent
                      && live_time_plausible
                      && (raw_dead_time_consistent || derived_dead_time_valid)));

        bool temporally_consistent = true;
        if (valid && !idle_zero) {
            const ros::Time now = ros::Time::now();
            std::lock_guard<std::mutex> history_lock(run_info_history_mutex_);
            if (has_last_valid_run_info_) {
                const double wall_dt = (now - last_run_info_stamp_).toSec();
                if (wall_dt > 0.0) {
                    const double delta_real = res.real_time_sec - last_run_info_real_time_sec_;
                    const double delta_live = res.live_time_sec - last_run_info_live_time_sec_;
                    const bool monotonic = delta_real >= -0.5 && delta_live >= -0.5;
                    const double max_growth = wall_dt * 3.0 + 5.0;
                    const bool growth_reasonable = delta_real <= max_growth && delta_live <= max_growth;
                    // NOTE: a stalled live-time while real-time advances is NOT a stale
                    // frame - a truly latched frame would show delta_real ~ 0 too. Real
                    // advancing while live stalls is exactly what GENUINE high dead-time
                    // looks like (the observable we want in Experiment C), so we accept
                    // it. Monotonicity + bounded growth still reject garbage/replayed
                    // frames. (Previously a stale_live_time heuristic wrongly rejected
                    // real high-dead-time samples.)
                    temporally_consistent = monotonic && growth_reasonable;
                }
            }

            if (temporally_consistent) {
                has_last_valid_run_info_ = true;
                last_run_info_real_time_sec_ = res.real_time_sec;
                last_run_info_live_time_sec_ = res.live_time_sec;
                last_run_info_stamp_ = now;
            }
        }

        const bool final_valid = valid && temporally_consistent;
        res.success = final_valid;
        if (!final_valid) {
            res.real_time_sec = std::numeric_limits<double>::quiet_NaN();
            res.live_time_sec = std::numeric_limits<double>::quiet_NaN();
            res.dead_time_percent = std::numeric_limits<double>::quiet_NaN();
            res.count_rate_hz = std::numeric_limits<double>::quiet_NaN();
            res.message = "Run info response invalid or timed out";
        } else if (used_derived_dead_time) {
            res.message = "Run info retrieved (dead time sanitized from real/live)";
        } else {
            res.message = "Run info retrieved";
        }
        return true;
    }

    bool PhdsGegiDriver::handleGetDetectorInfo(phds_gegi_driver::GetDetectorInfo::Request &req,
                                              phds_gegi_driver::GetDetectorInfo::Response &res) {
        ROS_INFO("Received get_detector_info request");
        auto detector_info = tcp_event_reader_.getDetectorInfo();
        res.serial_number = detector_info.serial_number;
        res.detector_temp_kelvin = detector_info.detector_temp_kelvin;
        res.detector_bias_status = detector_info.detector_bias_status;
        res.line_power_status = detector_info.line_power_status;
        res.batt1_percent = detector_info.batt1_percent;
        res.batt2_percent = detector_info.batt2_percent;
        const bool valid = !res.serial_number.empty()
                           && std::isfinite(res.detector_temp_kelvin)
                           && res.detector_temp_kelvin >= -50.0
                           && res.detector_temp_kelvin <= 200.0
                           && (res.detector_bias_status == 0 || res.detector_bias_status == 1)
                           && (res.line_power_status == 0 || res.line_power_status == 1)
                           && res.batt1_percent >= 0
                           && res.batt1_percent <= 100
                           && res.batt2_percent >= 0
                           && res.batt2_percent <= 100;

        if (valid) {
            {
                std::lock_guard<std::mutex> lock(detector_info_cache_mutex_);
                has_last_valid_detector_info_ = true;
                last_detector_info_ = detector_info;
                last_detector_info_stamp_ = ros::Time::now();
            }
            res.success = true;
            res.message = "Detector info retrieved";
            return true;
        }

        bool used_cached = false;
        {
            std::lock_guard<std::mutex> lock(detector_info_cache_mutex_);
            if (has_last_valid_detector_info_) {
                const double age_s = (ros::Time::now() - last_detector_info_stamp_).toSec();
                if (age_s >= 0.0 && age_s <= detector_info_cache_max_age_sec_) {
                    res.serial_number = last_detector_info_.serial_number;
                    res.detector_temp_kelvin = last_detector_info_.detector_temp_kelvin;
                    res.detector_bias_status = last_detector_info_.detector_bias_status;
                    res.line_power_status = last_detector_info_.line_power_status;
                    res.batt1_percent = last_detector_info_.batt1_percent;
                    res.batt2_percent = last_detector_info_.batt2_percent;
                    used_cached = true;
                }
            }
        }

        if (used_cached) {
            res.success = true;
            res.message = "Detector info retrieved (cached last valid sample)";
        } else {
            res.success = false;
            res.message = "Detector info response invalid or timed out";
        }
        return true;
    }

    bool PhdsGegiDriver::handleToggleBiasMode(phds_gegi_driver::ToggleBiasMode::Request &req,
                                             phds_gegi_driver::ToggleBiasMode::Response &res) {
        ROS_INFO("Received toggle_bias_mode request");
        res.success = tcp_event_reader_.sendToggleBiasMode();
        res.message = res.success ? "Bias toggle command sent" : "Failed to send bias toggle command";
        return true;
    }

    bool PhdsGegiDriver::handleStartTimedAcquisition(phds_gegi_driver::StartTimedAcquisition::Request &req,
                                                     phds_gegi_driver::StartTimedAcquisition::Response &res) {
        ROS_INFO_STREAM("Received start_timed_acquisition request: " << req.duration_minutes << " minutes");

        // Map duration to GeGI preset command character (manual: '1'=5min,
        // '2'=10, '3'=15, '4'=20, '5'=25, '6'=30, '7'=45, '8'=60 min).
        char cmd;
        switch (req.duration_minutes) {
            case 5:  cmd = '1'; break;
            case 10: cmd = '2'; break;
            case 15: cmd = '3'; break;
            case 20: cmd = '4'; break;
            case 25: cmd = '5'; break;
            case 30: cmd = '6'; break;
            case 45: cmd = '7'; break;
            case 60: cmd = '8'; break;
            default:
                res.success = false;
                res.message = "Unsupported duration. Valid values: 5, 10, 15, 20, 25, 30, 45, 60 minutes";
                return true;
        }

        res.success = tcp_event_reader_.sendTimedAcquisition(cmd);
        res.message = res.success
            ? std::to_string(req.duration_minutes) + "-minute acquisition started"
            : "Failed to send timed acquisition command";
        return true;
    }
}
