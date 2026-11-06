#!/bin/zsh
# Downloads every model into models/ (about 3.4 GB, most of it Whisper large-v3).
# Skips anything already there. Set MB_WHISPER_MODEL instead if you already have ggml-large-v3.bin.
set -e
cd "${0:A:h}/../models" 2>/dev/null || { mkdir -p "${0:A:h}/../models"; cd "${0:A:h}/../models"; }
S=https://github.com/k2-fsa/sherpa-onnx/releases/download
get() { [ -e "$2" ] && echo "have $2" || { echo "getting $2"; curl -fL --progress-bar -o "$2.part" "$1" && mv "$2.part" "$2"; }; }
untar() { [ -d "$2" ] && echo "have $2" || { echo "getting $2"; curl -fL --progress-bar "$1" | tar xj; }; }

untar $S/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2 sherpa-onnx-pyannote-segmentation-3-0
get   $S/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx \
      3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx
get   $S/asr-models/silero_vad.onnx silero_vad.onnx
untar $S/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2 sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
rm -f sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.onnx   # only the int8 model is used
get   https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin ggml-silero-v5.1.2.bin
[ -n "$MB_WHISPER_MODEL" ] || get https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin ggml-large-v3.bin
echo "models ready"
