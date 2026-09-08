#include "../include/camera_manager.hpp"

#include <iostream>


CameraManager::CameraManager()
{

}


std::vector<CameraInfo> CameraManager::scan()
{

    std::vector<CameraInfo> result;


    auto list = ctx.queryDeviceList();


    uint32_t count = list->getCount();


    std::cout
        << "[INFO] cameras found: "
        << count
        << std::endl;


    for(uint32_t i = 0; i < count; i++)
    {

        auto device = list->getDevice(i);

        auto info = device->getDeviceInfo();


        CameraInfo c;

        c.number = i + 1;

        c.sn = info->getSerialNumber();

        c.device = device;


        std::cout
            << "[CAMERA "
            << c.number
            << "] "
            << c.sn
            << std::endl;


        result.push_back(c);
    }


    return result;
}
