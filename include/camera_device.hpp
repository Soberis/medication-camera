#pragma once

#include <libobsensor/ObSensor.hpp>

#include <opencv2/opencv.hpp>

#include <memory>
#include <string>


class CameraDevice
{

public:


    CameraDevice(

        std::shared_ptr<ob::Device> dev,

        const std::string& sn,

        const std::string& filename

    );


    bool startRecord();


    void stop();



private:


    std::shared_ptr<ob::Device> device;


    std::string serial;


    std::string output;


    cv::VideoWriter writer;


    bool running = false;


    int frameCount = 0;


};
