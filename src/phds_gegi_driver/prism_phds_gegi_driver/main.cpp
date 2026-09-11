//
// Created by createc on 10/09/2020.
// Migrated from ROS 1 (node + nodelet + pluginlib manifest) to a single
// Prism psm::Application executable. There is no Prism equivalent of ROS
// nodelets, so the node/nodelet/pluginlib split collapses into one binary.
//

#include <phds_gegi_driver/phds_gegi_driver.hpp>

#include <prism/core/exceptions.hpp>

#include <chrono>
#include <iostream>
#include <thread>

int main(int argc, char *argv[]) {
    try {
        psm::Application app("phds_gegi_driver", "PHDS GeGi Compton detector driver", argc, argv);

        app.options().add_options("GeGi")
            ("gegi-ip", "GeGi detector IP address",
             cxxopts::value<std::string>()->default_value("192.168.50.109"))
            ("gegi-port", "GeGi detector TCP port",
             cxxopts::value<std::string>()->default_value("27015"))
            ("detector-frame", "Frame/reference-name reported on messages",
             cxxopts::value<std::string>()->default_value("detector"))
            ("detector-info-cache-max-age-sec", "Max age (s) of cached detector info used as a fallback",
             cxxopts::value<double>()->default_value("120.0"))
            ("idle-poll-interval-sec", "run_info/detector_info republish interval while idle (s)",
             cxxopts::value<double>()->default_value("2.0"))
            ("busy-poll-interval-sec", "run_info/detector_info republish interval while acquisition is active (s)",
             cxxopts::value<double>()->default_value("30.0"))
            ("energy-cal-c0", "Energy calibration correction c0 (keV): corrected = c0 + c1*E + c2*E^2",
             cxxopts::value<double>()->default_value("0.0"))
            ("energy-cal-c1", "Energy calibration correction c1 (unitless gain term)",
             cxxopts::value<double>()->default_value("1.0"))
            ("energy-cal-c2", "Energy calibration correction c2 (1/keV, quadratic term)",
             cxxopts::value<double>()->default_value("0.0"));

        auto result = app.parse();
        if (!result) return 1;

        app.initLogger(*result);

        auto connection = app.createConnection(*result);
        if (!connection) {
            PSM_ERROR("Could not connect to messaging backend — is the server running?");
            return 1;
        }

        phds_gegi_driver::PhdsGegiDriver driver(app, *result, connection);

        while (app.isRunning()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }

        connection->close();
        psm::utils::Logger::shutdown();
        return 0;

    } catch (const psm::PrismException &e) {
        std::cerr << "Prism error: " << e.what() << std::endl;
        return 1;
    } catch (const std::exception &e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return 1;
    }
}
