#pragma once
#include "camera_device.hpp"
#include <vector>

std::string utcNow();
void writeRecordingResult(const std::string& directory,
                          const std::vector<RecordingResult>& results);
