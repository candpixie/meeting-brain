"""Paths and tunables. Override any of these with env vars (MB_<NAME>)."""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
DATA = Path(os.environ.get("MB_DATA", ROOT / "data"))
AUDIO = DATA / "audio"        # compressed opus, auto-deleted after RETENTION_DAYS
TRAIN = DATA / "train"        # clips you corrected, kept for fine-tuning
NOTES = DATA / "notes"        # markdown export, one file per meeting (gbrain-ready)
DB = DATA / "brain.db"

SEGMENTATION = MODELS / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMBEDDING = MODELS / "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
VAD = MODELS / "silero_vad.onnx"
SENSEVOICE_DIR = MODELS / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
WHISPER_BIN = os.environ.get("MB_WHISPER_BIN") or shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
WHISPER_MODEL = Path(os.environ.get("MB_WHISPER_MODEL", MODELS / "ggml-large-v3.bin"))
WHISPER_VAD = MODELS / "ggml-silero-v5.1.2.bin"
FFMPEG = "/opt/homebrew/bin/ffmpeg"

OLLAMA_URL = os.environ.get("MB_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("MB_OLLAMA_MODEL", "qwen2.5:7b")         # cleanup, notes, speaker reasoning
OLLAMA_FAST_MODEL = os.environ.get("MB_OLLAMA_FAST_MODEL", "qwen2.5:7b")  # fallback if the main one is missing/too big
# A 17 GB model on a 24 GB Mac with a full disk crashed the machine (2026-10-20). Models bigger than
# this share of RAM are refused unless MB_ALLOW_BIG_MODEL=1.
MAX_MODEL_RAM_SHARE = 0.4
ALLOW_BIG_MODEL = os.environ.get("MB_ALLOW_BIG_MODEL") == "1"
REASON_SPEAKERS = os.environ.get("MB_REASON_SPEAKERS", "0") == "1"      # experimental LLM speaker hints

SR = 16000
RETENTION_DAYS = float(os.environ.get("MB_RETENTION_DAYS", 7))
KEEP_TRAINING_CLIPS = os.environ.get("MB_KEEP_TRAINING_CLIPS", "1") == "1"

# Speaker tuning. Cosine similarity between voiceprints (CAM++ zh/en model).
CLUSTER_THRESHOLD = float(os.environ.get("MB_CLUSTER_THRESHOLD", 0.4))  # real meeting: 0.25-0.4 all gave 6 speakers. 0.3 merged two TTS voices
ENROLL_MATCH = float(os.environ.get("MB_ENROLL_MATCH", 0.55))          # cluster -> known person
MIN_EMBED_SEC = 1.0  # shorter utterances are too noisy to cluster, they borrow a neighbour's label

# CPU threads for the speech models. MB_THREADS=2 (or `--quiet`) keeps the fans down at ~1.5x the time.
THREADS = int(os.environ.get("MB_THREADS", 4))

LANG = os.environ.get("MB_LANG", "yue")  # default meeting language, see mb/langs.py
PORT = int(os.environ.get("MB_PORT", 4470))

for d in (DATA, AUDIO, TRAIN, NOTES):
    d.mkdir(parents=True, exist_ok=True)
