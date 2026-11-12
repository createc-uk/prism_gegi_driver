//
// Prism-based replacement for the old ROS service model.
//
// Prism's protocol-agnostic Application/MessagingFactory layer intentionally
// has no request/reply primitive (NATS has one, but using it would tie this
// driver to a specific backend and break --protocol portability). Instead,
// every former ROS service is modelled as a pair of pub/sub topics:
//
//   <ns>.command         (in)  -- JSON {"command": "...", ...params,
//                                        "request_id"?: "..."}
//   <ns>.command_result   (out) -- JSON CommandResult, echoing "request_id"
//                                        if the caller supplied one.
//
// CommandServer dispatches incoming commands by name to registered handlers
// and publishes the result. CommandClient is an optional convenience for
// callers that want a synchronous call() (it correlates by request_id using
// a condition_variable -- no backend RPC feature is used, so this works on
// every protocol Prism supports).

#ifndef PHDS_GEGI_DRIVER_COMMAND_CHANNEL_HPP
#define PHDS_GEGI_DRIVER_COMMAND_CHANNEL_HPP

#include <phds_gegi_driver/messages.hpp>

#include <prism/core/sender_interface.hpp>
#include <prism/core/text_receiver.hpp>
#include <prism/utils/logger.hpp>

#include <nlohmann/json.hpp>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>

namespace phds_gegi_driver::command_channel {

    struct CommandResult {
        std::string command;
        bool success = false;
        std::string message;
        std::string request_id; // echoed back if the caller supplied one
        double stamp = 0.0;
    };

    inline void to_json(nlohmann::json &j, const CommandResult &r) {
        j = nlohmann::json{
            {"dataType", "CommandResult"},
            {"command", r.command},
            {"success", r.success},
            {"message", r.message},
            {"stamp", r.stamp},
        };
        if (!r.request_id.empty()) {
            j["request_id"] = r.request_id;
        }
    }

    inline void from_json(const nlohmann::json &j, CommandResult &r) {
        j.at("command").get_to(r.command);
        j.at("success").get_to(r.success);
        j.at("message").get_to(r.message);
        if (j.contains("stamp")) j.at("stamp").get_to(r.stamp);
        if (j.contains("request_id")) j.at("request_id").get_to(r.request_id);
    }

    // Handler receives the full command JSON (including "command" and any
    // extra params) and returns {success, message}. CommandServer fills in
    // the command name / request_id / stamp automatically.
    using Handler = std::function<CommandResult(const nlohmann::json &params)>;

    // Wraps one "*.command" receiver + one "*.command_result" sender and
    // dispatches by the JSON "command" field.
    class CommandServer {
    public:
        CommandServer(std::shared_ptr<psm::TextReceiver> commandReceiver,
                       std::shared_ptr<psm::SenderInterface<std::string>> resultSender)
            : commandReceiver_(std::move(commandReceiver)),
              resultSender_(std::move(resultSender)) {
            commandReceiver_->setMessageHandler(
                [this](const std::string &message, const std::string & /*source*/) {
                    handleMessage(message);
                });
        }

        // Registers a handler for a given "command" field value.
        void on(const std::string &commandName, Handler handler) {
            handlers_[commandName] = std::move(handler);
        }

        void start() { commandReceiver_->start(); }
        void stop() { commandReceiver_->stop(); }

    private:
        void handleMessage(const std::string &message) {
            std::string commandName;
            std::string requestId;
            nlohmann::json params;
            try {
                params = nlohmann::json::parse(message);
                if (!params.is_object()) {
                    throw std::invalid_argument("command must be a JSON object");
                }
                commandName = params.value("command", std::string());
                requestId = params.value("request_id", std::string());
                if (commandName.empty()) {
                    throw std::invalid_argument("command must be a non-empty string");
                }
            } catch (const std::exception &e) {
                PSM_WARN("Malformed GeGi detector command: {}", e.what());
                CommandResult malformed;
                malformed.command = commandName;
                malformed.request_id = requestId;
                malformed.message = std::string("Malformed command message: ") + e.what();
                malformed.stamp = messages::nowSeconds();
                nlohmann::json reply = malformed;
                sendResult(reply.dump(), commandName, requestId);
                return;
            }

            const std::string traceId =
                (params.contains("trace_id") && params["trace_id"].is_string())
                    ? params["trace_id"].get<std::string>() : std::string();
            PSM_INFO("GeGi detector command received: command={} request_id={} trace_id={}",
                     commandName, requestId.empty() ? "<none>" : requestId,
                     traceId.empty() ? "<none>" : traceId);

            std::string serializedResult;
            {
                // Process-wide at-most-once dispatch for each request_id.
                // Protects against accidental duplicate command publishes, not
                // process restarts or uncertain hardware TCP acknowledgement.
                std::lock_guard<std::mutex> lock(dispatchMutex_);
                const auto prior = requestId.empty() ? completed_.end()
                                                     : completed_.find(requestId);
                if (prior != completed_.end()) {
                    if (prior->second.first == commandName) {
                        PSM_WARN("GeGi command duplicate; reusing reply: command={} request_id={}",
                                 commandName, requestId);
                        serializedResult = prior->second.second;
                    } else {
                        CommandResult conflict;
                        conflict.command = commandName;
                        conflict.request_id = requestId;
                        conflict.message = "request_id already used for another command";
                        conflict.stamp = messages::nowSeconds();
                        nlohmann::json reply = conflict;
                        serializedResult = reply.dump();
                    }
                } else {
                    CommandResult result;
                    try {
                        auto it = handlers_.find(commandName);
                        if (it == handlers_.end()) {
                            result.message = "Unknown command: " + commandName;
                        } else {
                            result = it->second(params);
                        }
                    } catch (const std::exception &e) {
                        PSM_ERROR("GeGi detector command handler error: command={} request_id={} error={}",
                                  commandName, requestId, e.what());
                        result.success = false;
                        result.message = "Command handler failed; inspect driver logs";
                    } catch (...) {
                        PSM_ERROR("GeGi detector command handler error: command={} request_id={}",
                                  commandName, requestId);
                        result.success = false;
                        result.message = "Command handler failed; inspect driver logs";
                    }
                    result.command = commandName;
                    result.request_id = requestId;
                    result.stamp = messages::nowSeconds();
                    nlohmann::json reply = result;
                    serializedResult = reply.dump();

                    if (!requestId.empty()) {
                        completed_[requestId] = {commandName, serializedResult};
                        completedOrder_.push_back(requestId);
                        if (completedOrder_.size() > 1024) {
                            completed_.erase(completedOrder_.front());
                            completedOrder_.pop_front();
                        }
                    }
                }
            }
            sendResult(serializedResult, commandName, requestId);
        }

