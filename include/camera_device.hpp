#pragma once
#include <libobsensor/ObSensor.hpp>
#include <opencv2/opencv.hpp>
#include <chrono>
#include <atomic>
#include <memory>
#include <string>

extern std::atomic<bool> g_stop_requested;

struct CameraConfig {
    int number;
    std::string serial;
};

struct RecordingResult {
    int cameraNumber;
    std::string serial;
    std::string file;
    std::string startedAt;
    int frames = 0;
    int timeouts = 0;
    double elapsedSeconds = 0;
};

class CameraDevice {
public:
    CameraDevice(std::shared_ptr<ob::Device> device, CameraConfig config,
                 const std::string& outputDirectory);
    ~CameraDevice();
    void start();
    RecordingResult record(std::chrono::steady_clock::time_point deadline,
                           const std::atomic<bool>& cancelled);
    void stop() noexcept;
private:
    CameraConfig config;
    std::string directory;
    std::shared_ptr<ob::Pipeline> pipeline;
    cv::VideoWriter writer;
    bool streaming = false;
};
