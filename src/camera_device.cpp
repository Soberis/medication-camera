#include "camera_device.hpp"

#include <iostream>
#include <opencv2/opencv.hpp>


CameraDevice::CameraDevice(
    std::shared_ptr<ob::Device> device,
    const std::string& sn,
    const std::string& filename
)
:
device(device),
sn(sn),
filename(filename)
{

}


bool CameraDevice::startRecord()
{

    try
    {

        pipeline =
            std::make_shared<ob::Pipeline>(device);


        auto config =
            std::make_shared<ob::Config>();


        /*
         * RGB
         * 1920x1080 MJPG
         */
        auto colorProfiles =
            pipeline->getStreamProfileList(
                OB_SENSOR_COLOR
            );


        auto colorProfile =
            colorProfiles->getVideoStreamProfile(
                1920,
                1080,
                OB_FORMAT_MJPG,
                30
            );


        config->enableStream(
            colorProfile
        );


        std::cout
            << "[" << sn << "] "
            << "starting pipeline"
            << std::endl;



        pipeline->start(config);



        writer.open(
            filename,
            cv::VideoWriter::fourcc(
                'm',
                'p',
                '4',
                'v'
            ),
            30,
            cv::Size(
                1920,
                1080
            )
        );


        if(!writer.isOpened())
        {
            std::cerr
                << "["
                << sn
                << "] video writer open failed"
                << std::endl;

            return false;
        }



        std::cout
            << "[" << sn << "] "
            << "record start -> "
            << filename
            << std::endl;



        /*
         * 当前先测试单相机稳定录像
         */
        int frameCount = 0;


        while(frameCount < 300)
        {


            auto frameset =
                pipeline->waitForFrameset(
                    1000
                );


            if(!frameset)
            {
                std::cout
                    << "["
                    << sn
                    << "] timeout"
                    << std::endl;

                continue;
            }



            auto colorFrame =
                frameset->getFrame(
                    OB_FRAME_COLOR
                );


            if(!colorFrame)
            {
                continue;
            }



            auto color =
                colorFrame->as<
                    ob::ColorFrame
                >();



            /*
             * Gemini335 RGB MJPG
             * 转 OpenCV Mat
             */
            cv::Mat image(
                color->getHeight(),
                color->getWidth(),
                CV_8UC3,
                color->getData()
            );


            if(image.empty())
            {
                std::cerr
                    << "["
                    << sn
                    << "] empty image"
                    << std::endl;

                continue;
            }



            writer.write(
                image
            );


            frameCount++;



            if(frameCount % 30 == 0)
            {
                std::cout
                    << "["
                    << sn
                    << "] frame "
                    << frameCount
                    << std::endl;
            }

        }



        pipeline->stop();


        writer.release();



        std::cout
            << "["
            << sn
            << "] record finished"
            << std::endl;



        return true;

    }
    catch(ob::Error &e)
    {

        std::cerr
            << "[ORBBEC ERROR]"
            << std::endl
            << e.what()
            << std::endl;


        return false;
    }

}
