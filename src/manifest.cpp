#include "manifest.hpp"
#include <ctime>
#include <filesystem>
#include <iomanip>
#include <sstream>
#include <stdexcept>

std::string utcNow() {
    const auto now = std::chrono::system_clock::now();
    const auto seconds = std::chrono::system_clock::to_time_t(now);
    std::tm utc{};
#ifdef _WIN32
    gmtime_s(&utc, &seconds);
#else
    gmtime_r(&seconds, &utc);
#endif
    const auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()).count() % 1000;
    std::ostringstream value;
    value << std::put_time(&utc, "%Y-%m-%dT%H:%M:%S") << '.'
          << std::setfill('0') << std::setw(3) << millis << 'Z';
    return value.str();
}

void writeRecordingResult(const std::string& directory,
                          const std::vector<RecordingResult>& results) {
    const auto target = std::filesystem::path(directory) / "capture-result.json";
    const auto temporary = target.string() + ".tmp";
    cv::FileStorage out(temporary, cv::FileStorage::WRITE | cv::FileStorage::FORMAT_JSON);
    if (!out.isOpened()) throw std::runtime_error("Cannot write capture result");
    out << "schema_version" << "camera-recording-v1" << "videos" << "[";
    for (const auto& result : results) {
        out << "{" << "camera_number" << result.cameraNumber
            << "serial" << result.serial << "file_name" << result.file
            << "record_start_at" << result.startedAt << "frames" << result.frames
            << "timeouts" << result.timeouts << "elapsed_seconds" << result.elapsedSeconds << "}";
    }
    out << "]";
    out.release();
    std::filesystem::rename(temporary, target);
}
