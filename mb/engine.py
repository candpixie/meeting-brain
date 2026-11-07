"""Speech engines: VAD, diarization change points, voiceprints, clustering, ASR.

Design: utterances come from VAD (pauses) and are further cut wherever pyannote
sees a speaker change, which catches interruptions with no gap. Speakers are
then assigned per utterance by clustering voiceprints ourselves, so enrolled
people can be matched and corrected clusters can teach the voiceprints.
"""
import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import sherpa_onnx as so
import soundfile as sf
from opencc import OpenCC
from scipy.cluster.hierarchy import fcluster, linkage

from . import config

_cc = {}


def convert(text, preset):
    """Script conversion for the meeting language (SenseVoice and Whisper both mix scripts)."""
    if not preset["opencc"]:
        return text
    if preset["opencc"] not in _cc:
        _cc[preset["opencc"]] = OpenCC(preset["opencc"])
    return _cc[preset["opencc"]].convert(text)


@dataclass
class Utt:
    start: float
    end: float
    samples: np.ndarray = field(repr=False)
    cluster: int = -1
    overlap: bool = False
    emb: np.ndarray | None = field(default=None, repr=False)
    text: str = ""
    lang: str = ""
    alt: str = ""

    @property
    def dur(self):
        return self.end - self.start


# ---------- models (loaded once) ----------

@lru_cache(None)
def _vad_config():
    c = so.VadModelConfig()
    c.silero_vad.model = str(config.VAD)
    c.silero_vad.threshold = 0.5
    c.silero_vad.min_silence_duration = 0.3
    c.silero_vad.min_speech_duration = 0.25
    c.silero_vad.max_speech_duration = 12
    c.sample_rate = config.SR
    return c


@lru_cache(None)
def _extractor():
    return so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(model=str(config.EMBEDDING), num_threads=config.THREADS))


@lru_cache(None)
def _diarizer():
    cfg = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(config.SEGMENTATION)), num_threads=config.THREADS),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=str(config.EMBEDDING), num_threads=config.THREADS),
        clustering=so.FastClusteringConfig(num_clusters=-1, threshold=0.3),
        min_duration_on=0.3, min_duration_off=0.3)
    return so.OfflineSpeakerDiarization(cfg)


@lru_cache(None)
def _sensevoice():
    d = config.SENSEVOICE_DIR
    return so.OfflineRecognizer.from_sense_voice(
        model=str(d / "model.int8.onnx"), tokens=str(d / "tokens.txt"),
        language="auto", use_itn=True, num_threads=config.THREADS)


# ---------- segmentation ----------

def vad_utterances(a):
    c = _vad_config()
    vad = so.VoiceActivityDetector(c, buffer_size_in_seconds=max(60, len(a) / config.SR + 10))
    w = c.silero_vad.window_size
    out = []

    def drain():
        while not vad.empty():
            f = vad.front
            s = f.start / config.SR
            x = np.array(f.samples, dtype=np.float32)
            out.append(Utt(s, s + len(x) / config.SR, x))
            vad.pop()

    for i in range(0, len(a), w):
        vad.accept_waveform(a[i:i + w])
        drain()
    vad.flush()
    drain()
    return out


def change_points(a):
    """Speaker-change times + overlapped regions from pyannote segmentation."""
    segs = sorted(((s.start, s.end, s.speaker) for s in _diarizer().process(a).sort_by_start_time()))
    changes, overlaps = [], []
    for (s1, e1, k1), (s2, e2, k2) in zip(segs, segs[1:]):
        if k1 != k2:
            changes.append(s2 if s2 >= e1 else (s2 + min(e1, e2)) / 2)
    for i, (s1, e1, k1) in enumerate(segs):
        for s2, e2, k2 in segs[i + 1:]:
            if s2 >= e1:
                break
            if k1 != k2 and min(e1, e2) - s2 > 0.2:
                overlaps.append((s2, min(e1, e2)))
    return changes, overlaps


def split_at(utts, changes, overlaps, a):
    out = []
    for u in utts:
        cuts = [t for t in changes if u.start + 0.6 < t < u.end - 0.6]
        bounds = [u.start, *cuts, u.end]
        for s, e in zip(bounds, bounds[1:]):
            x = a[int(s * config.SR):int(e * config.SR)]
            v = Utt(s, e, x)
            v.overlap = any(min(e, oe) - max(s, os_) > 0.2 for os_, oe in overlaps)
            out.append(v)
    return out


