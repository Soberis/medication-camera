#pragma once

#include <string>
#include <vector>


struct VideoInfo
{
    int camera_number;

    std::string file;

    long size;
};



class Manifest
{

public:

    static void save(

        const std::string& path,

        const std::vector<VideoInfo>& videos

    );

};
