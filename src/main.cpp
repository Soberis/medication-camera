#include "camera_manager.hpp"
#include "camera_device.hpp"
#include "manifest.hpp"

#include <iostream>
#include <filesystem>
#include <vector>
#include <memory>


int main()
{

    try
    {

        std::filesystem::create_directories(
            "../output"
        );


        CameraManager manager;


        auto cameras =
            manager.scan();



        std::vector<VideoInfo> videos;


        std::vector<std::shared_ptr<CameraDevice>> devices;



        for(auto &c : cameras)
        {


            std::cout
            << "[CAMERA "
            << c.number
            << "] "
            << c.sn
            << std::endl;



            std::string file =

                "../output/camera_" +

                std::to_string(c.number) +

                ".mp4";



            auto cam =

                std::make_shared<CameraDevice>(

                    c.device,

                    c.sn,

                    file

                );



            devices.push_back(cam);



            VideoInfo info;


            info.camera_number = c.number;


            info.file =

                "camera_" +

                std::to_string(c.number) +

                ".mp4";



            videos.push_back(info);

        }



        generateManifest(
            videos
        );



        std::cout
        << "manifest generated"
        << std::endl;



        /*
         * 开始录像
         */

        for(auto &cam : devices)
        {

            if(!cam->startRecord())
            {

                std::cerr
                << "camera start failed"
                << std::endl;

            }

        }



        return 0;


    }

    catch(std::exception &e)
    {

        std::cerr
        << "[ERROR] "
        << e.what()
        << std::endl;


        return -1;

    }

}
