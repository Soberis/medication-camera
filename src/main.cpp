#include "camera_manager.hpp"
#include "manifest.hpp"
#include <csignal>
#include <filesystem>
#include <future>
#include <iostream>
#include <set>
#include <stdexcept>

std::atomic<bool> g_stop_requested{false};

namespace {
void signalHandler(int /*signum*/) {
    g_stop_requested.store(true);
}
}

int main(int argc, char** argv) {
    std::signal(SIGINT, signalHandler);
    std::signal(SIGTERM, signalHandler);

    if (argc != 3) {
        std::cerr << "Usage: medication_camera CONFIG.json NEW_OUTPUT_DIRECTORY\n";
        return 2;
    }
    try {
        cv::FileStorage input(argv[1], cv::FileStorage::READ);
        if (!input.isOpened()) throw std::runtime_error("Cannot open config");
        const int duration = static_cast<int>(input["duration_seconds"]);
        if (duration < 1 || duration > 3600) throw std::runtime_error("Duration must be 1..3600 seconds");
        std::vector<CameraConfig> config;
        std::set<int> numbers;
        std::set<std::string> serials;
        for (const auto& node : input["cameras"]) {
            CameraConfig camera{static_cast<int>(node["camera_number"]),
                                static_cast<std::string>(node["serial"])};
            if (camera.number < 1 || camera.number > 64 || camera.serial.empty() ||
                !numbers.insert(camera.number).second)
                throw std::runtime_error("Invalid camera identity");
            if (camera.serial != "auto" && !serials.insert(camera.serial).second)
                throw std::runtime_error("Duplicate camera serial");
            config.push_back(camera);
        }
        if (config.empty() || config.size() > 2)
            throw std::runtime_error("This recorder requires 1 or 2 configured cameras");
        if (!std::filesystem::create_directory(argv[2]))
            throw std::runtime_error("Output directory already exists; refusing overwrite");
        CameraManager manager;
        auto devices = manager.open(config, argv[2]);
        for (auto& device : devices) device->start();
        std::promise<std::chrono::steady_clock::time_point> start;
        auto gate = start.get_future().share();
        std::atomic<bool> cancelled{false};
        std::vector<std::future<RecordingResult>> workers;
        try {
            for (auto& device : devices) {
                workers.push_back(std::async(std::launch::async, [device, gate, &cancelled]() {
                    try { return device->record(gate.get(), cancelled); }
                    catch (...) {
                        cancelled = true;
                        device->stop();
                        throw;
                    }
                }));
            }
        } catch (...) {
            start.set_exception(std::current_exception());
            throw;
        }
        // Both pipelines are running before either worker starts recording.
        start.set_value(std::chrono::steady_clock::now() + std::chrono::seconds(duration));
        std::vector<RecordingResult> results;
        std::exception_ptr failure;
        for (auto& worker : workers) {
            try { results.push_back(worker.get()); }
            catch (...) { if (!failure) failure = std::current_exception(); }
        }
        if (failure) std::rethrow_exception(failure);
        writeRecordingResult(argv[2], results);
        std::cout << "Recording complete: " << argv[2] << std::endl;
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Recording failed: " << error.what() << std::endl;
        return 1;
    }
}
