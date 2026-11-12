// No-hardware C++ Prism command handler regression test.
// g++ -std=c++17 -Iinclude -I<prism>/include test/test_cpp_command_delivery.cpp
//     -o /tmp/gegi_cpp_command_test -lspdlog -lfmt
#include <phds_gegi_driver/command_channel.hpp>

#include <cassert>
#include <iostream>
#include <string>
#include <utility>
#include <vector>

using phds_gegi_driver::command_channel::CommandResult;
using phds_gegi_driver::command_channel::CommandServer;

struct FakeReceiver : psm::TextReceiver {
    MessageHandler callback;
    bool running = false;
    void setMessageHandler(MessageHandler handler) override { callback = std::move(handler); }
    void start() override { running = true; }
    void stop() override { running = false; }
    bool isRunning() const override { return running; }
    std::string getSource() const override { return "gegi.diagnostics.command"; }
    void deliver(const nlohmann::json &msg) {
        assert(running);
        callback(msg.dump(), getSource());
    }
    void deliverRaw(const std::string &msg) {
        assert(running);
        callback(msg, getSource());
    }
};

struct FakeSender : psm::SenderInterface<std::string> {
    std::vector<std::string> replies;
    void send(const std::string &msg) override { replies.push_back(msg); }
};

int main() {
    auto receiver = std::make_shared<FakeReceiver>();
    auto sender = std::make_shared<FakeSender>();
    CommandServer server(receiver, sender);
    int executed = 0;
    server.on("diagnostic_ping", [&](const nlohmann::json &) {
        ++executed;
        CommandResult r;
        r.success = true;
        r.message = "pong";
        return r;
    });
    server.start();

    receiver->deliver({{"dataType", "Command"}, {"command", "diagnostic_ping"},
                       {"request_id", "id-1"}});
    assert(executed == 1);
    assert(sender->replies.size() == 1);
    auto first = nlohmann::json::parse(sender->replies.back());
    assert(first.value("dataType", "") == "CommandResult");
    assert(first.value("request_id", "") == "id-1");
    assert(first.value("success", false));

    receiver->deliver({{"command", "diagnostic_ping"}, {"request_id", "id-1"}});
    assert(executed == 1);
    assert(sender->replies.at(0) == sender->replies.at(1));

    receiver->deliver({{"command", "other"}, {"request_id", "id-1"}});
    assert(executed == 1);
    auto wrong = nlohmann::json::parse(sender->replies.back());
    assert(!wrong.value("success", true));

    receiver->deliverRaw("[]");
    auto malformed = nlohmann::json::parse(sender->replies.back());
    assert(!malformed.value("success", true));

    receiver->deliver({{"command", "diagnostic_ping"}, {"request_id", "id-2"}});
    assert(executed == 2);
    server.stop();
    std::cout << "PASS: C++ CommandServer typed/raw JSON, correlation, dedup and malformed rejection\n";
    return 0;
}
