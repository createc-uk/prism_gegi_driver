//
// Prism message schemas for phds_gegi_driver.
//
// These are the plain-old-data payloads carried as JSON text over Prism
// TextSender/TextReceiver endpoints. They replace the old ROS message types
// (radiation_detector_msgs/ComptonEvent, radiation_detector_msgs/Spectrum,
// and the custom .srv response fields). Every field name here is part of the
// wire contract shared with the Python nodes (see
// src/phds_gegi_driver/prism_messages.py) and with any external consumer --
// keep the two in lockstep.
//
// All timestamps are seconds since the Unix epoch (double), since there is
// no ROS clock/Time type once ROS is removed.

#ifndef PHDS_GEGI_DRIVER_MESSAGES_HPP
#define PHDS_GEGI_DRIVER_MESSAGES_HPP

#include <nlohmann/json.hpp>

#include <chrono>
#include <cstdint>
#include <string>
#include <vector>

namespace phds_gegi_driver::messages {

    // Seconds-since-epoch helper used for every "stamp" field below.
    inline double nowSeconds() {
        return std::chrono::duration<double>(
                   std::chrono::system_clock::now().time_since_epoch())
            .count();
    }

    struct Vec3 {
        double x = 0.0;
        double y = 0.0;
        double z = 0.0;
    };

    inline void to_json(nlohmann::json &j, const Vec3 &v) {
        j = nlohmann::json{{"x", v.x}, {"y", v.y}, {"z", v.z}};
    }

    inline void from_json(const nlohmann::json &j, Vec3 &v) {
        j.at("x").get_to(v.x);
        j.at("y").get_to(v.y);
        j.at("z").get_to(v.z);
    }

    // Replaces radiation_detector_msgs/ComptonEvent. Published on
    // gegi.driver.compton_event, one message per 2-site Compton event.
    struct ComptonEvent {
        double stamp = 0.0;
        std::string frame_id;
        uint32_t seq = 0;
        double energy_kev_1 = 0.0;
        Vec3 reading_location_1;
        double energy_kev_2 = 0.0;
        Vec3 reading_location_2;
        double cone_angle = 0.0;
        double cone_angle_uncertainty = 0.0;
    };

    inline void to_json(nlohmann::json &j, const ComptonEvent &e) {
        j = nlohmann::json{
            {"dataType", "ComptonEvent"},
            {"stamp", e.stamp},
            {"frame_id", e.frame_id},
            {"seq", e.seq},
            {"energy_kev_1", e.energy_kev_1},
            {"reading_location_1", e.reading_location_1},
            {"energy_kev_2", e.energy_kev_2},
            {"reading_location_2", e.reading_location_2},
            {"cone_angle", e.cone_angle},
            {"cone_angle_uncertainty", e.cone_angle_uncertainty},
        };
    }

    inline void from_json(const nlohmann::json &j, ComptonEvent &e) {
        j.at("stamp").get_to(e.stamp);
        j.at("frame_id").get_to(e.frame_id);
        j.at("seq").get_to(e.seq);
        j.at("energy_kev_1").get_to(e.energy_kev_1);
        j.at("reading_location_1").get_to(e.reading_location_1);
        j.at("energy_kev_2").get_to(e.energy_kev_2);
        j.at("reading_location_2").get_to(e.reading_location_2);
        j.at("cone_angle").get_to(e.cone_angle);
        j.at("cone_angle_uncertainty").get_to(e.cone_angle_uncertainty);
    }

    // Replaces radiation_detector_msgs/Spectrum. Published on
    // gegi.spectrum.histogram / gegi.spectrum_singles.histogram.
    // NOTE: `spectrum` carries per-interval DELTA counts, not a running
    // total -- unchanged contract from the ROS version.
    struct Spectrum {
        double stamp = 0.0;
        std::string frame_id;
        uint32_t seq = 0;
        double end_time = 0.0;
        uint32_t real_time_ms = 0;
        uint32_t dead_time_ms = 0;
        uint32_t total_count = 0;
        std::vector<uint32_t> spectrum;
    };

