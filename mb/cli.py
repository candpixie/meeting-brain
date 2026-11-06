"""mb: your private meeting brain.

  mb process FILE [--engine whisper|sensevoice|both] [--speakers N] [--title T] [--no-llm]
  mb record [--title T] [--device :0]     record mic until Ctrl-C, then process
  mb enroll NAME FILE                     teach a voice from a clean clip
  mb people | mb vocab [add TERM [MISHEARD]] | mb devices
  mb serve                                dashboard at http://localhost:4470
  mb purge                                delete audio past retention (also runs on serve)
  mb compress FILE [--fast]               shrink a long lecture video
  mb export-finetune OUT.jsonl            your corrections as training pairs
"""
import argparse
import sys
from pathlib import Path
import time

from . import audio, config, db, pipeline, langs


def main(argv=None):
    p = argparse.ArgumentParser(prog="mb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("process")
    pr.add_argument("file")
    pr.add_argument("--engine", default="whisper", choices=["sensevoice", "whisper", "both"])
    pr.add_argument("--speakers", type=int)
    pr.add_argument("--lang", choices=list(langs.PRESETS), help="meeting language (default: yue, or MB_LANG)")
    pr.add_argument("--title")
    pr.add_argument("--no-llm", action="store_true")
    pr.add_argument("--keep-days", type=float)
    pr.add_argument("--meeting-id", type=int, help=argparse.SUPPRESS)  # used by the server
    pr.add_argument("--delete-input", action="store_true", help=argparse.SUPPRESS)
    pr.add_argument("--quiet", action="store_true", help="2 threads, low priority: slower but cool and quiet")
    rc = sub.add_parser("record")
    rc.add_argument("--title")
    rc.add_argument("--device", default=":0")
    rc.add_argument("--lang", choices=list(langs.PRESETS))
    rc.add_argument("--engine", default="whisper", choices=["sensevoice", "whisper", "both"])
    en = sub.add_parser("enroll")
    en.add_argument("name")
    en.add_argument("file")
    sub.add_parser("people")
    vo = sub.add_parser("vocab")
    vo.add_argument("action", nargs="?", choices=["add", "rm"])
    vo.add_argument("term", nargs="?")
    vo.add_argument("misheard", nargs="?")
    sub.add_parser("devices")
    sub.add_parser("serve")
    sub.add_parser("purge")
    co = sub.add_parser("compress")
    co.add_argument("file")
    co.add_argument("--fast", action="store_true")
    co.add_argument("--height", type=int, default=720)
    co.add_argument("--fps", type=int, default=10)
    ex = sub.add_parser("export-finetune")
    ex.add_argument("out")
    a = p.parse_args(argv)
    db.init()

    if getattr(a, "quiet", False):
        import os
        config.THREADS = 2
        os.nice(15)
    if a.cmd == "process":
        try:
            mid = pipeline.process(a.file, title=a.title, engine_name=a.engine, num_speakers=a.speakers,
                                   use_llm=not a.no_llm, keep_audio_days=a.keep_days, lang=a.lang,
                                   meeting_id=a.meeting_id)
        finally:
            if a.delete_input:  # uploads are copies; never leave one lying around
                Path(a.file).unlink(missing_ok=True)
        print(f"open http://localhost:{config.PORT}/#/m/{mid}")
    elif a.cmd == "record":
        dst = config.AUDIO / f"rec-{time.strftime('%Y%m%d-%H%M%S')}.opus"
        print("recording, Ctrl-C to stop")
        audio.record(dst, a.device)
        mid = pipeline.process(dst, title=a.title, engine_name=a.engine, lang=a.lang)
        dst.unlink(missing_ok=True)  # pipeline already saved its own compressed copy
        print(f"open http://localhost:{config.PORT}/#/m/{mid}")
    elif a.cmd == "enroll":
        n = pipeline.enroll(a.name, a.file)
        print(f"enrolled {a.name} from {n} utterances")
    elif a.cmd == "people":
        with db.conn() as c:
            for r in c.execute("SELECT name, n FROM people ORDER BY name"):
                print(f"{r['name']}\t{r['n']} samples")
    elif a.cmd == "vocab":
        with db.conn() as c:
            if a.action == "add" and a.term:
                db.add_vocab(c, a.term, a.misheard)
            elif a.action == "rm" and a.term:
                c.execute("DELETE FROM vocab WHERE term=?", (a.term,))
            for v in db.vocab_terms(c):
                print(v["term"] + (f"\t(misheard: {v['misheard']})" if v["misheard"] else ""))
    elif a.cmd == "devices":
        print(audio.devices())
    elif a.cmd == "serve":
        import uvicorn
        print(f"[mb] purged {pipeline.purge_expired()} expired recordings")
        uvicorn.run("mb.server:app", host="127.0.0.1", port=config.PORT, log_level="warning")
    elif a.cmd == "purge":
        print(f"purged {pipeline.purge_expired()} recordings")
    elif a.cmd == "compress":
        out = audio.compress_lecture(a.file, height=a.height, fps=a.fps, fast=a.fast)
        before, after = (x.stat().st_size / 1e6 for x in (__import__("pathlib").Path(a.file), out))
        print(f"{out}: {before:.0f} MB -> {after:.0f} MB")
    elif a.cmd == "export-finetune":
        print(f"wrote {pipeline.export_finetune(a.out)} pairs to {a.out}")


if __name__ == "__main__":
    sys.exit(main())
