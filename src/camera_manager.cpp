#include "camera_manager.hpp"
#include <map>
#include <stdexcept>

std::vector<std::shared_ptr<CameraDevice>> CameraManager::open(
    const std::vector<CameraConfig>& cameras, const std::string& directory) {
    auto list = context.queryDeviceList();
    std::map<std::string, std::shared_ptr<ob::Device>> devices;
    for (uint32_t i = 0; i < list->getCount(); ++i) {
        auto device = list->getDevice(i);
        devices.emplace(device->getDeviceInfo()->getSerialNumber(), device);
    }
    std::vector<std::shared_ptr<CameraDevice>> result;
    for (const auto& camera : cameras) {
        auto found = devices.find(camera.serial);
        if (found == devices.end())
            throw std::runtime_error("Configured camera missing: " + camera.serial);
        result.push_back(std::make_shared<CameraDevice>(found->second, camera, directory));
    }
    return result;
}
