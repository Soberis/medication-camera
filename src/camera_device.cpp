#include "camera_device.hpp"
#include "manifest.hpp"
#include <filesystem>
#include <iostream>
#include <limits>
#include <stdexcept>

CameraDevice::CameraDevice(std::shared_ptr<ob::Device> device, CameraConfig config,
                           const std::string& outputDirectory)
    : config(std::move(config)), directory(outputDirectory),
      pipeline(std::make_shared<ob::Pipeline>(device)) {}

CameraDevice::~CameraDevice() { stop(); }

void CameraDevice::start() {
    auto profiles = pipeline->getStreamProfileList(OB_SENSOR_COLOR);
    auto profile = profiles->getVideoStreamProfile(1920, 1080, OB_FORMAT_MJPG, 30);
    auto streamConfig = std::make_shared<ob::Config>();
    streamConfig->enableStream(profile);
    const auto path = std::filesystem::path(directory) /
                      ("camera_" + std::to_string(config.number) + ".raw.mp4");
    if (std::filesystem::exists(path))
        throw std::runtime_error("Refusing to overwrite an existing recording");
    std::vector<std::pair<std::string, int>> codecs = {
        {"avc1", cv::VideoWriter::fourcc('a', 'v', 'c', '1')},
        {"H264", cv::VideoWriter::fourcc('H', '2', '6', '4')},
        {"MJPG", cv::VideoWriter::fourcc('M', 'J', 'P', 'G')},
        {"XVID", cv::VideoWriter::fourcc('X', 'V', 'I', 'D')},
        {"mp4v", cv::VideoWriter::fourcc('m', 'p', '4', 'v')}
    };
    bool opened = false;
    for (const auto& [name, fcc] : codecs) {
        if (writer.open(path.string(), cv::CAP_FFMPEG, fcc, 30, cv::Size(1920, 1080))) {
            std::cout << "Opened camera " << config.serial << " writer with codec " << name << " (CAP_FFMPEG)\n";
            opened = true;
            break;
        }
        if (writer.open(path.string(), fcc, 30, cv::Size(1920, 1080))) {
            std::cout << "Opened camera " << config.serial << " writer with codec " << name << "\n";
            opened = true;
            break;
        }
    }
    if (!opened)
        throw std::runtime_error("Cannot open video writer for camera " + config.serial);
    pipeline->start(streamConfig);
    streaming = true;
}

RecordingResult CameraDevice::record(std::chrono::steady_clock::time_point deadline,
                                     const std::atomic<bool>& cancelled) {
    RecordingResult result{config.number, config.serial,
                           "camera_" + std::to_string(config.number) + ".raw.mp4", ""};
    auto started = std::chrono::steady_clock::now();
    auto lastFrame = started;
    auto lastReport = started;
    int lastCount = 0;
    while (!cancelled && std::chrono::steady_clock::now() < deadline) {
        auto frames = pipeline->waitForFrameset(1000);
        auto frame = frames ? frames->getFrame(OB_FRAME_COLOR) : nullptr;
        if (!frame) {
            ++result.timeouts;
            if (std::chrono::steady_clock::now() - lastFrame > std::chrono::seconds(5))
                throw std::runtime_error("No RGB frames for 5 seconds: " + config.serial);
            continue;
        }
        auto color = frame->as<ob::ColorFrame>();
        const auto size = color->getDataSize();
        if (!size || size > static_cast<uint32_t>(std::numeric_limits<int>::max()))
            throw std::runtime_error("Invalid MJPG frame size");
        // MJPG is compressed bytes, not a width * height BGR pixel buffer.
        cv::Mat encoded(1, static_cast<int>(size), CV_8UC1, color->getData());
        auto image = cv::imdecode(encoded, cv::IMREAD_COLOR);
        if (image.empty() || image.size() != cv::Size(1920, 1080))
            throw std::runtime_error("Invalid decoded RGB frame: " + config.serial);
        if (result.frames == 0) result.startedAt = utcNow();
        writer.write(image);
        ++result.frames;
        lastFrame = std::chrono::steady_clock::now();
        const double interval = std::chrono::duration<double>(lastFrame - lastReport).count();
        if (interval >= 1.0) {
            std::cout << "camera=" << config.number << " sn=" << config.serial
                      << " rgb_fps=" << (result.frames - lastCount) / interval
                      << " timeouts=" << result.timeouts << std::endl;
            lastReport = lastFrame;
            lastCount = result.frames;
        }
    }
    result.elapsedSeconds = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - started).count();
    stop();
    if (cancelled) throw std::runtime_error("Recording cancelled because another camera failed");
    if (result.frames == 0) throw std::runtime_error("Recording contains no frames");
    return result;
}

void CameraDevice::stop() noexcept {
    if (streaming) {
        try { pipeline->stop(); }
        catch (...) { std::cerr << "Pipeline stop failed: " << config.serial << std::endl; }
        streaming = false;
    }
    try { writer.release(); } catch (...) {}
}
