#pragma once

#include <libobsensor/ObSensor.hpp>
#include <vector>
#include <memory>


struct CameraInfo
{
    int number;
    std::string sn;
    std::shared_ptr<ob::Device> device;
};


class CameraManager
{

public:

    CameraManager();

    std::vector<CameraInfo> scan();


private:

    ob::Context ctx;

};
