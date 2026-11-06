"""audio file -> speakers + transcript + notes, then the audio is compressed and scheduled for deletion."""
import json
import re
import time
from pathlib import Path

import numpy as np

from . import audio, config, db, engine, langs, llm


_t0 = time.time()


def log(msg):
    print(f"[mb {time.time() - _t0:6.0f}s] {msg}", flush=True)


def people(c):
    return [(r["id"], r["name"], db.vec(r["embedding"])) for r in c.execute("SELECT * FROM people")]


def process(path, title=None, engine_name="whisper", num_speakers=None, use_llm=True,
            keep_audio_days=None, meeting_id=None, lang=None):
    db.init()
    with db.conn() as c:
        lang = lang or db.setting(c, "lang", config.LANG)
    path = Path(path)
    t0 = time.time()
    with db.conn() as c:
        if meeting_id is None:
            meeting_id = c.execute("INSERT INTO meetings(title, created, engine, status, auto_title, lang) "
                                   "VALUES (?,?,?,?,?,?)", (title or path.stem, path.stat().st_mtime, engine_name,
                                                            "processing", int(title is None), lang)).lastrowid
        else:
            c.execute("UPDATE meetings SET status='processing', engine=?, lang=? WHERE id=?",
                      (engine_name, lang, meeting_id))
    try:
        _run(meeting_id, path, engine_name, num_speakers, use_llm, keep_audio_days, langs.get(lang))
    except Exception as e:
        with db.conn() as c:
            c.execute("UPDATE meetings SET status='error', error=? WHERE id=?", (repr(e), meeting_id))
        raise
    log(f"done in {time.time() - t0:.0f}s -> meeting {meeting_id}")
    return meeting_id


def _run(mid, path, engine_name, num_speakers, use_llm, keep_audio_days, preset):
    log("loading audio")
    a = audio.load(path)
    dur = len(a) / config.SR

    log("finding speech + speaker changes")
    utts = engine.vad_utterances(a)
    changes, overlaps = engine.change_points(a)
    utts = engine.split_at(utts, changes, overlaps, a)

    log(f"{len(utts)} utterances, clustering voiceprints")
    cents = engine.cluster(utts, num_speakers=num_speakers)
    with db.conn() as c:
        ppl = people(c)
        vocab = db.vocab_terms(c)
    matches = engine.match_people(cents, ppl)
    names = {p[0]: p[1] for p in ppl}

    log(f"transcribing with {engine_name} ({preset['name']})")
    # whisper (forced Cantonese + mixed prompt) won the code-switch test; sensevoice is ~10x faster but
    # garbles English inside Cantonese sentences. "both" keeps sensevoice as the alt line for comparison.
    if engine_name == "sensevoice":
        engine.sensevoice(utts, preset)
    else:
        if engine_name == "both":
            engine.sensevoice(utts, preset)
            for u in utts:
                u.alt, u.text = u.text, ""
        engine.whisper(a, utts, vocab, preset)

    clean = [u.text for u in utts]
    if use_llm and llm.available():
        if preset["cleanup"]:
            log(f"LLM ({llm.model()}): fixing misheard English")
            clean = llm.cleanup([u.text for u in utts], vocab, list(names.values()), preset["cleanup"])
    elif use_llm:
        log(f"Ollama not running (or no model pulled), skipping LLM passes")
        use_llm = False

    with db.conn() as c:
        c.execute("DELETE FROM segments WHERE meeting_id=?", (mid,))
        c.execute("DELETE FROM speakers WHERE meeting_id=?", (mid,))
        spk_ids, n = {}, 0
        for k in sorted({u.cluster for u in utts}):
            pid, score = matches.get(k, (None, None))
            if pid is None:
                n += 1
            label = names[pid] if pid else f"Speaker {n}"
            secs = sum(u.dur for u in utts if u.cluster == k)
            emb = cents.get(k)
            spk_ids[k] = c.execute(
                "INSERT INTO speakers(meeting_id, label, person_id, match_score, embedding, seconds) VALUES (?,?,?,?,?,?)",
                (mid, label, pid, score, db.blob(emb) if emb is not None else None, secs)).lastrowid
        for u, cl in zip(utts, clean):
            if not (u.text or u.alt):
                continue
            c.execute("INSERT INTO segments(meeting_id, start, end, speaker_id, acoustic_speaker_id, lang, overlap,"
                      " raw, clean, alt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (mid, u.start, u.end, spk_ids[u.cluster], spk_ids[u.cluster], u.lang, int(u.overlap),
                       u.text, cl if cl != u.text else None, u.alt or None))

    if use_llm:
        if config.REASON_SPEAKERS:
            log("LLM: reasoning about speaker labels (experimental)")
            reason_speakers(mid)
        log("LLM: writing notes")
        summarize(mid)

    log("compressing audio")
    days = config.RETENTION_DAYS if keep_audio_days is None else keep_audio_days
    opus = config.AUDIO / f"meeting-{mid}.opus"
    audio.to_opus(path, opus)
    with db.conn() as c:
        c.execute("UPDATE meetings SET duration=?, audio_path=?, audio_expires=?, status='done', error=NULL WHERE id=?",
                  (dur, str(opus), time.time() + days * 86400, mid))
        db.reindex(c, mid)
    export_markdown(mid)


