"""Local dashboard API. Bound to 127.0.0.1 only."""
import json
import os
import subprocess
import sys
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from . import config, db, export, langs, pipeline

app = FastAPI()
db.init()
STATIC = Path(__file__).parent / "static"
_job_lock = threading.Lock()  # one heavy job at a time, RAM is the constraint on this Mac
_jobs = {}


def _bg(key, fn, *args, **kw):
    def run():
        _jobs[key] = "queued"
        with _job_lock:
            _jobs[key] = "running"
            try:
                fn(*args, **kw)
                _jobs.pop(key, None)
            except Exception as e:  # surfaced in the UI via meetings.error / _jobs
                _jobs[key] = f"error: {e!r}"
    threading.Thread(target=run, daemon=True).start()


def _purge_loop():
    while True:
        try:
            pipeline.purge_expired()
        except Exception:
            pass
        time.sleep(3600)


threading.Thread(target=_purge_loop, daemon=True).start()

@app.on_event("startup")
def _recover():
    """The engine stopped mid-job last time (quit, crash, reboot): mark it so the Mac app resends it.
    Runs on server start only, never on import."""
    with db.conn() as c:
        for r in c.execute("SELECT id, job_pid FROM meetings WHERE status IN ('queued', 'processing')").fetchall():
            if r["job_pid"] and _alive(r["job_pid"]):
                continue  # its worker outlived the old server and is still going
            c.execute("UPDATE meetings SET status='interrupted', error='engine stopped before this finished' "
                      "WHERE id=?", (r["id"],))


def _alive(pid):
    try:
        os.kill(pid, 0)
        return "mb.cli" in subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                          capture_output=True, text=True).stdout
    except OSError:
        return False


@app.get("/api/ping")
def ping():
    return {"ok": True, "jobs": dict(_jobs)}


