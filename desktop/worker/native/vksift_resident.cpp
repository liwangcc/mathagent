// Persistent VulkanSift extractor for MathAgent Windows workers.
// ABI is pinned to maelaubert/VulkanSift commit
// 04245a5e194c9c1aa2ac07c2d257eedad3c8699d.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <fcntl.h>
#include <io.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>

namespace {

enum Result : int32_t {
  kSuccess = 0,
  kInvalidInput = 1,
  kVulkanError = 2,
};

struct ExternalWindowInfo {
  void* context;
  void* window;
};

struct Config {
  uint32_t input_image_max_size;
  uint32_t sift_buffer_count;
  uint32_t max_nb_sift_per_buffer;
  bool use_input_upsampling;
  uint8_t nb_octaves;
  uint8_t nb_scales_per_octave;
  float input_image_blur_level;
  float seed_scale_sigma;
  float intensity_threshold;
  float edge_threshold;
  uint32_t max_nb_orientation_per_keypoint;
  int32_t descriptor_format;
  int32_t gpu_device_index;
  bool use_hardware_interpolated_blur;
  int32_t pyramid_precision_mode;
  void (*on_error_callback_function)(Result);
  bool use_gpu_debug_functions;
  ExternalWindowInfo gpu_debug_external_window_info;
};

struct Feature {
  float x;
  float y;
  float scale_x;
  float scale_y;
  uint32_t scale_idx;
  int32_t octave_idx;
  float sigma;
  float orientation;
  float intensity;
  uint8_t descriptor[128];
};

using GpuName = char[256];

#pragma pack(push, 1)
struct RequestHeader {
  char magic[4];
  uint32_t version;
  uint32_t width;
  uint32_t height;
  uint32_t payload_size;
};

struct ResponseHeader {
  char magic[4];
  uint32_t version;
  uint32_t status;
  uint32_t feature_count;
  uint32_t native_error;
};

struct MetadataResponse {
  char magic[4];
  uint32_t version;
  uint32_t vendor_id;
  uint32_t device_name_size;
};

struct WireFeature {
  float x;
  float y;
  float sigma;
  float orientation;
  uint8_t descriptor[128];
};
#pragma pack(pop)

static_assert(sizeof(Config) == 88, "unexpected VulkanSift config ABI");
static_assert(sizeof(Feature) == 164, "unexpected VulkanSift feature ABI");
static_assert(sizeof(RequestHeader) == 20, "unexpected request ABI");
static_assert(sizeof(ResponseHeader) == 20, "unexpected response ABI");
static_assert(sizeof(MetadataResponse) == 16, "unexpected metadata ABI");
static_assert(sizeof(WireFeature) == 144, "unexpected wire feature ABI");

constexpr uint32_t kProtocolVersion = 1;
constexpr uint32_t kMaxEdge = 1280;
constexpr uint32_t kMaxPixels = kMaxEdge * kMaxEdge;
constexpr uint32_t kMaxFeatures = 3000;
constexpr uint32_t kStatusOk = 0;
constexpr uint32_t kStatusInvalidRequest = 1;
constexpr uint32_t kStatusNativeError = 2;
constexpr uint32_t kStatusInternalError = 3;
constexpr uint32_t kNvidiaVendorId = 0x10DE;

using Instance = void*;
using LoadVulkanFn = Result (*)();
using UnloadVulkanFn = void (*)();
using GetAvailableGpusFn = void (*)(uint32_t*, GpuName*);
using SetLogLevelFn = void (*)(int32_t);
using GetDefaultConfigFn = Config (*)();
using CreateInstanceFn = Result (*)(Instance*, const Config*);
using DestroyInstanceFn = void (*)(Instance*);
using DetectFeaturesFn = void (*)(Instance, const uint8_t*, uint32_t, uint32_t, uint32_t);
using GetFeaturesNumberFn = uint32_t (*)(Instance, uint32_t);
using DownloadFeaturesFn = void (*)(Instance, Feature*, uint32_t);

std::atomic<int32_t> g_native_error{kSuccess};

void OnNativeError(Result result) {
  g_native_error.store(static_cast<int32_t>(result), std::memory_order_relaxed);
}

template <typename T>
T Resolve(HMODULE module, const char* name) {
  return reinterpret_cast<T>(GetProcAddress(module, name));
}

bool ReadExact(void* output, size_t size) {
  std::cin.read(reinterpret_cast<char*>(output), static_cast<std::streamsize>(size));
  return static_cast<size_t>(std::cin.gcount()) == size;
}

bool WriteResponse(uint32_t status, uint32_t native_error,
                   const std::vector<WireFeature>& features = {}) {
  const ResponseHeader header{{'V', 'K', 'R', 'S'}, kProtocolVersion, status,
                              static_cast<uint32_t>(features.size()), native_error};
  std::cout.write(reinterpret_cast<const char*>(&header), sizeof(header));
  if (!features.empty()) {
    std::cout.write(reinterpret_cast<const char*>(features.data()),
                    static_cast<std::streamsize>(features.size() * sizeof(WireFeature)));
  }
  std::cout.flush();
  return static_cast<bool>(std::cout);
}

bool WriteMetadata(const std::string& device_name) {
  if (device_name.empty() || device_name.size() > 255) {
    return false;
  }
  const MetadataResponse response{{'V', 'K', 'P', 'M'}, kProtocolVersion,
                                  kNvidiaVendorId,
                                  static_cast<uint32_t>(device_name.size())};
  std::cout.write(reinterpret_cast<const char*>(&response), sizeof(response));
  std::cout.write(device_name.data(), static_cast<std::streamsize>(device_name.size()));
  std::cout.flush();
  return static_cast<bool>(std::cout);
}

std::filesystem::path DllPath() {
  std::vector<wchar_t> buffer(32768);
  const DWORD length = GetModuleFileNameW(nullptr, buffer.data(),
                                          static_cast<DWORD>(buffer.size()));
  if (length == 0 || length >= buffer.size()) {
    return {};
  }
  return std::filesystem::path(std::wstring(buffer.data(), length)).parent_path() /
         L"vulkansift.dll";
}

int32_t FindNvidiaGpu(const std::vector<GpuName>& names) {
  for (size_t index = 0; index < names.size(); ++index) {
    std::string name(names[index]);
    std::transform(name.begin(), name.end(), name.begin(), [](unsigned char value) {
      return static_cast<char>(std::tolower(value));
    });
    if (name.find("nvidia") != std::string::npos) {
      return static_cast<int32_t>(index);
    }
  }
  return -1;
}

}  // namespace