# ---------- voiceprints ----------

def embed(x):
    ex = _extractor()
    st = ex.create_stream()
    st.accept_waveform(config.SR, x)
    st.input_finished()
    e = np.array(ex.compute(st), dtype=np.float32)
    return e / (np.linalg.norm(e) + 1e-9)


def norm(v):
    return v / (np.linalg.norm(v) + 1e-9)


def cluster(utts, num_speakers=None, threshold=None):
    threshold = config.CLUSTER_THRESHOLD if threshold is None else threshold
    longs = [u for u in utts if u.dur >= config.MIN_EMBED_SEC]
    for u in longs:
        u.emb = embed(u.samples)
    if not longs:  # very short recording: everyone is one speaker
        for u in utts:
            u.cluster = 0
        return {}
    E = np.stack([u.emb for u in longs])
    D = np.array([u.dur for u in longs])
    if len(longs) == 1:
        labels = np.array([1])
    else:
        # Real recordings give a long tail of tiny clusters (135 on a 52 min cafe meeting at 0.5).
        # Cluster loosely, then fold small clusters into their nearest big one, then, if the head
        # count is known, keep merging the two most similar speakers. maxclust on the raw tree
        # doesn't work: it spends clusters on outlier clips.
        Z = linkage(E, method="average", metric="cosine")
        min_sec = min(max(20.0, 0.02 * D.sum()), 0.1 * D.sum())
        labels = _merge(E, D, fcluster(Z, t=1 - threshold, criterion="distance"), min_sec, target=num_speakers)
        # Head count given but similar voices fused: cut the tree stricter until there are enough
        # people, then merge back down to exactly that many.
        t = threshold
        while num_speakers and len(set(labels)) < num_speakers and t < 0.9:
            t += 0.05
            labels = _merge(E, D, fcluster(Z, t=1 - t, criterion="distance"), min_sec, target=num_speakers)
    cents = {k: norm(E[labels == k].mean(0)) for k in set(labels)}
    # one refinement pass: move each utterance to its nearest centroid
    for u in longs:
        u.cluster = int(max(cents, key=lambda k: float(u.emb @ cents[k])))
    cents = {k: norm(np.stack([u.emb for u in longs if u.cluster == k]).mean(0))
             for k in {u.cluster for u in longs}}
    # short utterances: nearest centroid if confident, else previous speaker
    prev = None
    for u in utts:
        if u.emb is None:
            e = embed(u.samples) if u.dur > 0.4 else None
            best = max(cents, key=lambda k: float(e @ cents[k])) if e is not None else None
            if best is not None and float(e @ cents[best]) > 0.35:
                u.cluster = int(best)
            else:
                u.cluster = prev if prev is not None else int(next(iter(cents)))
        prev = u.cluster
    return cents


def _merge(E, D, labels, min_sec, target=None):
    labels = labels.copy()
    while len(set(labels)) > 1:
        ks = sorted(set(labels))
        secs = {k: D[labels == k].sum() for k in ks}
        C = {k: norm(E[labels == k].mean(0)) for k in ks}
        small = [k for k in ks if secs[k] < min_sec]
        if small and len(small) < len(ks) and not (target and len(ks) <= target):
            k = min(small, key=secs.get)
            into = max((b for b in ks if b not in small), key=lambda b: float(C[k] @ C[b]))
        elif target and len(ks) > target:
            _, k, into = max((float(C[a] @ C[b]), a, b) for i, a in enumerate(ks) for b in ks[i + 1:])
        else:
            break
        labels[labels == k] = into
    return labels


def match_people(cents, people):
    """Greedy one-to-one match of clusters to enrolled people. people: [(id, name, vec)]."""
    pairs = sorted(((float(c @ p[2]), k, p[0]) for k, c in cents.items() for p in people if p[2] is not None),
                   reverse=True)
    used_k, used_p, out = set(), set(), {}
    for s, k, pid in pairs:
        if s < config.ENROLL_MATCH or k in used_k or pid in used_p:
            continue
        out[k] = (pid, s)
        used_k.add(k)
        used_p.add(pid)
    return out


# ---------- ASR ----------

def sensevoice(utts, preset):
    rec = _sensevoice()
    for b in range(0, len(utts), 32):  # batching everything at once ate ~8 GB on a 52 min meeting
        batch = utts[b:b + 32]
        streams = []
        for u in batch:
            st = rec.create_stream()
            st.accept_waveform(config.SR, u.samples)
            streams.append(st)
        rec.decode_streams(streams)
        for u, st in zip(batch, streams):
            u.text = convert(st.result.text.strip(), preset)
            u.lang = st.result.lang.strip("<|>")


