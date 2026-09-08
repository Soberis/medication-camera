#pragma once
#include "camera_device.hpp"
#include <vector>

class CameraManager {
public:
    std::vector<std::shared_ptr<CameraDevice>> open(
        const std::vector<CameraConfig>& cameras, const std::string& directory);
private:
    ob::Context context;
};
