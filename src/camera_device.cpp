#include "camera_device.hpp"

#include <opencv2/opencv.hpp>

#include <iostream>
#include <chrono>
#include <iomanip>


CameraDevice::CameraDevice(
    std::shared_ptr<ob::Device> dev,
    const std::string& sn,
    const std::string& filename
)
    :
    device(dev),
    serial(sn),
    output(filename)
{

}



bool CameraDevice::startRecord()
{
    try
    {

        auto pipeline =
            std::make_shared<ob::Pipeline>(device);


        auto config =
            std::make_shared<ob::Config>();


        /*
         * RGB
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


        /*
         * Depth
         */
        auto depthProfiles =
            pipeline->getStreamProfileList(
                OB_SENSOR_DEPTH
            );


        auto depthProfile =
            depthProfiles->getVideoStreamProfile(
                1280,
                800,
                OB_FORMAT_Y16,
                30
            );


        config->enableStream(
            colorProfile
        );


        config->enableStream(
            depthProfile
        );


        config->setFrameAggregateOutputMode(
            OB_FRAME_AGGREGATE_OUTPUT_ALL_TYPE_FRAME_REQUIRE
        );


        pipeline->enableFrameSync();


        pipeline->start(config);



        /*
         * MP4 writer
         *
         * 使用 MJPG
         * 避免 RK3588 h264_v4l2m2m失败
         */

        writer.open(

            output,

            cv::VideoWriter::fourcc(
                'M',
                'J',
                'P',
                'G'
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
            << "["<<serial<<"] "
            << "video writer open failed "
            << output
            << std::endl;

            pipeline->stop();

            return false;
        }



        std::cout
        << "["<<serial<<"] "
        << "record start -> "
        << output
        << std::endl;



        running=true;



        while(running)
        {

            auto frameset =
                pipeline->waitForFrameset(
                    1000
                );


            if(!frameset)
            {
                continue;
            }



            auto colorRaw =
                frameset->getFrame(
                    OB_FRAME_COLOR
                );


            if(!colorRaw)
            {
                continue;
            }



            auto color =
                colorRaw->as<
                    ob::ColorFrame
                >();



            cv::Mat frame(

                color->getHeight(),

                color->getWidth(),

                CV_8UC3

            );


            /*
             * MJPG转换BGR
             */

            cv::Mat mjpg(

                color->getHeight(),

                color->getWidth(),

                CV_8UC1,

                (void*)color->getData()

            );


            cv::imdecode(

                mjpg,

                cv::IMREAD_COLOR

            ).copyTo(frame);



            if(frame.empty())
            {
                continue;
            }



            writer.write(frame);



            frameCount++;



            if(frameCount%30==0)
            {

                std::cout
                <<"["<<serial<<"] "
                <<"frames="
                <<frameCount
                <<std::endl;

            }


        }


        pipeline->stop();

        writer.release();


        return true;


    }

    catch(ob::Error &e)
    {

        std::cerr
        <<"ORBBEC ERROR "
        <<e.what()
        <<std::endl;

        return false;

    }

}



void CameraDevice::stop()
{

    running=false;

}