        void sendResult(const std::string &payload,
                        const std::string &commandName,
                        const std::string &requestId) {
            try {
                resultSender_->send(payload);
                PSM_INFO("GeGi detector reply send invoked: command={} request_id={}",
                         commandName, requestId.empty() ? "<none>" : requestId);
            } catch (const std::exception &e) {
                // Never reexecute a detector action just because its reply failed.
                PSM_ERROR("GeGi detector reply failed: command={} request_id={} error={}",
                          commandName, requestId, e.what());
            }
        }

        std::mutex dispatchMutex_;
        std::unordered_map<std::string, std::pair<std::string, std::string>> completed_;
        std::deque<std::string> completedOrder_;

        std::shared_ptr<psm::TextReceiver> commandReceiver_;
        std::shared_ptr<psm::SenderInterface<std::string>> resultSender_;
        std::unordered_map<std::string, Handler> handlers_;
    };

    // Optional client-side convenience: publish a command and, if desired,
    // wait for the matching command_result (matched by request_id). Not
    // used by the C++ driver itself, but shared here for other C++ tools
    // (e.g. data_recorder-style orchestrators written in C++) that want a
    // synchronous call() without depending on any protocol-specific RPC.
    class CommandClient {
    public:
        CommandClient(std::shared_ptr<psm::SenderInterface<std::string>> commandSender,
                       std::shared_ptr<psm::TextReceiver> resultReceiver)
            : commandSender_(std::move(commandSender)),
              resultReceiver_(std::move(resultReceiver)) {
            resultReceiver_->setMessageHandler(
                [this](const std::string &message, const std::string & /*source*/) {
                    onResult(message);
                });
            resultReceiver_->start();
        }

        ~CommandClient() { resultReceiver_->stop(); }

        // Fire-and-forget.
        void publish(nlohmann::json command) {
            commandSender_->send(command.dump());
        }

        // Publishes `command` with a generated request_id and blocks (up to
        // timeoutMs) for the correlated command_result.
        std::optional<CommandResult> call(nlohmann::json command, int timeoutMs = 5000) {
            const std::string requestId = generateRequestId();
            command["request_id"] = requestId;

            std::unique_lock<std::mutex> lock(mutex_);
            pending_[requestId] = std::nullopt;
            lock.unlock();

            commandSender_->send(command.dump());

            lock.lock();
            const bool got = cv_.wait_for(lock, std::chrono::milliseconds(timeoutMs), [&] {
                auto it = pending_.find(requestId);
                return it != pending_.end() && it->second.has_value();
            });

            std::optional<CommandResult> result;
            if (got) {
                result = pending_[requestId];
            }
            pending_.erase(requestId);
            return result;
        }

    private:
        void onResult(const std::string &message) {
            try {
                auto j = nlohmann::json::parse(message);
                CommandResult result = j.get<CommandResult>();
                if (result.request_id.empty()) return;

                std::lock_guard<std::mutex> lock(mutex_);
                auto it = pending_.find(result.request_id);
                if (it != pending_.end()) {
                    it->second = result;
                    cv_.notify_all();
                }
            } catch (const std::exception &e) {
                PSM_WARN("CommandClient: failed to parse command_result: {}", e.what());
            }
        }

        static std::string generateRequestId() {
            static std::atomic<uint64_t> counter{0};
            const auto now = messages::nowSeconds();
            return std::to_string(now) + "-" + std::to_string(counter.fetch_add(1));
        }

        std::shared_ptr<psm::SenderInterface<std::string>> commandSender_;
        std::shared_ptr<psm::TextReceiver> resultReceiver_;

        std::mutex mutex_;
        std::condition_variable cv_;
        std::unordered_map<std::string, std::optional<CommandResult>> pending_;
    };

} // namespace phds_gegi_driver::command_channel

#endif // PHDS_GEGI_DRIVER_COMMAND_CHANNEL_HPP