@app.get("/api/meetings/{mid}/export/{fmt}")
def export_meeting(mid: int, fmt: str, notes: int = 1):
    if fmt not in export.FORMATS:
        raise HTTPException(400, "format must be txt, md, docx or pdf")
    d = export.gather(mid)
    if not d:
        raise HTTPException(404, "no such meeting")
    fn, mime = export.FORMATS[fmt]
    body = fn(d, notes=bool(notes))
    name = export.filename(d, fmt) if notes else export.filename(d, fmt).replace(f".{fmt}", f" transcript.{fmt}")
    return Response(body.encode() if isinstance(body, str) else body, media_type=mime,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.get("/api/langs")
def get_langs():
    with db.conn() as c:
        return {"default": db.setting(c, "lang", config.LANG),
                "presets": [{"code": k, "name": v["name"]} for k, v in langs.PRESETS.items()]}


class Lang(BaseModel):
    lang: str


@app.post("/api/langs/default")
def set_lang(b: Lang):
    if b.lang not in langs.PRESETS:
        raise HTTPException(400, "unknown language")
    with db.conn() as c:
        db.set_setting(c, "lang", b.lang)
    return {"ok": True}


@app.get("/api/sources")
def sources():
    """Sources already ingested, so the Mac app can skip them."""
    with db.conn() as c:
        return {r["source"]: r["id"] for r in c.execute(
            "SELECT id, source FROM meetings WHERE source IS NOT NULL AND status != 'interrupted'")}


@app.get("/api/interrupted")
def interrupted():
    with db.conn() as c:
        return [r["source"] for r in c.execute(
            "SELECT source FROM meetings WHERE source IS NOT NULL AND status='interrupted'")]


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


# ---------- meetings ----------

@app.get("/api/meetings")
def meetings(q: str = ""):
    with db.conn() as c:
        if q.strip():
            q = q.strip()
            if len(q) >= 3:
                ids = c.execute("SELECT DISTINCT meeting_id FROM seg_fts WHERE seg_fts MATCH ?",
                                ('"' + q.replace('"', '""') + '"',)).fetchall()
            else:
                ids = c.execute("SELECT DISTINCT meeting_id FROM segments WHERE COALESCE(edited, clean, raw) LIKE ?",
                                (f"%{q}%",)).fetchall()
            ids = {r[0] for r in ids}
            ids |= {r[0] for r in c.execute("SELECT id FROM meetings WHERE title LIKE ? OR summary LIKE ?",
                                            (f"%{q}%", f"%{q}%"))}
            rows = [r for r in c.execute("SELECT * FROM meetings ORDER BY created DESC") if r["id"] in ids]
        else:
            rows = c.execute("SELECT * FROM meetings ORDER BY created DESC").fetchall()
        out = []
        for r in rows:
            s = json.loads(r["summary"]) if r["summary"] else {}
            ppl = [x["label"] for x in c.execute("SELECT label FROM speakers WHERE meeting_id=? ORDER BY seconds DESC",
                                                 (r["id"],))]
            out.append({"id": r["id"], "title": r["title"], "created": r["created"], "duration": r["duration"],
                        "status": r["status"], "error": r["error"], "people": ppl,
                        "gist": s.get("summary", "")[:220], "topics": s.get("topics", []),
                        "todo": len(s.get("action_items", [])), "has_audio": bool(r["audio_path"]),
                        "audio_expires": r["audio_expires"], "job": _jobs.get(f"m{r['id']}")})
        if q.strip():
            for m in out:
                m["hits"] = [dict(x) for x in c.execute(
                    "SELECT id, start, COALESCE(edited, clean, raw) AS text FROM segments WHERE meeting_id=? "
                    "AND COALESCE(edited, clean, raw) LIKE ? ORDER BY start LIMIT 3", (m["id"], f"%{q.strip()}%"))]
        return out


@app.get("/api/meetings/{mid}")
def meeting(mid: int):
    with db.conn() as c:
        m = c.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404)
        spk = [{"id": r["id"], "label": r["label"], "person_id": r["person_id"], "match": r["match_score"],
                "seconds": r["seconds"]}
               for r in c.execute("SELECT * FROM speakers WHERE meeting_id=? ORDER BY seconds DESC", (mid,))]
        segs = []
        for r in c.execute("SELECT * FROM segments WHERE meeting_id=? ORDER BY start", (mid,)):
            segs.append({"id": r["id"], "start": r["start"], "end": r["end"], "speaker_id": r["speaker_id"],
                         "acoustic_speaker_id": r["acoustic_speaker_id"], "lang": r["lang"],
                         "overlap": bool(r["overlap"]), "raw": r["raw"], "clean": r["clean"],
                         "edited": r["edited"], "alt": r["alt"], "text": db.seg_text(r),
                         "hint": json.loads(r["hint"]) if r["hint"] else None})
        return {"id": mid, "title": m["title"], "created": m["created"], "duration": m["duration"],
                "status": m["status"], "error": m["error"], "engine": m["engine"],
                "summary": json.loads(m["summary"]) if m["summary"] else None,
                "has_audio": bool(m["audio_path"]) and Path(m["audio_path"]).exists(),
                "audio_expires": m["audio_expires"], "speakers": spk, "segments": segs,
                "job": _jobs.get(f"m{mid}")}


