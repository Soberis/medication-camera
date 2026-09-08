#include "manifest.hpp"

#include <fstream>


void generateManifest(
    const std::vector<VideoInfo>& videos
)
{

    std::ofstream out(
        "../output/manifest.json"
    );


    out << "{\n";
    out << "\"schema_version\":\"external-video-ingest-v1\",\n";
    out << "\"videos\":[\n";


    for(size_t i = 0; i < videos.size(); i++)
    {

        out << "{\n";

        out << "\"camera_number\":"
            << videos[i].camera_number
            << ",\n";


        out << "\"file_name\":\""
            << videos[i].file
            << "\"\n";


        out << "}";


        if(i + 1 < videos.size())
            out << ",";


        out << "\n";
    }


    out << "]\n";
    out << "}\n";

}
