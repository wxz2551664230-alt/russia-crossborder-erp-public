# External image-processing runtime

Third-party executables, DLLs and model binaries are excluded from this public source snapshot. Obtain the matching Windows Real-ESRGAN ncnn Vulkan runtime and model from its publisher, follow the upstream license, and place the required files here before building the desktop package:

- realesrgan-ncnn-vulkan.exe
- vcomp140.dll
- models/realesrgan-x4plus.bin
- models/realesrgan-x4plus.param

Upstream: https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan

Without this runtime, image upscaling and the complete desktop package build are not available; the ERP source remains inspectable and the web/backend can be built separately.
