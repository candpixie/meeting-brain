"""Local LLM passes via Ollama. Nothing leaves the laptop.

cleanup   fixes English words the ASR heard as Cantonese homophones (我哋心一下 -> 我哋 sync 一下)
speakers  experimental reasoning pass: suggests speaker fixes from conversational cues
summary   summary, decisions, action items per person
"""
import json
import subprocess
import urllib.request

from . import config


_model = None


def model():
    return _model or config.OLLAMA_MODEL


def _chat(system, prompt, num_ctx=16384, temperature=0.1):
    body = {"model": model(), "stream": False, "format": "json", "keep_alive": "20s",
            "options": {"temperature": temperature, "num_ctx": num_ctx},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    req = urllib.request.Request(f"{config.OLLAMA_URL}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        txt = json.loads(r.read())["message"]["content"]
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return {}


def available():
    """Pick the configured model, or the fast fallback if it isn't pulled. False if Ollama is down."""
    global _model
    try:
        with urllib.request.urlopen(f"{config.OLLAMA_URL}/api/tags", timeout=3) as r:
            sizes = {m["name"]: m.get("size", 0) for m in json.loads(r.read())["models"]}
    except Exception:
        return False
    ram = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout or 0)
    for m in (config.OLLAMA_MODEL, config.OLLAMA_FAST_MODEL):
        if m not in sizes:
            continue
        if ram and sizes[m] > ram * config.MAX_MODEL_RAM_SHARE and not config.ALLOW_BIG_MODEL:
            print(f"[mb] skipping {m}: {sizes[m] / 1e9:.0f} GB is too big for {ram / 1e9:.0f} GB RAM")
            continue
        _model = m
        return True
    return False


CLEAN_SYS = """你係香港會議逐字稿校對員。啲人講嘢會廣東話夾英文（code-switching）。
語音辨識成日將英文字聽錯做同音中文字，例如「我哋心一下」其實係「我哋 sync 一下」，「卓吓」係「check 吓」。
你嘅工作：只修正呢類聽錯，同埋專有名詞串錯。
規則：
- 保留口語廣東話（嘅、咗、喺、嚟），唔好改做書面語，唔好翻譯，唔好潤色，唔好加減內容。
- 英文字用返英文原文，前後加空格。
- 唔肯定就唔好改。
- 用繁體中文。
輸出 JSON：{"lines": [{"i": 行號, "text": "修正後"}]}，只列有改動嘅行。"""


def cleanup(lines, vocab, people, system=None):
    """lines: [str]. Returns list of same length with fixes applied."""
    out = list(lines)
    terms = ", ".join(sorted({v["term"] for v in vocab} | set(people)))
    hints = "; ".join(f"「{v['misheard']}」→{v['term']}" for v in vocab if v.get("misheard"))
    for b in range(0, len(lines), 40):
        chunk = lines[b:b + 40]
        numbered = "\n".join(f"{b + i}. {t}" for i, t in enumerate(chunk) if t.strip())
        if not numbered:
            continue
        prompt = (f"常用詞彙（會議入面成日出現）：{terms or '（未有）'}\n"
                  f"以前聽錯過：{hints or '（未有）'}\n\n逐字稿：\n{numbered}")
        res = _chat(system or CLEAN_SYS, prompt)
        for item in res.get("lines", []):
            try:
                i, t = int(item["i"]), str(item["text"]).strip()
            except (KeyError, ValueError, TypeError):
                continue
            end = lines[i].rstrip()[-1:] if b <= i < b + len(chunk) else ""
            if t.endswith("。") and end in (".", "?", "!", ""):
                t = t[:-1] + end  # the model likes to sinicise English punctuation
            if b <= i < b + len(chunk) and t and t != lines[i] and _plausible(lines[i], t):
                out[i] = t
    return out


def _plausible(before, after):
    """Reject rewrites that drift too far from the ASR (the model paraphrasing)."""
    if len(after) > len(before) * 2 + 10 or len(after) < len(before) * 0.5:
        return False
    # a homophone fix swaps a few hanzi for English, so nearly all remaining hanzi must be original
    cjk = [ch for ch in after if "一" <= ch <= "鿿"]
    return not cjk or sum(ch in before for ch in cjk) >= 0.8 * len(cjk)


SPK_SYS = """You review speaker labels in a meeting transcript (Cantonese/English mixed).
Labels came from voice acoustics only, which fails on short turns, similar voices and interruptions.
Use conversational logic: someone addressed by name usually answers next; a question and its answer are
usually different speakers; people don't usually reply to themselves; "I'll send it" connects to who was asked.
Only flag lines where the evidence is strong. Never relabel more than a few lines.
Return JSON: {"fixes": [{"i": line, "speaker": "label from the list", "why": "short reason"}]}"""


def speakers(rows, labels):
    """rows: [(i, label, text)]. Returns {i: (label, why)} suggestions."""
    out = {}
    for b in range(0, len(rows), 60):
        chunk = rows[b:b + 60]
        prompt = (f"Speaker labels: {', '.join(labels)}\n\n" +
                  "\n".join(f"{i}. [{lab}] {t}" for i, lab, t in chunk))
        for f in _chat(SPK_SYS, prompt).get("fixes", []):
            try:
                i, lab = int(f["i"]), str(f["speaker"])
            except (KeyError, ValueError, TypeError):
                continue
            if lab in labels and any(r[0] == i and r[1] != lab for r in chunk):
                out[i] = (lab, str(f.get("why", ""))[:200])
    return out


SUM_SYS = """You write meeting notes for the person who recorded the meeting (their second brain).
Write the notes in the same language, or mix of languages, that the people used. Spoken Cantonese stays
spoken Cantonese (嘅, 咗, 喺), not formal written Chinese.
NEVER translate English terms, product names or jargon (TestFlight stays TestFlight, not 測試飛行).
Read workplace English the way it is meant: "push the build" means ship/release it, not postpone.
Keep the script the transcript uses (Traditional or Simplified). Only state what the transcript supports. Be concrete, no filler.
If speakers are unnamed ("Speaker 1"), keep those labels.
Return JSON: {"title": "short title", "summary": "3-6 sentences",
"decisions": ["..."], "action_items": [{"who": "name", "what": "...", "due": "if stated, else empty"}],
"open_questions": ["..."], "topics": ["short tags"]}"""


def summary(transcript, vocab=(), language=""):
    if language:
        transcript = f"Meeting language: {language}\n" + transcript
    if vocab:
        transcript = "Known terms: " + ", ".join(v["term"] for v in vocab) + "\n\n" + transcript
    if len(transcript) <= 14000:
        return _chat(SUM_SYS, transcript, num_ctx=24576, temperature=0.2)
    parts = [transcript[i:i + 12000] for i in range(0, len(transcript), 12000)]
    partials = [_chat(SUM_SYS, f"(Part {n + 1} of {len(parts)})\n{p}", num_ctx=24576) for n, p in enumerate(parts)]
    return _chat(SUM_SYS, "Merge these partial notes of one meeting into a single set of notes:\n" +
                 json.dumps(partials, ensure_ascii=False), num_ctx=24576, temperature=0.2)
