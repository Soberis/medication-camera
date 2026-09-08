#pragma once

#include <string>
#include <vector>


struct VideoInfo
{
    int camera_number;
    std::string file;
};


void generateManifest(
    const std::vector<VideoInfo>& videos
);
