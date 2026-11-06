"""Meeting language presets. Voiceprints don't care about language; these tune transcription,
script conversion and the LLM passes.

prompt   style primer for Whisper: shows English words staying in English mid-sentence.
         Keep it unrelated to real meeting content.
opencc   script conversion applied to the transcript (None = leave as is)
cleanup  system prompt for fixing English heard as same-sounding native words (None = skip)
"""

CANTONESE_CLEANUP = """你係香港會議逐字稿校對員。啲人講嘢會廣東話夾英文（code-switching）。
語音辨識成日將英文字聽錯做同音中文字，例如「我哋心一下」其實係「我哋 sync 一下」，「卓吓」係「check 吓」。
你嘅工作：只修正呢類聽錯，同埋專有名詞串錯。
規則：
- 保留口語廣東話（嘅、咗、喺、嚟），唔好改做書面語，唔好翻譯，唔好潤色，唔好加減內容。
- 英文字用返英文原文，前後加空格。
- 唔肯定就唔好改。
- 用繁體中文。
輸出 JSON：{"lines": [{"i": 行號, "text": "修正後"}]}，只列有改動嘅行。"""

MANDARIN_CLEANUP = """你是会议逐字稿校对员。说话的人会中文夹英文（code-switching）。
语音识别常把英文词听成同音的中文字，例如「我们森克一下」其实是「我们 sync 一下」。
你的工作：只修正这类听错，以及专有名词写错。
规则：
- 保留口语，不要改写成书面语，不要翻译，不要润色，不要增减内容。
- 英文词用英文原文，前后加空格。
- 不确定就不要改。
- 保持原文使用的简体或繁体。
输出 JSON：{"lines": [{"i": 行号, "text": "修正后"}]}，只列出有改动的行。"""

PRESETS = {
    "yue": dict(name="Cantonese + English", whisper="yue", opencc="s2hk", cleanup=CANTONESE_CLEANUP,
                prompt="OK，我哋開始啦。今次個 project 嘅 scope 要 confirm 一下，有 question 可以直接 ask。",
                terms="會提到：{}。"),
    "zh": dict(name="Mandarin + English (Simplified)", whisper="zh", opencc="t2s", cleanup=MANDARIN_CLEANUP,
               prompt="好，我们开始吧。这次 project 的 scope 要 confirm 一下，有 question 可以直接问。",
               terms="会提到：{}。"),
    "zh-tw": dict(name="Mandarin + English (Traditional)", whisper="zh", opencc="s2twp", cleanup=MANDARIN_CLEANUP,
                  prompt="好，我們開始吧。這次 project 的 scope 要 confirm 一下，有 question 可以直接問。",
                  terms="會提到：{}。"),
    "ja": dict(name="Japanese + English", whisper="ja", opencc=None, cleanup=None,
               prompt="はい、始めましょう。今回の project の scope を confirm したいです。question があればどうぞ。",
               terms="用語：{}。"),
    "ko": dict(name="Korean + English", whisper="ko", opencc=None, cleanup=None,
               prompt="네, 시작하겠습니다. 이번 project 의 scope 를 confirm 하고 싶어요. question 있으면 말씀하세요.",
               terms="용어: {}."),
    "es": dict(name="Spanish + English", whisper="es", opencc=None, cleanup=None,
               prompt="",  # a mixed primer made Whisper output nothing for Spanish
               terms="Términos: {}."),
    "fr": dict(name="French + English", whisper="fr", opencc=None, cleanup=None,
               prompt="",  # same failure as Spanish, untested
               terms="Termes : {}."),
    "en": dict(name="English", whisper="en", opencc=None, cleanup=None,
               prompt="OK, let's get started.", terms="Terms: {}."),
    "auto": dict(name="Auto-detect", whisper="auto", opencc=None, cleanup=None, prompt="", terms="{}."),
}


def get(code):
    return PRESETS.get(code or "", PRESETS["yue"])