def transcript_text(c, mid):
    rows = c.execute("SELECT s.*, k.label FROM segments s JOIN speakers k ON k.id=s.speaker_id "
                     "WHERE s.meeting_id=? ORDER BY start", (mid,)).fetchall()
    return "\n".join(f"[{_ts(r['start'])}] {r['label']}: {db.seg_text(r)}" for r in rows)


def reason_speakers(mid):
    with db.conn() as c:
        rows = c.execute("SELECT s.*, k.label FROM segments s JOIN speakers k ON k.id=s.speaker_id "
                         "WHERE s.meeting_id=? ORDER BY start", (mid,)).fetchall()
        spk = {r["label"]: r["id"] for r in c.execute("SELECT * FROM speakers WHERE meeting_id=?", (mid,))}
        if len(spk) < 2:
            return
        sugg = llm.speakers([(i, r["label"], db.seg_text(r)) for i, r in enumerate(rows)], list(spk))
        c.execute("UPDATE segments SET hint=NULL WHERE meeting_id=?", (mid,))
        for i, (lab, why) in sugg.items():
            c.execute("UPDATE segments SET hint=? WHERE id=?",
                      (db.dumps({"speaker_id": spk[lab], "label": lab, "why": why}), rows[i]["id"]))


def summarize(mid):
    with db.conn() as c:
        text = transcript_text(c, mid)
        if not text.strip():
            return
        lang = c.execute("SELECT lang FROM meetings WHERE id=?", (mid,)).fetchone()["lang"]
        s = llm.summary(text, db.vocab_terms(c), langs.get(lang)["name"])
        c.execute("UPDATE meetings SET summary=? WHERE id=?", (db.dumps(s), mid))
        if s.get("title"):
            c.execute("UPDATE meetings SET title=? WHERE id=? AND auto_title=1", (s["title"], mid))


