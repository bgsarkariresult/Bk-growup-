"""
Hinglish subtitles for BK GrowUp Shorts.

Kaam:
1. Final (1.5x tez) audio par faster-whisper chalake har shabd ka sahi time nikalta hai.
2. Time ko script ke shabdon se match karta hai (text script ka hi rehta hai, Whisper ka nahi).
3. Har Hindi shabd ko Hinglish (Roman) me badalta hai: pehle Gemini (3 keys) -> g4f -> offline mapper.
4. 3-4 shabd ke chhote subtitle (.ass) banata hai aur ffmpeg se video par jala deta hai.

Kuch bhi fail ho to add_hinglish_subtitles() False lautata hai aur original video jaisa tha waisa hi rehta hai.
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import time
import unicodedata

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small").strip() or "small"
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-3.6-flash")

WORDS_PER_CAPTION = 3
MAX_CAPTION_CHARS = 17
SENTENCE_END = ("।", ".", "!", "?", ",", ";", ":", "|")

# ------------------------------------------------------------------
# Shabd / text helpers
# ------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[\u0964\u0965\.\,\!\?\;\:\-\u2013\u2014\"'\u2018\u2019\u201c\u201d\(\)\[\]\{\}\|\u2026/]+")


def _strip_punct(word):
    return _PUNCT_RE.sub("", word).strip()


def _norm(word):
    w = unicodedata.normalize("NFC", _strip_punct(word)).lower()
    w = w.replace("\u093c", "")  # nukta hatao (ज़ ~ ज)
    w = w.replace("\u0901", "\u0902")  # chandrabindu ~ anusvara
    return w


def tokenize_script(script):
    return [w for w in str(script).split() if _strip_punct(w)]


# ------------------------------------------------------------------
# Audio duration
# ------------------------------------------------------------------
def _audio_duration(path):
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception:
        return 0.0


# ------------------------------------------------------------------
# Whisper se shabd-wise timing
# ------------------------------------------------------------------
def whisper_words(audio_path):
    """[(word, start, end), ...] — fail ho to exception."""
    from faster_whisper import WhisperModel
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        audio_path, language="hi", word_timestamps=True, beam_size=1, vad_filter=False)
    out = []
    for seg in segments:
        for w in (seg.words or []):
            text = (w.word or "").strip()
            if text and _norm(text):
                out.append((text, float(w.start), float(w.end)))
    return out


# ------------------------------------------------------------------
# Script shabdon ko Whisper shabdon se align karna (Needleman-Wunsch)
# ------------------------------------------------------------------
def _sim(a, b):
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def align_timings(script_words, asr_words, audio_dur):
    """Har script shabd ke liye (start, end). Match na hone wale shabdon ka time beech se nikalta hai."""
    n = len(script_words)
    m = len(asr_words)
    starts = [None] * n
    ends = [None] * n
    anchored = 0

    if n and m:
        a = [_norm(w) for w in script_words]
        b = [_norm(w[0]) for w in asr_words]
        gap = -0.5
        score = [[0.0] * (m + 1) for _ in range(n + 1)]
        move = [[0] * (m + 1) for _ in range(n + 1)]  # 1=diag, 2=up (script gap), 3=left (asr gap)
        for i in range(1, n + 1):
            score[i][0] = i * gap
            move[i][0] = 2
        for j in range(1, m + 1):
            score[0][j] = j * gap
            move[0][j] = 3
        sims = {}
        for i in range(1, n + 1):
            for j in range(1, m + 1):
                s = _sim(a[i - 1], b[j - 1])
                sims[(i, j)] = s
                diag = score[i - 1][j - 1] + (s * 2 - 0.9)
                up = score[i - 1][j] + gap
                left = score[i][j - 1] + gap
                best = max(diag, up, left)
                score[i][j] = best
                move[i][j] = 1 if best == diag else (2 if best == up else 3)
        i, j = n, m
        while i > 0 or j > 0:
            mv = move[i][j]
            if mv == 1 and i > 0 and j > 0:
                if sims[(i, j)] >= 0.5:
                    starts[i - 1] = asr_words[j - 1][1]
                    ends[i - 1] = asr_words[j - 1][2]
                    anchored += 1
                i -= 1
                j -= 1
            elif mv == 2 and i > 0:
                i -= 1
            else:
                j -= 1

    ratio = anchored / n if n else 0
    if ratio < 0.3:
        return _proportional(script_words, 0.2, max(audio_dur - 0.2, 0.5)), ratio

    # Beech ke khali shabdon ko interpolate karo
    i = 0
    while i < n:
        if starts[i] is not None:
            i += 1
            continue
        j = i
        while j < n and starts[j] is None:
            j += 1
        t0 = ends[i - 1] if i > 0 else 0.0
        t1 = starts[j] if j < n else audio_dur
        if t1 < t0:
            t1 = t0 + 0.2 * (j - i)
        seg = _proportional(script_words[i:j], t0, t1)
        for k, (s, e) in enumerate(seg):
            starts[i + k], ends[i + k] = s, e
        i = j

    # Time hamesha aage badhe
    prev = 0.0
    for k in range(n):
        if starts[k] < prev:
            starts[k] = prev
        if ends[k] < starts[k] + 0.05:
            ends[k] = starts[k] + 0.05
        prev = starts[k]
    return list(zip(starts, ends)), ratio


def _proportional(words, t0, t1):
    if not words:
        return []
    total = sum(len(_strip_punct(w)) + 1 for w in words)
    span = max(t1 - t0, 0.05 * len(words))
    out, cur = [], t0
    for w in words:
        d = span * (len(_strip_punct(w)) + 1) / total
        out.append((cur, cur + d))
        cur += d
    return out


# ------------------------------------------------------------------
# Hinglish transliteration
# ------------------------------------------------------------------
def _gemini_keys():
    keys = [os.environ.get(f"GEMINI_API_KEY_{i}", "").strip() for i in (1, 2, 3)]
    return [k for k in keys if k]


def _parse_word_list(raw, n):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    data = json.loads(raw)
    if isinstance(data, dict):  # kabhi {"words": [...]} aa jata hai
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
    if isinstance(data, list) and len(data) == n and all(isinstance(x, str) for x in data):
        return [x.strip() for x in data]
    return None


def _llm_hinglish(words):
    """Gemini (3 keys) -> g4f. Exactly len(words) Hinglish shabd lautata hai, warna None."""
    n = len(words)
    prompt = (
        "Convert each Hindi (Devanagari) word below into Hinglish: Hindi written in Roman letters, "
        "the way Indians type on WhatsApp (e.g. आज->aaj, नहीं->nahi, बड़ी->badi, क्या->kya, "
        "फॉर्म->form, एडमिट->admit, कार्ड->card, रिजल्ट->result). "
        "Words that are English written in Devanagari must use normal English spelling. "
        "Keep digits as digits. All lowercase. No punctuation.\n"
        f"Output ONLY a JSON array of exactly {n} strings, same order, one per input word, nothing else.\n"
        f"INPUT: {json.dumps(words, ensure_ascii=False)}"
    )

    for idx, key in enumerate(_gemini_keys(), start=1):
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=key)
            for attempt in (1, 2):
                try:
                    resp = client.models.generate_content(
                        model=GEMINI_MODEL_NAME, contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json", temperature=0.1))
                    res = _parse_word_list(resp.text, n)
                    if res:
                        print(f"✅ Hinglish transliteration: Gemini Key #{idx}")
                        return res
                    print(f"⚠️ Gemini Key #{idx} attempt {attempt}: shabd ginti match nahi hui")
                except Exception as e:
                    print(f"⚠️ Gemini Key #{idx} attempt {attempt} fail: {e}")
                time.sleep(1.5)
        except Exception as e:
            print(f"⚠️ Gemini Key #{idx} init fail: {e}")

    try:
        from g4f.client import Client
        client = Client()
        for attempt in (1, 2):
            try:
                resp = client.chat.completions.create(
                    model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.1)
                res = _parse_word_list(resp.choices[0].message.content, n)
                if res:
                    print("✅ Hinglish transliteration: g4f")
                    return res
            except Exception as e:
                print(f"⚠️ g4f attempt {attempt} fail: {e}")
            time.sleep(1.5)
    except Exception as e:
        print(f"⚠️ g4f unavailable: {e}")
    return None


# --- Offline fallback: seedha Devanagari -> Roman ---
_VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri",
           "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
_MATRAS = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri",
           "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e"}
_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j",
         "झ": "jh", "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t",
         "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh",
         "म": "m", "य": "y", "र": "r", "ल": "l", "ळ": "l", "व": "v", "श": "sh", "ष": "sh",
         "स": "s", "ह": "h"}
_NUKTA = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f"}
_DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}


def devanagari_to_roman(word):
    """Simple offline Hinglish (fallback). Roman shabd jaise ke taise rehte hain."""
    w = unicodedata.normalize("NFC", _strip_punct(word))
    if not re.search(r"[\u0900-\u097F]", w):
        return w.lower()
    w = w.replace("ज्ञ", "ग्य")
    units = []  # (roman_consonant_or_'', vowel_str or None=inherent-a)
    i = 0
    while i < len(w):
        ch = w[i]
        if ch in _DIGITS:
            units.append((_DIGITS[ch], ""))
        elif ch in _VOWELS:
            units.append(("", _VOWELS[ch]))
        elif ch in _CONS:
            base = _CONS[ch]
            if i + 1 < len(w) and w[i + 1] == "\u093c":
                base = _NUKTA.get(ch, base)
                i += 1
            vowel = None
            if i + 1 < len(w):
                nxt = w[i + 1]
                if nxt in _MATRAS:
                    vowel = _MATRAS[nxt]
                    i += 1
                elif nxt == "\u094d":  # virama
                    vowel = ""
                    i += 1
            units.append((base, vowel))
        elif ch in ("\u0902", "\u0901"):
            units.append(("", "n"))
        elif ch == "\u0903":
            units.append(("", "h"))
        else:
            units.append((ch if ch.isascii() else "", ""))
        i += 1
    # Schwa: antim shabd ka 'a' hatao; dusre-akhir ka 'a' tab hatao jab aakhri unit me lamba matra ho
    last_cons_idx = [k for k, (c, v) in enumerate(units) if c and v is None]
    if last_cons_idx and last_cons_idx[-1] == len(units) - 1:
        units[-1] = (units[-1][0], "")
    if len(units) >= 3:
        pen = len(units) - 2
        c, v = units[pen]
        last_v = units[-1][1]
        if c and v is None and last_v in ("aa", "ee", "oo", "i", "e", "o", "ai", "au") and pen > 0:
            units[pen] = (c, "")
    out = ""
    for c, v in units:
        out += c + ("a" if v is None else v)
    out = out.replace("aaa", "aa")
    if len(out) > 2 and out.endswith("aa") and out[-3] not in "aeiou":
        out = out[:-2] + "a"
    if len(out) > 2 and out.endswith("ee") and out[-3] not in "aeiou":
        out = out[:-2] + "i"
    return out.lower()


def hinglish_words(script_words):
    """Script ke har shabd ka Hinglish (1:1). Pehle LLM, na chale to offline mapper."""
    clean = [_strip_punct(w) for w in script_words]
    if any(re.search(r"[\u0900-\u097F]", w) for w in clean):
        res = _llm_hinglish(clean)
        if res:
            return [r if r else devanagari_to_roman(c) for r, c in zip(res, clean)]
        print("⚠️ LLM transliteration nahi hui — offline mapper use ho raha hai.")
    return [devanagari_to_roman(w) for w in clean]


# ------------------------------------------------------------------
# Captions + ASS file
# ------------------------------------------------------------------
def build_captions(script_words, roman_words, timings, cap_seconds):
    captions = []
    cur = []  # (roman, start, end)
    cur_chars = 0

    def flush():
        nonlocal cur, cur_chars
        if cur:
            captions.append([" ".join(x[0] for x in cur), cur[0][1], cur[-1][2], list(cur)])
        cur, cur_chars = [], 0

    for orig, rom, (s, e) in zip(script_words, roman_words, timings):
        if not rom:
            continue
        if cur and (len(cur) >= WORDS_PER_CAPTION or cur_chars + len(rom) + 1 > MAX_CAPTION_CHARS):
            flush()
        cur.append((rom, s, e))
        cur_chars += len(rom) + 1
        if orig.rstrip().endswith(SENTENCE_END):
            flush()
    flush()

    # Flicker rokne ke liye: chhote gap bhar do, end ko agle start tak badhao
    for k in range(len(captions) - 1):
        gap = captions[k + 1][1] - captions[k][2]
        if 0 <= gap < 0.45:
            captions[k][2] = captions[k + 1][1]
        elif gap > 0:
            captions[k][2] += 0.25
    captions = [c for c in captions if c[1] < cap_seconds]
    for c in captions:
        c[2] = min(c[2], cap_seconds)
        if c[2] - c[1] < 0.25:
            c[2] = c[1] + 0.25
    return captions


def _ass_time(t):
    t = max(t, 0.0)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def write_ass(captions, ass_path, width=1080, height=1920):
    """Bada, saaf subtitle: safed text + kala outline; jo shabd bol raha hai us par peela box.
    Subtitle neeche CTA patti / Shorts buttons se upar (~62% height par) rehta hai."""
    font_size = 96
    margin_v = int(height * 0.36)  # neeche se; text ka niche ka kinara ~64% height par
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Cap: safed text, mota kala outline
        f"Style: Cap,Noto Sans,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,"
        f"-1,0,0,0,100,100,0,0,1,9,3,2,60,60,{margin_v},1\n"
        # Hi: kala text, peela box (BorderStyle 3 = box, Outline = box ka padding)
        f"Style: Hi,Noto Sans,{font_size},&H00000000,&H000000FF,&H0033C9FF,&H00000000,"
        f"-1,0,0,0,100,100,0,0,3,16,0,2,60,60,{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def clean(t):
        return t.replace("\\", "").replace("{", "(").replace("}", ")")

    lines = []
    for cap in captions:
        text, s, e = cap[0], cap[1], cap[2]
        words = cap[3] if len(cap) > 3 else []
        # Layer 0: poora caption safed
        lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,{clean(text)}")
        # Layer 1: har shabd ke bolne ke waqt sirf wahi shabd peele box me
        for k, (rom, ws, we) in enumerate(words):
            hs = s if k == 0 else ws
            he = words[k + 1][1] if k + 1 < len(words) else e
            he = min(max(he, hs + 0.12), e)
            if he <= hs:
                continue
            parts = []
            for j, (r2, _a, _b) in enumerate(words):
                alpha = "&H00&" if j == k else "&HFF&"
                parts.append("{\\alpha" + alpha + "}" + clean(r2))
            lines.append(
                f"Dialogue: 1,{_ass_time(hs)},{_ass_time(he)},Hi,,0,0,0,,{' '.join(parts)}")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")


def burn_subtitles(video_in, ass_path, video_out):
    """ass file ka simple naam use hota hai taaki ffmpeg filter me path escape ka jhanjhat na ho."""
    work_ass = "temp_subs.ass"
    shutil.copyfile(ass_path, work_ass)
    try:
        subprocess.run(
            ["ffmpeg", "-i", video_in, "-vf", f"subtitles={work_ass}",
             "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
             "-c:a", "copy", "-movflags", "+faststart", "-y", video_out],
            capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ ffmpeg subtitle burn error: {e.stderr.decode(errors='ignore')[-600:]}")
        return False
    finally:
        if os.path.exists(work_ass):
            os.remove(work_ass)


# ------------------------------------------------------------------
# Main entry
# ------------------------------------------------------------------
def add_hinglish_subtitles(audio_path, script_text, video_path, cap_seconds=59.0):
    """Video par Hinglish subtitle lagata hai (video_path usi jagah replace hota hai).
    True = subtitle lag gaye, False = nahi lage (video jaisa tha waisa hi hai)."""
    tmp_out = video_path.replace(".mp4", "_subs_tmp.mp4")
    ass_path = "temp_captions.ass"
    try:
        print("📝 Hinglish subtitles ban rahe hain...")
        script_words = tokenize_script(script_text)
        if not script_words:
            print("⚠️ Script khali hai, subtitle skip.")
            return False

        audio_dur = _audio_duration(audio_path) or min(cap_seconds, 35.0)

        try:
            asr = whisper_words(audio_path)
            print(f"🎧 Whisper ne {len(asr)} shabd pakde (script me {len(script_words)} shabd).")
        except Exception as e:
            print(f"⚠️ Whisper fail ({e}) — andaaze se timing banegi.")
            asr = []

        timings, ratio = align_timings(script_words, asr, audio_dur)
        print(f"🎯 Timing match: {ratio:.0%} shabd seedhe match hue.")

        roman = hinglish_words(script_words)
        captions = build_captions(script_words, roman, timings, cap_seconds)
        if not captions:
            print("⚠️ Koi caption nahi bana.")
            return False

        write_ass(captions, ass_path)
        if not burn_subtitles(video_path, ass_path, tmp_out) or not os.path.exists(tmp_out) \
                or os.path.getsize(tmp_out) < 10000:
            return False
        os.replace(tmp_out, video_path)
        print(f"✅ Hinglish subtitles video par lag gaye ({len(captions)} captions).")
        return True
    except Exception as e:
        print(f"❌ Subtitle step error: {e}")
        return False
    finally:
        for f in (tmp_out, ass_path):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
