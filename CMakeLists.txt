cmake_minimum_required(VERSION 3.10)

project(medication-camera)


set(CMAKE_CXX_STANDARD 17)


set(ORBBEC_SDK_PATH
/opt/orbbec/OrbbecSDK_v2.9.3_202607151523_2f6561c_linux_arm64
)


find_package(OpenCV REQUIRED)


include_directories(

include

${ORBBEC_SDK_PATH}/include

${OpenCV_INCLUDE_DIRS}

)


link_directories(

${ORBBEC_SDK_PATH}/lib

)


add_executable(

medication_camera

src/main.cpp

src/camera_manager.cpp

src/camera_device.cpp

src/manifest.cpp

)


target_link_libraries(

medication_camera

OrbbecSDK

${OpenCV_LIBS}

pthread

)