@app.get("/api/meetings/{mid}/audio")
def meeting_audio(mid: int):
    with db.conn() as c:
        m = c.execute("SELECT audio_path FROM meetings WHERE id=?", (mid,)).fetchone()
    if not m or not m["audio_path"] or not Path(m["audio_path"]).exists():
        raise HTTPException(404, "audio already deleted")
    return FileResponse(m["audio_path"], media_type="audio/ogg")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), title: str = Form(""), engine: str = Form("whisper"),
                 speakers: str = Form(""), source: str = Form(""), recorded: str = Form(""), lang: str = Form("")):
    dup = None
    if source:
        with db.conn() as c:
            dup = c.execute("SELECT id, status FROM meetings WHERE source=?", (source,)).fetchone()
        if dup and dup["status"] != "interrupted":
            return {"id": dup["id"], "duplicate": True}
    tmp = Path(tempfile.mkdtemp()) / (file.filename or "upload")
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    name = title or Path(file.filename or "meeting").stem
    when = float(recorded) if recorded else time.time()
    with db.conn() as c:
        if source and dup:  # redo the interrupted one in place
            mid = dup["id"]
            for t in ("segments", "speakers", "seg_fts"):
                c.execute(f"DELETE FROM {t} WHERE meeting_id=?", (mid,))
            c.execute("UPDATE meetings SET status='queued', error=NULL, title=?, auto_title=?, created=? WHERE id=?",
                      (name, int(not title), when, mid))
        else:
            mid = c.execute("INSERT INTO meetings(title, created, engine, status, auto_title, source) "
                            "VALUES (?,?,?,?,?,?)",
                            (name, when, engine, "queued", int(not title), source or None)).lastrowid

    def job():
        # Its own process: the speaker-change model holds Python's lock for minutes, which froze the
        # dashboard when this ran in-thread.
        cmd = [sys.executable, "-m", "mb.cli", "process", str(tmp), "--meeting-id", str(mid),
               "--engine", engine, "--delete-input"]
        cmd += ["--speakers", speakers] if speakers else []
        cmd += ["--lang", lang] if lang else []
        try:
            p = subprocess.Popen(cmd, cwd=Path(__file__).parent.parent)
            with db.conn() as c:
                c.execute("UPDATE meetings SET job_pid=? WHERE id=?", (p.pid, mid))
            if p.wait() != 0:
                raise RuntimeError(f"processing exited with {p.returncode}")
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)  # original upload never lingers
    _bg(f"m{mid}", job)
    return {"id": mid}


class Title(BaseModel):
    title: str


@app.post("/api/meetings/{mid}/title")
def set_title(mid: int, b: Title):
    with db.conn() as c:
        c.execute("UPDATE meetings SET title=?, auto_title=0 WHERE id=?", (b.title.strip(), mid))
    pipeline.export_markdown(mid)
    return {"ok": True}


@app.post("/api/meetings/{mid}/summarize")
def resummarize(mid: int):
    if not pipeline.llm.available():
        raise HTTPException(503, "Ollama is not running")

    def job():
        pipeline.summarize(mid)
        pipeline.export_markdown(mid)
    _bg(f"m{mid}", job)
    return {"ok": True}


@app.post("/api/meetings/{mid}/reason")
def reason(mid: int):
    if not pipeline.llm.available():
        raise HTTPException(503, "Ollama is not running")
    _bg(f"m{mid}", pipeline.reason_speakers, mid)
    return {"ok": True}


@app.post("/api/meetings/{mid}/drop-audio")
def drop_audio(mid: int):
    with db.conn() as c:
        m = c.execute("SELECT audio_path FROM meetings WHERE id=?", (mid,)).fetchone()
        if m and m["audio_path"]:
            Path(m["audio_path"]).unlink(missing_ok=True)
        c.execute("UPDATE meetings SET audio_path=NULL WHERE id=?", (mid,))
    return {"ok": True}


@app.delete("/api/meetings/{mid}")
def delete_meeting(mid: int):
    drop_audio(mid)
    with db.conn() as c:
        for t in ("segments", "speakers", "seg_fts"):
            c.execute(f"DELETE FROM {t} WHERE meeting_id=?", (mid,))
        c.execute("DELETE FROM meetings WHERE id=?", (mid,))
    for p in config.NOTES.glob(f"*--m{mid}.md"):
        p.unlink()
    return {"ok": True}


# ---------- corrections ----------

class Name(BaseModel):
    name: str


@app.post("/api/speakers/{sid}/name")
def name_speaker(sid: int, b: Name):
    if not b.name.strip():
        raise HTTPException(400)
    pipeline.assign_speaker(sid, b.name.strip())
    return {"ok": True}


class SegSpeaker(BaseModel):
    speaker_id: int


@app.post("/api/segments/{seg}/speaker")
def seg_speaker(seg: int, b: SegSpeaker):
    pipeline.set_segment_speaker(seg, b.speaker_id)
    return {"ok": True}


class Text(BaseModel):
    text: str


@app.post("/api/segments/{seg}/text")
def seg_text(seg: int, b: Text):
    return {"learned": pipeline.edit_segment(seg, b.text)}