def _ts(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def export_markdown(mid):
    """One markdown file per meeting, ready to drop into gbrain or any notes app."""
    import json
    with db.conn() as c:
        m = c.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
        s = json.loads(m["summary"]) if m["summary"] else {}
        spk = [r["label"] for r in c.execute("SELECT label FROM speakers WHERE meeting_id=? ORDER BY seconds DESC", (mid,))]
        lines = [f"# {m['title']}", "",
                 f"- date: {time.strftime('%Y-%m-%d %H:%M', time.localtime(m['created']))}",
                 f"- duration: {_ts(m['duration'] or 0)}", f"- people: {', '.join(spk)}"]
        if s.get("topics"):
            lines.append(f"- topics: {', '.join(s['topics'])}")
        if s.get("summary"):
            lines += ["", "## Summary", s["summary"]]
        if s.get("decisions"):
            lines += ["", "## Decisions", *[f"- {d}" for d in s["decisions"]]]
        if s.get("action_items"):
            lines += ["", "## Action items", *[f"- [{'x' if a.get('done') else ' '}] **{a.get('who', '?')}**: {a.get('what', '')}"
                                               + (f" (due {a['due']})" if a.get("due") else "") for a in s["action_items"]]]
        if s.get("open_questions"):
            lines += ["", "## Open questions", *[f"- {q}" for q in s["open_questions"]]]
        lines += ["", "## Transcript", transcript_text(c, mid)]
    slug = re.sub(r"[^\w一-鿿-]+", "-", m["title"]).strip("-")[:60] or f"meeting-{mid}"
    day = time.strftime("%Y-%m-%d", time.localtime(m["created"]))
    for old in config.NOTES.glob(f"*--m{mid}.md"):
        old.unlink()
    p = config.NOTES / f"{day}-{slug}--m{mid}.md"
    p.write_text("\n".join(lines) + "\n")
    return p


# ---------- learning from your corrections ----------

def enroll(name, path=None, samples=None):
    """Create/refresh a person's voiceprint from a clean clip of them talking."""
    db.init()
    a = samples if samples is not None else audio.load(path)
    utts = [u for u in engine.vad_utterances(a) if u.dur >= config.MIN_EMBED_SEC] or []
    if not utts:
        raise ValueError("no usable speech in that clip (need at least ~1s)")
    e = engine.norm(np.mean([engine.embed(u.samples) for u in utts], axis=0))
    with db.conn() as c:
        _update_person(c, name, e, weight=len(utts))
    return len(utts)


def _update_person(c, name, e, weight=1):
    r = c.execute("SELECT * FROM people WHERE name=?", (name,)).fetchone()
    if r is None:
        return c.execute("INSERT INTO people(name, embedding, n) VALUES (?,?,?)", (name, db.blob(e), weight)).lastrowid
    old, n = db.vec(r["embedding"]), r["n"] or 0
    new = e if old is None else engine.norm(old * n + e * weight)
    c.execute("UPDATE people SET embedding=?, n=? WHERE id=?", (db.blob(new), n + weight, r["id"]))
    return r["id"]


def assign_speaker(speaker_id, name):
    """You named a speaker in a meeting: relabel it, and teach that person's voiceprint."""
    with db.conn() as c:
        k = c.execute("SELECT * FROM speakers WHERE id=?", (speaker_id,)).fetchone()
        e = db.vec(k["embedding"])
        if e is not None:
            pid = _update_person(c, name, e, weight=max(1, int((k["seconds"] or 10) / 10)))
        else:
            pid = c.execute("INSERT OR IGNORE INTO people(name) VALUES (?)", (name,)).lastrowid or \
                c.execute("SELECT id FROM people WHERE name=?", (name,)).fetchone()["id"]
        other = c.execute("SELECT id FROM speakers WHERE meeting_id=? AND person_id=? AND id!=?",
                          (k["meeting_id"], pid, speaker_id)).fetchone()
        if other:  # this name already exists in the meeting: merge the two clusters
            c.execute("UPDATE segments SET speaker_id=? WHERE speaker_id=?", (other["id"], speaker_id))
            c.execute("UPDATE speakers SET seconds=seconds+? WHERE id=?", (k["seconds"] or 0, other["id"]))
            c.execute("DELETE FROM speakers WHERE id=?", (speaker_id,))
        else:
            c.execute("UPDATE speakers SET label=?, person_id=?, match_score=NULL WHERE id=?", (name, pid, speaker_id))
        c.execute("INSERT INTO corrections(segment_id, kind, before, after, created) VALUES (?,?,?,?,?)",
                  (None, "speaker_name", k["label"], name, time.time()))
        _rename_in_notes(c, k["meeting_id"], k["label"], name)
    export_markdown(k["meeting_id"])


def _rename_in_notes(c, meeting_id, old, new):
    """Notes written before you named someone say "Speaker 2". Carry the new name into them."""
    m = c.execute("SELECT summary FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if not m or not m["summary"] or not old or old == new:
        return
    pat = re.compile(re.escape(old) + r"(?!\d)")
    def fix(v):
        if isinstance(v, str):
            return pat.sub(new, v)
        if isinstance(v, list):
            return [fix(x) for x in v]
        if isinstance(v, dict):
            return {k: fix(x) for k, x in v.items()}
        return v
    c.execute("UPDATE meetings SET summary=? WHERE id=?",
              (json.dumps(fix(json.loads(m["summary"])), ensure_ascii=False), meeting_id))


def set_segment_speaker(seg_id, speaker_id):
    with db.conn() as c:
        s = c.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        c.execute("UPDATE segments SET speaker_id=?, hint=NULL WHERE id=?", (speaker_id, seg_id))
        c.execute("INSERT INTO corrections(segment_id, kind, before, after, created) VALUES (?,?,?,?,?)",
                  (seg_id, "segment_speaker", str(s["speaker_id"]), str(speaker_id), time.time()))
    export_markdown(s["meeting_id"])


LATIN = re.compile(r"[A-Za-z][A-Za-z0-9.+#'-]*")
# everyday English that isn't worth priming Whisper with
COMMON = set("""a an the and or but so to of in on at by for with from up out off over into about as is are was were
be been am do does did have has had will would can could should may might must not no yes ok okay i you he she it we
they me him her us them my your our their this that these those what which who how why when where all any some more
most very just also then than too only one two first last next new good bad big small get got go going make made take
let see say said know think want need like look use well really still even back now here there because if""".split())


def edit_segment(seg_id, text):
    """You fixed a line. Save it, learn new vocab, and keep the clip as training data."""
    with db.conn() as c:
        s = c.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        m = c.execute("SELECT * FROM meetings WHERE id=?", (s["meeting_id"],)).fetchone()
        before = db.seg_text(s)
        if text.strip() == before.strip():
            return []
        c.execute("UPDATE segments SET edited=? WHERE id=?", (text, seg_id))
        clip = None
        if config.KEEP_TRAINING_CLIPS and m["audio_path"] and Path(m["audio_path"]).exists():
            a = audio.load(m["audio_path"])
            clip = config.TRAIN / f"seg-{seg_id}.opus"
            audio.write_clip(a[int(s["start"] * config.SR):int(s["end"] * config.SR)], clip)
        c.execute("INSERT INTO corrections(segment_id, kind, before, after, clip_path, created) VALUES (?,?,?,?,?,?)",
                  (seg_id, "text", before, text, str(clip) if clip else None, time.time()))
        old_words = {w.lower() for w in LATIN.findall(s["raw"] or "")}
        learned = [w for w in dict.fromkeys(LATIN.findall(text))
                   if w.lower() not in old_words and w.lower() not in COMMON and (len(w) > 2 or w.isupper())]
        for w in learned:
            db.add_vocab(c, w, source="learned")
        db.reindex(c, s["meeting_id"])
    export_markdown(s["meeting_id"])
    return learned


def purge_expired():
    """Delete audio past its retention date. Transcripts, notes and training clips stay."""
    n = 0
    with db.conn() as c:
        for m in c.execute("SELECT * FROM meetings WHERE audio_path IS NOT NULL AND audio_expires < ?", (time.time(),)):
            Path(m["audio_path"]).unlink(missing_ok=True)
            c.execute("UPDATE meetings SET audio_path=NULL WHERE id=?", (m["id"],))
            n += 1
    return n


def export_finetune(dst):
    """JSONL of (clip, corrected text) pairs for a Whisper LoRA fine-tune later."""
    import json
    with db.conn() as c:
        rows = c.execute("SELECT * FROM corrections WHERE kind='text' AND clip_path IS NOT NULL ORDER BY id").fetchall()
    latest = {}
    for r in rows:
        latest[r["segment_id"]] = r
    with open(dst, "w") as f:
        for r in latest.values():
            if Path(r["clip_path"]).exists():
                f.write(json.dumps({"audio": r["clip_path"], "text": r["after"], "asr": r["before"]},
                                   ensure_ascii=False) + "\n")
    return len(latest)
