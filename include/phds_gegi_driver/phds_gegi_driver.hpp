//
// Created by createc on 10/09/2020.
// Migrated from ROS 1 to Prism messaging.
//

#ifndef PHDS_GEGI_DRIVER_PHDS_GEGI_DRIVER_HPP
#define PHDS_GEGI_DRIVER_PHDS_GEGI_DRIVER_HPP

#include <phds_gegi_driver/command_channel.hpp>
#include <phds_gegi_driver/data_structures.hpp>
#include <phds_gegi_driver/messages.hpp>
#include <socket_comms/tcp_event_reader.hpp>

#include <prism/core/connection.hpp>
#include <prism/core/sender_interface.hpp>
#include <prism/core/text_receiver.hpp>
#include <prism/utils/application.hpp>

#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

namespace phds_gegi_driver {

    // C++ driver node: owns the single TCP link to the GeGi detector
    // (unchanged, via socket_comms::TcpEventReader) and republishes
    // everything on the Prism-agnostic messaging layer. Every former ROS
    // service is now a pub/sub "command" topic (see command_channel.hpp);
    // every former on-demand query (get_run_info / get_detector_info) is a
    // periodically-republished state topic instead of a blocking call.
    class PhdsGegiDriver {
    public:
        PhdsGegiDriver(psm::Application &app,
                        const cxxopts::ParseResult &args,
                        std::shared_ptr<psm::Connection> connection);

        ~PhdsGegiDriver();

    private:
        void process1SiteEvent(const compton_events::Event1Site &event);
        void process2SiteEvent(const compton_events::Event2Site &event);

        void publishEnergy(double energy_kev);
        void publishSinglesEnergy(double energy_kev);

        // Command handlers (registered with CommandServer).
        command_channel::CommandResult handleStartAcquisition(const nlohmann::json &params);
        command_channel::CommandResult handleStopAcquisition(const nlohmann::json &params);
        command_channel::CommandResult handleClearData(const nlohmann::json &params);
        command_channel::CommandResult handleToggleBiasMode(const nlohmann::json &params);
        command_channel::CommandResult handleStartTimedAcquisition(const nlohmann::json &params);

        // Background poll loop publishing run_info / detector_info state
        // topics. Polls at idle_poll_interval_sec_ while nothing is running,
        // and at busy_poll_interval_sec_ during acquisition, to avoid
        // contending with the event stream on the single detector TCP link
        // (same rationale as the old ~30s ROS run_info_poll_s default).
        void stateLoop();

        messages::RunInfo queryRunInfo();
        messages::DetectorInfo queryDetectorInfo();

        unsigned int event_sequence_id_ = 0;

        socket_comms::TcpEventReader tcp_event_reader_;

        std::shared_ptr<psm::SenderInterface<std::string>> event_sender_;
        std::shared_ptr<psm::SenderInterface<std::string>> energy_sender_;
        std::shared_ptr<psm::SenderInterface<std::string>> singles_energy_sender_;
        std::shared_ptr<psm::SenderInterface<std::string>> run_info_sender_;
        std::shared_ptr<psm::SenderInterface<std::string>> detector_info_sender_;
        std::shared_ptr<psm::SenderInterface<std::string>> dead_time_sender_;

        std::unique_ptr<command_channel::CommandServer> command_server_;

        std::thread state_thread_;
        std::atomic<bool> state_thread_running_{false};
        std::atomic<bool> acquisition_active_{false};

        double idle_poll_interval_sec_ = 2.0;
        double busy_poll_interval_sec_ = 30.0;

        // Last accepted detector run-info sample (used to reject jumpy artifacts).
        std::mutex run_info_history_mutex_;
        bool has_last_valid_run_info_ = false;
        double last_run_info_real_time_sec_ = 0.0;
        double last_run_info_live_time_sec_ = 0.0;
        double last_run_info_stamp_ = 0.0;

        // Last accepted detector-info sample (used as bounded fallback when
        // detector does not answer info requests transiently).
        std::mutex detector_info_cache_mutex_;
        bool has_last_valid_detector_info_ = false;
        socket_comms::DetectorInfo last_detector_info_;
        double last_detector_info_stamp_ = 0.0;
        double detector_info_cache_max_age_sec_ = 120.0;

        std::string detector_frame_;
    };
}

#endif //PHDS_GEGI_DRIVER_PHDS_GEGI_DRIVER_HPP
