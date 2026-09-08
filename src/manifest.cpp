#include "../include/manifest.hpp"

#include <fstream>


void Manifest::save(

    const std::string& path,

    const std::vector<VideoInfo>& videos

)
{

    std::ofstream out(path);


    out << "{\n";

    out << "\"schema_version\":\"external-video-ingest-v1\",\n";

    out << "\"source_system\":\"firefly-capture\",\n";

    out << "\"videos\":[\n";


    for(size_t i=0;i<videos.size();i++)
    {

        auto &v = videos[i];


        out << "{\n";

        out << "\"camera_number\":"
            << v.camera_number
            << ",\n";


        out << "\"file_name\":\""
            << v.file
            << "\"\n";


        out << "}";


        if(i+1 < videos.size())
            out << ",";


        out << "\n";
    }


    out << "]\n";

    out << "}\n";
}
