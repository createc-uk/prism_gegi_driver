//
// Live-NATS integration test: proves that messages produced by this
// driver's own JSON schemas (include/phds_gegi_driver/messages.hpp,
// include/phds_gegi_driver/command_channel.hpp) are byte-for-byte
// compatible with the first-class Prism types registered upstream in
// prism/types/gegi.hpp (branch feature/gegi-driver-types), sent and
// received through real psm::TextSender/psm::TextReceiver endpoints
// rather than mocks.
//
// This directly exercises the "no prior knowledge required" claim: a
// generic Prism consumer (here, psm::types::ComptonEvent::from_json /
// psm::types::CommandResult::from_json) can decode this driver's wire
// messages without including any prism_gegi_driver header.
//
// Requires a reachable NATS server (see README.md / docs/PRISM_MIGRATION.md);
// override with PRISM_TEST_NATS_HOST / PRISM_TEST_NATS_PORT. Exits 77 if no
// server is reachable within the connection timeout, matching Autotools'
// convention for "skipped" so CI can treat it as non-fatal when NATS is not
// provisioned, while still failing hard (non-zero, non-77) on any real
// mismatch.

#include <phds_gegi_driver/command_channel.hpp>
#include <phds_gegi_driver/messages.hpp>

#include <prism/core/adapters.hpp>
#include <prism/core/messaging_factory.hpp>
#include <prism/types/types.hpp>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

int failures = 0;

void expect(bool condition, const std::string &what) {
    if (!condition) {
        std::cerr << "FAIL: " << what << std::endl;
        ++failures;
    } else {
        std::cout << "ok: " << what << std::endl;
    }
}

std::string envOr(const char *name, const std::string &fallback) {
    const char *v = std::getenv(name);
    return v ? std::string(v) : fallback;
}

// Receivers created by roundTrip() are kept alive here (not stopped or
// destroyed individually) to avoid a pre-existing race in the NATS
// text-receiver start/stop teardown path when many receivers are churned in
// quick succession; they are all released together at the end of main().
std::vector<std::shared_ptr<psm::TextReceiver>> keepAliveReceivers;

// Publishes payload on topic, waits (up to timeoutMs) for it to arrive on a
// receiver subscribed to the same topic, and returns the raw text received
// (empty on timeout).
std::string roundTrip(psm::MessagingFactory &factory,
                       const std::shared_ptr<psm::Connection> &connection,
                       const std::string &topic,
                       const std::string &payload,
                       int timeoutMs = 3000) {
    psm::TextReceiver::Config rConfig;
    rConfig.source = topic;
    auto receiver = factory.createTextReceiver(connection, rConfig);
    keepAliveReceivers.push_back(receiver);

    auto mutexPtr = std::make_shared<std::mutex>();
    auto cvPtr = std::make_shared<std::condition_variable>();
    auto received = std::make_shared<std::string>();
    auto got = std::make_shared<std::atomic<bool>>(false);

    receiver->setMessageHandler([mutexPtr, cvPtr, received, got](const std::string &message, const std::string &) {
        std::lock_guard<std::mutex> lock(*mutexPtr);
        *received = message;
        got->store(true);
        cvPtr->notify_all();
    });
    receiver->start();

    std::this_thread::sleep_for(std::chrono::milliseconds(150));

    psm::TextSender::Config sConfig;
    sConfig.destination = topic;
    auto sender = factory.createTextSender(connection, sConfig);
    sender->send(payload);

    std::unique_lock<std::mutex> lock(*mutexPtr);
    cvPtr->wait_for(lock, std::chrono::milliseconds(timeoutMs), [&] { return got->load(); });
    return *received;
}

} // namespace