int main() {
  _setmode(_fileno(stdin), _O_BINARY);
  _setmode(_fileno(stdout), _O_BINARY);
  std::ios::sync_with_stdio(false);
  std::cin.tie(nullptr);

  const auto dll_path = DllPath();
  if (dll_path.empty()) {
    return 10;
  }
  HMODULE module = LoadLibraryW(dll_path.c_str());
  if (module == nullptr) {
    return 11;
  }

  const auto load_vulkan = Resolve<LoadVulkanFn>(module, "vksift_loadVulkan");
  const auto unload_vulkan = Resolve<UnloadVulkanFn>(module, "vksift_unloadVulkan");
  const auto get_available_gpus =
      Resolve<GetAvailableGpusFn>(module, "vksift_getAvailableGPUs");
  const auto set_log_level = Resolve<SetLogLevelFn>(module, "vksift_setLogLevel");
  const auto get_default_config =
      Resolve<GetDefaultConfigFn>(module, "vksift_getDefaultConfig");
  const auto create_instance =
      Resolve<CreateInstanceFn>(module, "vksift_createInstance");
  const auto destroy_instance =
      Resolve<DestroyInstanceFn>(module, "vksift_destroyInstance");
  const auto detect_features =
      Resolve<DetectFeaturesFn>(module, "vksift_detectFeatures");
  const auto get_features_number =
      Resolve<GetFeaturesNumberFn>(module, "vksift_getFeaturesNumber");
  const auto download_features =
      Resolve<DownloadFeaturesFn>(module, "vksift_downloadFeatures");
  if (!load_vulkan || !unload_vulkan || !get_available_gpus || !set_log_level ||
      !get_default_config || !create_instance || !destroy_instance ||
      !detect_features || !get_features_number || !download_features) {
    FreeLibrary(module);
    return 12;
  }

  set_log_level(0);
  if (load_vulkan() != kSuccess) {
    FreeLibrary(module);
    return 13;
  }

  uint32_t gpu_count = 0;
  get_available_gpus(&gpu_count, nullptr);
  std::vector<GpuName> gpu_names(gpu_count);
  if (gpu_count > 0) {
    get_available_gpus(&gpu_count, gpu_names.data());
  }
  const int32_t nvidia_gpu_index = FindNvidiaGpu(gpu_names);
  if (nvidia_gpu_index < 0) {
    unload_vulkan();
    FreeLibrary(module);
    return 15;
  }
  const std::string selected_gpu_name(gpu_names[nvidia_gpu_index]);

  Config config = get_default_config();
  config.input_image_max_size = kMaxPixels;
  config.sift_buffer_count = 1;
  config.max_nb_sift_per_buffer = kMaxFeatures;
  config.use_input_upsampling = false;
  config.intensity_threshold = 0.04f;
  config.gpu_device_index = nvidia_gpu_index;
  config.on_error_callback_function = OnNativeError;
  Instance instance = nullptr;
  if (create_instance(&instance, &config) != kSuccess || instance == nullptr) {
    unload_vulkan();
    FreeLibrary(module);
    return 14;
  }

  int exit_code = 0;
  while (true) {
    RequestHeader request{};
    if (!ReadExact(&request, sizeof(request))) {
      if (std::cin.eof()) {
        break;
      }
      exit_code = 20;
      break;
    }
    if (request.version != kProtocolVersion) {
      WriteResponse(kStatusInvalidRequest, kInvalidInput);
      exit_code = 21;
      break;
    }
    if (std::memcmp(request.magic, "VKPQ", 4) == 0) {
      if (request.width != 0 || request.height != 0 || request.payload_size != 0 ||
          !WriteMetadata(selected_gpu_name)) {
        exit_code = 21;
        break;
      }
      continue;
    }
    if (std::memcmp(request.magic, "VKRQ", 4) != 0) {
      WriteResponse(kStatusInvalidRequest, kInvalidInput);
      exit_code = 21;
      break;
    }
    if (request.width == 0 && request.height == 0 && request.payload_size == 0) {
      break;
    }
    const uint64_t pixels = static_cast<uint64_t>(request.width) * request.height;
    if (request.width == 0 || request.height == 0 || request.width > kMaxEdge ||
        request.height > kMaxEdge || pixels > kMaxPixels ||
        request.payload_size != pixels) {
      WriteResponse(kStatusInvalidRequest, kInvalidInput);
      continue;
    }
    std::vector<uint8_t> image(request.payload_size);
    if (!ReadExact(image.data(), image.size())) {
      exit_code = 22;
      break;
    }

    g_native_error.store(kSuccess, std::memory_order_relaxed);
    detect_features(instance, image.data(), request.width, request.height, 0);
    int32_t native_error = g_native_error.load(std::memory_order_relaxed);
    if (native_error != kSuccess) {
      WriteResponse(kStatusNativeError, static_cast<uint32_t>(native_error));
      if (native_error == kVulkanError) {
        exit_code = 23;
        break;
      }
      continue;
    }
    const uint32_t count = get_features_number(instance, 0);
    native_error = g_native_error.load(std::memory_order_relaxed);
    if (native_error != kSuccess || count == 0 || count > kMaxFeatures) {
      WriteResponse(kStatusNativeError,
                    static_cast<uint32_t>(native_error != kSuccess
                                              ? native_error
                                              : kInvalidInput));
      continue;
    }
    std::vector<Feature> native_features(count);
    download_features(instance, native_features.data(), 0);
    native_error = g_native_error.load(std::memory_order_relaxed);
    if (native_error != kSuccess) {
      WriteResponse(kStatusNativeError, static_cast<uint32_t>(native_error));
      if (native_error == kVulkanError) {
        exit_code = 24;
        break;
      }
      continue;
    }

    std::vector<WireFeature> wire_features(count);
    for (uint32_t index = 0; index < count; ++index) {
      const Feature& source = native_features[index];
      WireFeature& target = wire_features[index];
      target.x = source.x;
      target.y = source.y;
      target.sigma = source.sigma;
      target.orientation = source.orientation;
      std::memcpy(target.descriptor, source.descriptor, sizeof(target.descriptor));
    }
    if (!WriteResponse(kStatusOk, kSuccess, wire_features)) {
      exit_code = 25;
      break;
    }
  }

  destroy_instance(&instance);
  unload_vulkan();
  FreeLibrary(module);
  return exit_code;
}