@app.post("/api/segments/{seg}/hint/{action}")
def seg_hint(seg: int, action: str):
    with db.conn() as c:
        r = c.execute("SELECT hint FROM segments WHERE id=?", (seg,)).fetchone()
        if not r or not r["hint"]:
            raise HTTPException(404)
        h = json.loads(r["hint"])
        c.execute("UPDATE segments SET hint=NULL WHERE id=?", (seg,))
        c.execute("INSERT INTO corrections(segment_id, kind, before, after, created) VALUES (?,?,?,?,?)",
                  (seg, f"hint_{action}", None, db.dumps(h), time.time()))
    if action == "accept":
        pipeline.set_segment_speaker(seg, h["speaker_id"])
    return {"ok": True}


# ---------- people + vocab ----------

@app.get("/api/people")
def people():
    with db.conn() as c:
        out = []
        for r in c.execute("SELECT * FROM people ORDER BY name"):
            n = c.execute("SELECT COUNT(DISTINCT meeting_id) FROM speakers WHERE person_id=?", (r["id"],)).fetchone()[0]
            secs = c.execute("SELECT COALESCE(SUM(seconds),0) FROM speakers WHERE person_id=?", (r["id"],)).fetchone()[0]
            out.append({"id": r["id"], "name": r["name"], "samples": r["n"], "has_voice": r["embedding"] is not None,
                        "meetings": n, "seconds": secs})
        return out


@app.post("/api/people/enroll")
async def enroll(name: str = Form(...), file: UploadFile = File(...)):
    tmp = Path(tempfile.mkdtemp()) / (file.filename or "clip")
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        n = pipeline.enroll(name.strip(), tmp)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return {"utterances": n}


@app.delete("/api/people/{pid}")
def delete_person(pid: int):
    with db.conn() as c:
        c.execute("UPDATE speakers SET person_id=NULL WHERE person_id=?", (pid,))
        c.execute("DELETE FROM people WHERE id=?", (pid,))
    return {"ok": True}


@app.get("/api/vocab")
def vocab():
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT id, term, misheard, source FROM vocab ORDER BY term COLLATE NOCASE")]


class Vocab(BaseModel):
    term: str
    misheard: str | None = None


@app.post("/api/vocab")
def add_vocab(b: Vocab):
    with db.conn() as c:
        db.add_vocab(c, b.term, (b.misheard or "").strip() or None)
    return {"ok": True}


@app.delete("/api/vocab/{vid}")
def del_vocab(vid: int):
    with db.conn() as c:
        c.execute("DELETE FROM vocab WHERE id=?", (vid,))
    return {"ok": True}


# ---------- action items across meetings ----------

@app.get("/api/actions")
def actions():
    with db.conn() as c:
        out = []
        for m in c.execute("SELECT id, title, created, summary FROM meetings WHERE summary IS NOT NULL ORDER BY created DESC"):
            for i, a in enumerate(json.loads(m["summary"]).get("action_items", [])):
                out.append({"meeting_id": m["id"], "meeting": m["title"], "created": m["created"], "i": i, **a})
        return out


class Done(BaseModel):
    done: bool


@app.post("/api/meetings/{mid}/actions/{i}")
def toggle_action(mid: int, i: int, b: Done):
    with db.conn() as c:
        m = c.execute("SELECT summary FROM meetings WHERE id=?", (mid,)).fetchone()
        s = json.loads(m["summary"])
        s["action_items"][i]["done"] = b.done
        c.execute("UPDATE meetings SET summary=? WHERE id=?", (db.dumps(s), mid))
    pipeline.export_markdown(mid)
    return {"ok": True}


@app.get("/api/stats")
def stats():
    size = lambda d: sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*), COALESCE(SUM(duration),0) FROM meetings").fetchone()
        corr = c.execute("SELECT COUNT(*) FROM corrections").fetchone()[0]
        clips = c.execute("SELECT COUNT(*) FROM corrections WHERE clip_path IS NOT NULL").fetchone()[0]
    return {"meetings": n[0], "hours": n[1] / 3600, "audio_mb": size(config.AUDIO) / 1e6,
            "train_mb": size(config.TRAIN) / 1e6, "db_mb": config.DB.stat().st_size / 1e6 if config.DB.exists() else 0,
            "corrections": corr, "training_clips": clips, "retention_days": config.RETENTION_DAYS,
            "llm": pipeline.llm.model() if pipeline.llm.available() else None, "notes_dir": str(config.NOTES)}