    inline void to_json(nlohmann::json &j, const Spectrum &s) {
        j = nlohmann::json{
            {"dataType", "Spectrum"},
            {"stamp", s.stamp},
            {"frame_id", s.frame_id},
            {"seq", s.seq},
            {"end_time", s.end_time},
            {"real_time_ms", s.real_time_ms},
            {"dead_time_ms", s.dead_time_ms},
            {"total_count", s.total_count},
            {"spectrum", s.spectrum},
        };
    }

    inline void from_json(const nlohmann::json &j, Spectrum &s) {
        j.at("stamp").get_to(s.stamp);
        j.at("frame_id").get_to(s.frame_id);
        j.at("seq").get_to(s.seq);
        j.at("end_time").get_to(s.end_time);
        j.at("real_time_ms").get_to(s.real_time_ms);
        j.at("dead_time_ms").get_to(s.dead_time_ms);
        j.at("total_count").get_to(s.total_count);
        j.at("spectrum").get_to(s.spectrum);
    }

    // Replaces the phds_gegi_driver/GetRunInfo service response. Published
    // periodically on gegi.detector.run_info (2s idle / 30s busy poll).
    struct RunInfo {
        bool valid = false;
        double real_time_sec = 0.0;
        double live_time_sec = 0.0;
        double dead_time_percent = 0.0;
        double count_rate_hz = 0.0;
        std::string message;
        double stamp = 0.0;
    };

    inline void to_json(nlohmann::json &j, const RunInfo &r) {
        j = nlohmann::json{
            {"dataType", "RunInfo"},
            {"valid", r.valid},
            {"real_time_sec", r.real_time_sec},
            {"live_time_sec", r.live_time_sec},
            {"dead_time_percent", r.dead_time_percent},
            {"count_rate_hz", r.count_rate_hz},
            {"message", r.message},
            {"stamp", r.stamp},
        };
    }

    inline void from_json(const nlohmann::json &j, RunInfo &r) {
        j.at("valid").get_to(r.valid);
        j.at("real_time_sec").get_to(r.real_time_sec);
        j.at("live_time_sec").get_to(r.live_time_sec);
        j.at("dead_time_percent").get_to(r.dead_time_percent);
        j.at("count_rate_hz").get_to(r.count_rate_hz);
        j.at("message").get_to(r.message);
        j.at("stamp").get_to(r.stamp);
    }

    // Replaces the phds_gegi_driver/GetDetectorInfo service response.
    // Published periodically on gegi.detector.detector_info.
    struct DetectorInfo {
        bool valid = false;
        std::string serial_number;
        double detector_temp_kelvin = 0.0;
        int detector_bias_status = 0;
        int line_power_status = 0;
        int batt1_percent = 0;
        int batt2_percent = 0;
        std::string message;
        double stamp = 0.0;
        bool cached = false;
    };

    inline void to_json(nlohmann::json &j, const DetectorInfo &d) {
        j = nlohmann::json{
            {"dataType", "DetectorInfo"},
            {"valid", d.valid},
            {"serial_number", d.serial_number},
            {"detector_temp_kelvin", d.detector_temp_kelvin},
            {"detector_bias_status", d.detector_bias_status},
            {"line_power_status", d.line_power_status},
            {"batt1_percent", d.batt1_percent},
            {"batt2_percent", d.batt2_percent},
            {"message", d.message},
            {"stamp", d.stamp},
            {"cached", d.cached},
        };
    }

    inline void from_json(const nlohmann::json &j, DetectorInfo &d) {
        j.at("valid").get_to(d.valid);
        j.at("serial_number").get_to(d.serial_number);
        j.at("detector_temp_kelvin").get_to(d.detector_temp_kelvin);
        j.at("detector_bias_status").get_to(d.detector_bias_status);
        j.at("line_power_status").get_to(d.line_power_status);
        j.at("batt1_percent").get_to(d.batt1_percent);
        j.at("batt2_percent").get_to(d.batt2_percent);
        j.at("message").get_to(d.message);
        j.at("stamp").get_to(d.stamp);
        j.at("cached").get_to(d.cached);
    }

} // namespace phds_gegi_driver::messages

#endif // PHDS_GEGI_DRIVER_MESSAGES_HPP