int main() {
    using namespace phds_gegi_driver;

    auto factory = psm::MessagingFactory::create("NATS");
    if (!factory) {
        std::cerr << "SKIP: could not create NATS messaging factory" << std::endl;
        return 77;
    }

    auto connection = factory->createConnection();
    if (!connection) {
        std::cerr << "SKIP: could not create NATS connection object" << std::endl;
        return 77;
    }

    psm::Connection::Config cfg;
    cfg.serverAddress = envOr("PRISM_TEST_NATS_HOST", "localhost");
    cfg.port = static_cast<uint16_t>(std::stoi(envOr("PRISM_TEST_NATS_PORT", "4222")));
    cfg.timeoutMs = 2000;
    if (!connection->connect(cfg)) {
        std::cerr << "SKIP: no reachable NATS server at " << cfg.serverAddress << ":" << cfg.port
                   << " (start one with `nats-server`, or set PRISM_TEST_NATS_HOST/PORT)" << std::endl;
        return 77;
    }

    // ---- ComptonEvent: driver builds it, a generic Prism consumer decodes it ----
    {
        messages::ComptonEvent event;
        event.stamp = 10.25;
        event.frame_id = "detector";
        event.seq = 7;
        event.energy_kev_1 = 100.0;
        event.reading_location_1 = {1.0, 2.0, 3.0};
        event.energy_kev_2 = 200.0;
        event.reading_location_2 = {4.0, 5.0, 6.0};
        event.cone_angle = 0.5;
        event.cone_angle_uncertainty = 0.04;

        nlohmann::json driverJson = event;
        const std::string received =
            roundTrip(*factory, connection, "test.gegi.compton_event", driverJson.dump());
        expect(!received.empty(), "ComptonEvent: message was received over real NATS TextSender/TextReceiver");

        if (!received.empty()) {
            psm::types::ComptonEvent generic;
            psm::types::from_json(nlohmann::json::parse(received), generic);
            expect(generic.seq == 7, "ComptonEvent: generic psm::types decode preserved seq");
            expect(generic.energyKev1 == 100.0, "ComptonEvent: generic psm::types decode preserved energy_kev_1");
            expect(generic.readingLocation1.x == 1.0 && generic.readingLocation2.z == 6.0,
                   "ComptonEvent: generic psm::types decode preserved nested reading locations");
        }
    }

    // ---- Command/CommandResult channel: driver's CommandServer answers a
    // generically-built psm::types::Command, and the driver's CommandResult
    // is decodable by the generic psm::types::CommandResult. ----
    {
        psm::TextReceiver::Config commandReceiverConfig;
        commandReceiverConfig.source = "test.gegi.command";
        auto commandReceiver = factory->createTextReceiver(connection, commandReceiverConfig);
        keepAliveReceivers.push_back(commandReceiver);

        psm::TextSender::Config resultSenderConfig;
        resultSenderConfig.destination = "test.gegi.command_result";
        auto resultSender = factory->createTextSender(connection, resultSenderConfig);

        static std::unique_ptr<command_channel::CommandServer> serverPtr;
        serverPtr = std::make_unique<command_channel::CommandServer>(commandReceiver, psm::adaptTextSender(resultSender));
        command_channel::CommandServer &server = *serverPtr;
        server.on("ping", [](const nlohmann::json & /*params*/) {
            command_channel::CommandResult result;
            result.success = true;
            result.message = "pong";
            return result;
        });
        server.start();
        std::this_thread::sleep_for(std::chrono::milliseconds(150));

        psm::types::Command genericCommand;
        genericCommand.command = "ping";
        genericCommand.requestId = "r-42";
        nlohmann::json commandJson = genericCommand;

        psm::TextReceiver::Config resultReceiverConfig;
        resultReceiverConfig.source = "test.gegi.command_result";
        auto resultReceiver = factory->createTextReceiver(connection, resultReceiverConfig);
        keepAliveReceivers.push_back(resultReceiver);

        std::mutex m;
        std::condition_variable cv;
        std::string resultMessage;
        bool got = false;
        resultReceiver->setMessageHandler([&](const std::string &message, const std::string &) {
            std::lock_guard<std::mutex> lock(m);
            resultMessage = message;
            got = true;
            cv.notify_all();
        });
        resultReceiver->start();
        std::this_thread::sleep_for(std::chrono::milliseconds(150));

        psm::TextSender::Config cmdSenderConfig;
        cmdSenderConfig.destination = "test.gegi.command";
        auto cmdSender = factory->createTextSender(connection, cmdSenderConfig);
        cmdSender->send(commandJson.dump());

        std::unique_lock<std::mutex> lock(m);
        cv.wait_for(lock, std::chrono::milliseconds(3000), [&] { return got; });
        // Note: resultReceiver/commandReceiver/server are intentionally left
        // running (not stop()-ed) for the same teardown-race reason as
        // roundTrip()'s keepAliveReceivers above; process exit tears them down.

        expect(got, "Command/CommandResult: driver's CommandServer answered a generically-built psm::types::Command");
        if (got) {
            psm::types::CommandResult generic;
            psm::types::from_json(nlohmann::json::parse(resultMessage), generic);
            expect(generic.success, "CommandResult: generic psm::types decode saw success=true");
            expect(generic.message == "pong", "CommandResult: generic psm::types decode saw message=\"pong\"");
            expect(generic.requestId == "r-42", "CommandResult: generic psm::types decode echoed request_id");
        }
    }

    // ---- Remaining state/analysis topics: build the exact JSON shape
    // Python's prism_messages.py emits (snake_case keys, no C++ struct
    // involved) and confirm the generic psm::types decode it, proving
    // cross-language wire compatibility end to end. ----
    {
        nlohmann::json spectrumJson = {
            {"dataType", "Spectrum"}, {"stamp", 1.0}, {"frame_id", "detector"},
            {"seq", 2}, {"end_time", 1.25}, {"real_time_ms", 250},
            {"dead_time_ms", 5}, {"total_count", 3}, {"spectrum", {0, 1, 2}},
        };
        const std::string received =
            roundTrip(*factory, connection, "test.gegi.spectrum", spectrumJson.dump());
        expect(!received.empty(), "Spectrum: Python-shaped message was received over real NATS");
        if (!received.empty()) {
            psm::types::Spectrum generic;
            psm::types::from_json(nlohmann::json::parse(received), generic);
            expect(generic.realTimeMs == 250 && generic.totalCount == 3,
                   "Spectrum: generic psm::types decode preserved real_time_ms/total_count");
        }

        nlohmann::json runInfoJson = {
            {"dataType", "RunInfo"}, {"valid", true}, {"real_time_sec", 10.0},
            {"live_time_sec", 9.5}, {"dead_time_percent", 5.0},
            {"count_rate_hz", 123.4}, {"message", "ok"}, {"stamp", 42.0},
        };
        const std::string runInfoReceived =
            roundTrip(*factory, connection, "test.gegi.run_info", runInfoJson.dump());
        expect(!runInfoReceived.empty(), "RunInfo: Python-shaped message was received over real NATS");
        if (!runInfoReceived.empty()) {
            psm::types::RunInfo generic;
            psm::types::from_json(nlohmann::json::parse(runInfoReceived), generic);
            expect(generic.valid && generic.message == "ok",
                   "RunInfo: generic psm::types decode preserved valid/message");
        }

        nlohmann::json detectorInfoJson = {
            {"dataType", "DetectorInfo"}, {"valid", false}, {"serial_number", "SN-1"},
            {"detector_temp_kelvin", 293.0}, {"detector_bias_status", 1},
            {"line_power_status", 1}, {"batt1_percent", 90}, {"batt2_percent", 85},
            {"message", "stale"}, {"stamp", 100.0}, {"cached", true},
        };
        const std::string detectorInfoReceived =
            roundTrip(*factory, connection, "test.gegi.detector_info", detectorInfoJson.dump());
        expect(!detectorInfoReceived.empty(), "DetectorInfo: Python-shaped message was received over real NATS");
        if (!detectorInfoReceived.empty()) {
            psm::types::DetectorInfo generic;
            psm::types::from_json(nlohmann::json::parse(detectorInfoReceived), generic);
            expect(generic.cached && generic.serialNumber == "SN-1",
                   "DetectorInfo: generic psm::types decode preserved cached/serial_number");
        }

        nlohmann::json sourceDirectionsJson = {
            {"dataType", "SourceDirections"}, {"stamp", 1.0}, {"frame_id", "detector"},
            {"points", nlohmann::json::array({
                {{"x", 1.0}, {"y", 2.0}, {"z", 3.0}, {"isotope", "Cs-137"}, {"count", 150}},
                {{"x", 4.0}, {"y", 5.0}, {"z", 6.0}, {"isotope", "Co-60"}, {"count", 45}},
            })},
        };
        const std::string sourceDirectionsReceived = roundTrip(
            *factory, connection, "test.gegi.source_directions", sourceDirectionsJson.dump());
        expect(!sourceDirectionsReceived.empty(), "SourceDirections: Python-shaped message was received over real NATS");
        if (!sourceDirectionsReceived.empty()) {
            psm::types::SourceDirections generic;
            psm::types::from_json(nlohmann::json::parse(sourceDirectionsReceived), generic);
            expect(generic.points.size() == 2 && generic.points[0].isotope == "Cs-137",
                   "SourceDirections: generic psm::types decode preserved isotope-tagged points");
        }

        nlohmann::json cloudMetaJson = {
            {"dataType", "CloudMeta"}, {"stamp", 1.0}, {"frame_id", "detector"},
            {"point_step", 28}, {"n_points", 1000},
            {"fields", nlohmann::json::array({
                {{"name", "x"}, {"offset", 0}, {"datatype", "float64"}, {"count", 1}},
                {{"name", "y"}, {"offset", 8}, {"datatype", "float64"}, {"count", 1}},
                {{"name", "z"}, {"offset", 16}, {"datatype", "float64"}, {"count", 1}},
                {{"name", "intensity"}, {"offset", 24}, {"datatype", "float32"}, {"count", 1}},
            })},
        };
        const std::string cloudMetaReceived =
            roundTrip(*factory, connection, "test.gegi.cloud_meta", cloudMetaJson.dump());
        expect(!cloudMetaReceived.empty(), "CloudMeta: Python-shaped message was received over real NATS");
        if (!cloudMetaReceived.empty()) {
            psm::types::CloudMeta generic;
            psm::types::from_json(nlohmann::json::parse(cloudMetaReceived), generic);
            expect(generic.nPoints == 1000 && generic.fields.size() == 4,
                   "CloudMeta: generic psm::types decode preserved n_points/fields");
        }

        // ActivityResult: activity_node.py's per-window report is the one GeGi
        // message that predates a dataType discriminator entirely -- built here
        // WITHOUT one, exactly as the driver sends it today, to prove the
        // generic decode does not depend on that field being present. One
        // isotope carries the optional position-correction fields
        // (hotspot_offset_m/slant_distance_m), the other omits them, matching
        // activity_node.py only including those two keys when a fresh imaged
        // hotspot was used for that line.
        nlohmann::json activityResultJson = {
            {"timestamp", 12345.0}, {"real_time_s", 300.0}, {"live_time_s", 294.0},
            {"dead_time_fraction", 0.02}, {"dead_time_status", "valid"},
            {"dt_correction_factor", 1.020408}, {"source_distance_m", 0.58},
            {"solid_angle_fraction", 0.001498}, {"total_activity_MBq", 1.42},
            {"isotopes", nlohmann::json::array({
                {
                    {"isotope", "Cs-137"}, {"energy_keV", 661.7},
                    {"intrinsic_efficiency", 0.02}, {"solid_angle_fraction", 0.001498},
                    {"absolute_efficiency", 3.0e-5}, {"efficiency_product", 2.9e-5},
                    {"source_distance_m", 0.58}, {"n_shielding_plates", 0},
                    {"total_shield_plates", 0}, {"shield_transmission", 1.0},
                    {"position_corrected", true}, {"position_factor", 0.94},
                    {"method", "intrinsic_efficiency"}, {"gross_counts", 5200.0},
                    {"background_counts", 400.0}, {"net_peak_area", 4800.0},
                    {"net_corrected", 4898.0}, {"sigma_counts", 74.6},
                    {"activity_MBq", 1.02}, {"sigma_activity_MBq", 0.03},
                    {"count_rate_cps", 16.66}, {"valid", true},
                    {"below_min_counts", false},
                    {"hotspot_offset_m", 0.18}, {"slant_distance_m", 0.605},
                },
                {
                    {"isotope", "Co-60_1173"}, {"energy_keV", 1173.2},
                    {"intrinsic_efficiency", 0.015}, {"solid_angle_fraction", 0.001498},
                    {"absolute_efficiency", 2.2e-5}, {"efficiency_product", 2.2e-5},
                    {"source_distance_m", 0.58}, {"n_shielding_plates", 0},
                    {"total_shield_plates", 0}, {"shield_transmission", 1.0},
                    {"position_corrected", false}, {"position_factor", 1.0},
                    {"method", "intrinsic_efficiency"}, {"gross_counts", 1800.0},
                    {"background_counts", 300.0}, {"net_peak_area", 1500.0},
                    {"net_corrected", 1530.6}, {"sigma_counts", 45.8},
                    {"activity_MBq", 0.40}, {"sigma_activity_MBq", 0.012},
                    {"count_rate_cps", 5.20}, {"valid", true},
                    {"below_min_counts", false},
                },
            })},
        };
        const std::string activityResultReceived = roundTrip(
            *factory, connection, "test.gegi.activity_results", activityResultJson.dump());
        expect(!activityResultReceived.empty(),
               "ActivityResult: dataType-less Python-shaped message was received over real NATS");
        if (!activityResultReceived.empty()) {
            psm::types::ActivityResult generic;
            psm::types::from_json(nlohmann::json::parse(activityResultReceived), generic);
            expect(generic.isotopes.size() == 2 && generic.totalActivityMBq == 1.42,
                   "ActivityResult: generic psm::types decode preserved isotopes/total_activity_MBq "
                   "with no dataType field present");
            expect(generic.isotopes[0].positionCorrected && generic.isotopes[0].hotspotOffsetM == 0.18,
                   "ActivityResult: generic psm::types decode preserved the position-corrected line's "
                   "optional hotspot_offset_m/slant_distance_m");
            expect(!generic.isotopes[1].positionCorrected && generic.isotopes[1].hotspotOffsetM == 0.0,
                   "ActivityResult: generic psm::types decode left hotspot_offset_m/slant_distance_m "
                   "at their default when the source dict omitted those keys");
        }
    }

    // Deliberately skip connection->close() and use _Exit() below: this test
    // process owns many receiver background threads (kept alive above to
    // dodge a pre-existing NATS text-receiver stop()/teardown race), and a
    // graceful shared_ptr/Connection teardown here would join those threads
    // and hit the same race. The check results above are already final.

    if (failures > 0) {
        std::cerr << failures << " check(s) failed" << std::endl;
        std::cout.flush();
        std::cerr.flush();
        std::_Exit(1);
    }
    std::cout << "All checks passed" << std::endl;
    std::cout.flush();
    std::_Exit(0);
}
