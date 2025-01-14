// Copyright (c) OpenMMLab. All rights reserved
#include "trt_bicubic_interpolate.hpp"

#include <assert.h>

#include <chrono>

#include "trt_bicubic_interpolate_kernel.hpp"
#include "trt_plugin_helper.hpp"
#include "trt_serialize.hpp"
using namespace nvinfer1;

namespace mmdeploy {
namespace {
static const char *PLUGIN_VERSION{"1"};
static const char *PLUGIN_NAME{"TRTBicubicInterpolate"};
}  // namespace

TRTBicubicInterpolate::TRTBicubicInterpolate(const std::string &name,
                                             std::vector<int> output_size, std::vector<float> scale_factor, bool align_corners)
    : TRTPluginBase(name), mOutputSize(output_size), mScaleFactor(scale_factor), mAlignCorners(align_corners) {}

TRTBicubicInterpolate::TRTBicubicInterpolate(const std::string name, const void *data,
                                             size_t length)
    : TRTPluginBase(name) {
  deserialize_value(&data, &length, &mOutputSize);
  deserialize_value(&data, &length, &mScaleFactor);
  deserialize_value(&data, &length, &mAlignCorners);
}

nvinfer1::IPluginV2DynamicExt *TRTBicubicInterpolate::clone() const TRT_NOEXCEPT {
  TRTBicubicInterpolate *plugin =
      new TRTBicubicInterpolate(mLayerName, mOutputSize, mScaleFactor, mAlignCorners);
  plugin->setPluginNamespace(getPluginNamespace());

  return plugin;
}

nvinfer1::DimsExprs TRTBicubicInterpolate::getOutputDimensions(
    int outputIndex, const nvinfer1::DimsExprs *inputs, int nbInputs,
    nvinfer1::IExprBuilder &exprBuilder) TRT_NOEXCEPT {
  nvinfer1::DimsExprs ret;
  ret.nbDims = 4;
  ret.d[0] = inputs[0].d[0];
  ret.d[1] = inputs[0].d[1];
  ret.d[2] = exprBuilder.constant(mOutputSize[0]);
  ret.d[3] = exprBuilder.constant(mOutputSize[1]);
  return ret;
}

bool TRTBicubicInterpolate::supportsFormatCombination(int pos,
                                                      const nvinfer1::PluginTensorDesc *ioDesc,
                                                      int nbInputs, int nbOutputs) TRT_NOEXCEPT {
  if (pos == 0) {
    return (ioDesc[pos].type == nvinfer1::DataType::kFLOAT &&
            ioDesc[pos].format == nvinfer1::TensorFormat::kLINEAR);

  } else {
    return ioDesc[pos].type == ioDesc[0].type && ioDesc[pos].format == ioDesc[0].format;
  }
}

void TRTBicubicInterpolate::configurePlugin(const nvinfer1::DynamicPluginTensorDesc *inputs,
                                            int nbInputs,
                                            const nvinfer1::DynamicPluginTensorDesc *outputs,
                                            int nbOutputs) TRT_NOEXCEPT {}

size_t TRTBicubicInterpolate::getWorkspaceSize(const nvinfer1::PluginTensorDesc *inputs,
                                               int nbInputs,
                                               const nvinfer1::PluginTensorDesc *outputs,
                                               int nbOutputs) const TRT_NOEXCEPT {
  return 0;
}

int TRTBicubicInterpolate::enqueue(const nvinfer1::PluginTensorDesc *inputDesc,
                                   const nvinfer1::PluginTensorDesc *outputDesc,
                                   const void *const *inputs, void *const *outputs, void *workSpace,
                                   cudaStream_t stream) TRT_NOEXCEPT {
  int batch = inputDesc[0].dims.d[0];
  int channels = inputDesc[0].dims.d[1];
  int height = inputDesc[0].dims.d[2];
  int width = inputDesc[0].dims.d[3];

  int height_out = outputDesc[0].dims.d[2];
  int width_out = outputDesc[0].dims.d[3];
  const void *x = inputs[0];
  void *output = outputs[0];

  float height_scale = mScaleFactor[0];
  float width_scale = mScaleFactor[1];
  // TODO: add fp16 support
  auto data_type = inputDesc[0].type;
  switch (data_type) {
    case nvinfer1::DataType::kFLOAT:
      bicubic_interpolate<float>((float *)x, (float *)output, batch, channels, height, width,
                                 height_out, width_out, height_scale, width_scale, mAlignCorners, stream);
      break;
    default:
      return 1;
      break;
  }

  return 0;
}

nvinfer1::DataType TRTBicubicInterpolate::getOutputDataType(int index,
                                                            const nvinfer1::DataType *inputTypes,
                                                            int nbInputs) const TRT_NOEXCEPT {
  return inputTypes[0];
}

// IPluginV2 Methods
const char *TRTBicubicInterpolate::getPluginType() const TRT_NOEXCEPT { return PLUGIN_NAME; }

const char *TRTBicubicInterpolate::getPluginVersion() const TRT_NOEXCEPT { return PLUGIN_VERSION; }

int TRTBicubicInterpolate::getNbOutputs() const TRT_NOEXCEPT { return 1; }

size_t TRTBicubicInterpolate::getSerializationSize() const TRT_NOEXCEPT {
  return serialized_size(mOutputSize) + serialized_size(mScaleFactor) + serialized_size(mAlignCorners);
}

void TRTBicubicInterpolate::serialize(void *buffer) const TRT_NOEXCEPT {
  serialize_value(&buffer, mOutputSize);
  serialize_value(&buffer, mScaleFactor);
  serialize_value(&buffer, mAlignCorners);
}

////////////////////// creator /////////////////////////////

TRTBicubicInterpolateCreator::TRTBicubicInterpolateCreator() {
  mPluginAttributes.clear();
  mPluginAttributes.emplace_back(nvinfer1::PluginField("output_size"));
  mPluginAttributes.emplace_back(nvinfer1::PluginField("scale_factor"));
  mPluginAttributes.emplace_back(nvinfer1::PluginField("align_corners"));
  mFC.nbFields = mPluginAttributes.size();
  mFC.fields = mPluginAttributes.data();
}

const char *TRTBicubicInterpolateCreator::getPluginName() const TRT_NOEXCEPT { return PLUGIN_NAME; }

const char *TRTBicubicInterpolateCreator::getPluginVersion() const TRT_NOEXCEPT {
  return PLUGIN_VERSION;
}

nvinfer1::IPluginV2DynamicExt *TRTBicubicInterpolateCreator::createPlugin(
    const char *name, const nvinfer1::PluginFieldCollection *fc) TRT_NOEXCEPT {
  nvinfer1::Dims size{2, {1, 1}};
  std::vector<int> output_size;
  std::vector<float> scale_factor;
  bool align_corners = 1;

  for (int i = 0; i < fc->nbFields; i++) {
    if (fc->fields[i].data == nullptr) {
      continue;
    }
    std::string field_name(fc->fields[i].name);

    if (field_name.compare("output_size") == 0) {
      int data_size = (fc->fields[i].length);
      if (data_size != 2) {
        data_size = data_size / sizeof(int);
      }
      ASSERT(data_size == 2)
      const int *data_start = static_cast<const int *>(fc->fields[i].data);
      output_size = std::vector<int>(data_start, data_start + data_size);
    }

    if (field_name.compare("scale_factor") == 0) {
      int data_size = (fc->fields[i].length);
      if (data_size != 2) {
        data_size = data_size / sizeof(float);
      }
      ASSERT(data_size == 2)
      const float *data_start = static_cast<const float *>(fc->fields[i].data);
      scale_factor = std::vector<float>(data_start, data_start + data_size);
    }

    if (field_name.compare("align_corners") == 0) {
      align_corners = static_cast<const int *>(fc->fields[i].data)[0];
    }
  }

  TRTBicubicInterpolate *plugin = new TRTBicubicInterpolate(name, output_size, scale_factor, align_corners);
  plugin->setPluginNamespace(getPluginNamespace());
  return plugin;
}

nvinfer1::IPluginV2DynamicExt *TRTBicubicInterpolateCreator::deserializePlugin(
    const char *name, const void *serialData, size_t serialLength) TRT_NOEXCEPT {
  auto plugin = new TRTBicubicInterpolate(name, serialData, serialLength);
  plugin->setPluginNamespace(getPluginNamespace());
  return plugin;
}
REGISTER_TENSORRT_PLUGIN(TRTBicubicInterpolateCreator);
}  // namespace mmdeploy