def whisper_prompt(vocab, preset):
    # style primer only: shows English words staying English mid-sentence. Kept unrelated to real content.
    sep = "、" if preset["whisper"] in ("yue", "zh", "ja") else ", "
    terms = sep.join(v["term"] for v in vocab[:60])
    return (preset["prompt"] + (" " + preset["terms"].format(terms) if terms else "")).strip()


def whisper(a, utts, vocab, preset, field_name="text"):
    """Whisper large-v3 forced to the meeting language with a mixed-language prompt. Runs once on the
    whole file (model load is the slow part) and maps segments onto utterances by midpoint."""
    if not config.WHISPER_MODEL.exists():
        raise FileNotFoundError(f"Whisper model missing: {config.WHISPER_MODEL}")
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        sf.write(wav, a, config.SR)
        subprocess.run([config.WHISPER_BIN, "-m", str(config.WHISPER_MODEL), "-f", str(wav), "-l", preset["whisper"],
                        *(["--prompt", p, "--carry-initial-prompt"] if (p := whisper_prompt(vocab, preset)) else []), "-sns",
                        *(["--vad", "-vm", str(config.WHISPER_VAD)] if config.WHISPER_VAD.exists() else []),
                        "-oj", "-of", str(Path(td) / "out"), "-np"], check=True, capture_output=True)
        out = json.loads((Path(td) / "out.json").read_text())
        segs = out["transcription"]
        detected = out.get("result", {}).get("language") or preset["whisper"]
    for u in utts:
        setattr(u, field_name, "")
    spans = []
    for s in segs:
        t0, t1 = s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000
        mid = (t0 + t1) / 2
        txt = convert(s["text"].strip(), preset)
        if not txt or not utts:
            continue
        spans.append((t0, t1))
        u = min(utts, key=lambda u: 0 if u.start <= mid <= u.end else min(abs(mid - u.start), abs(mid - u.end)))
        setattr(u, field_name, (getattr(u, field_name) + " " + txt).strip())
    if field_name == "text":
        for u in utts:
            u.lang = detected
    _fill_gaps(utts, preset, field_name, spans)


def _covered(u, spans):
    # share of the utterance that some first-pass segment already spoke for
    hit = sum(max(0.0, min(u.end, t1) - max(u.start, t0)) for t0, t1 in spans)
    return hit / max(u.dur, 1e-6)


def _allowed_langs(preset):
    w = preset["whisper"]
    if w == "auto":
        return None
    langs = {w, "en"}
    if w in ("yue", "zh"):
        langs |= {"yue", "zh"}
    return langs


def _fill_gaps(utts, preset, field_name, spans=()):
    """Whisper on the whole file sometimes skips a sentence in a second language outright (an English
    reply in a Spanish meeting vanished in testing). Redo just those utterances, language auto-detected,
    in one run so the model loads once."""
    # Only audio the first pass never covered. An empty utterance whose words went to its neighbour is not
    # a gap, and redoing it duplicates the line. Results in other languages are hallucinations on short
    # clips (a Vietnamese YouTube outro turned up in testing), so they're dropped.
    gaps = [u for u in utts if not getattr(u, field_name) and u.dur >= 1.0 and _covered(u, spans) < 0.3]
    if not gaps:
        return
    with tempfile.TemporaryDirectory() as td:
        files = []
        for i, u in enumerate(gaps):
            f = Path(td) / f"g{i}.wav"
            sf.write(f, u.samples, config.SR)
            files.append(f)
        subprocess.run([config.WHISPER_BIN, "-m", str(config.WHISPER_MODEL), "-l", "auto", "-sns", "-np", "-oj",
                        *[x for f in files for x in ("-f", str(f))]], check=True, capture_output=True, cwd=td)
        for u, f in zip(gaps, files):
            j = f.with_name(f.name + ".json")
            if not j.exists():
                continue
            out = json.loads(j.read_text())
            txt = " ".join(x["text"].strip() for x in out["transcription"]).strip()
            lang = out.get("result", {}).get("language")
            ok = _allowed_langs(preset)
            if ok is not None and lang not in ok:
                continue
            if txt:
                setattr(u, field_name, convert(txt, preset))
                if field_name == "text":
                    u.lang = lang or u.lang
