import os
import re
import json
import time
import asyncio
import requests
import subprocess
from bs4 import BeautifulSoup
from g4f.client import Client
import edge_tts
from PIL import Image, ImageDraw, ImageFont

# ==========================================
# PATCH: Fix moviepy ANTIALIAS issue for Pillow 10.0.0+
# ==========================================
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.Resampling.LANCZOS

from moviepy.editor import ImageClip, AudioFileClip, VideoFileClip
from moviepy.video.fx import resize
from playwright.sync_api import sync_playwright

# Google API Imports
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ==========================================
# 1. Configuration & Setup
# ==========================================
client = Client()
SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
MAX_CHUNK_CHARACTERS = 1000
MAX_RETRIES = 3


def get_youtube_service():
    """YouTube API OAuth Authentication Setup with Error Handling"""
    creds = None
    token_file = 'token.json'
   
    try:
        if os.path.exists(token_file):
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
           
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists('client_secrets.json'):
                    print("❌ Error: 'client_secrets.json' file missing in folder.")
                    return None
                flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
                creds = flow.run_local_server(port=0)
               
            with open(token_file, 'w') as token:
                token.write(creds.to_json())
               
        return build('youtube', 'v3', credentials=creds)
    except Exception as e:
        print(f"⚠️ YouTube Auth Error: {e}")
        return None


# ==========================================
# 2. Content Type Detection
# ==========================================
def detect_content_type(title, content):
    content_lower = content.lower()
    title_lower = title.lower()
   
    result_keywords = ['result', 'रिजल्ट', 'नतीजा', 'cut off', 'कट ऑफ', 'score', 'स्कोर']
    admit_keywords = ['admit card', 'एडमिट कार्ड', 'hall ticket', 'हॉल टिकट', 'call letter', 'कॉल लेटर']
    vacancy_keywords = ['vacancy', 'वैकेंसी', 'notification', 'नोटिफिकेशन', 'apply', 'आवेदन', 'recruitment', 'भर्ती']
   
    title_score = {'result': 0, 'admit': 0, 'vacancy': 0}
    content_score = {'result': 0, 'admit': 0, 'vacancy': 0}
   
    for kw in result_keywords:
        if kw in title_lower: title_score['result'] += 2
        if kw in content_lower: content_score['result'] += 1
           
    for kw in admit_keywords:
        if kw in title_lower: title_score['admit'] += 2
        if kw in content_lower: content_score['admit'] += 1
           
    for kw in vacancy_keywords:
        if kw in title_lower: title_score['vacancy'] += 2
        if kw in content_lower: content_score['vacancy'] += 1
   
    total_score = {
        'result': title_score['result'] + content_score['result'],
        'admit': title_score['admit'] + content_score['admit'],
        'vacancy': title_score['vacancy'] + content_score['vacancy']
    }
   
    max_type = max(total_score, key=total_score.get)
   
    if total_score[max_type] < 2:
        date_pattern = r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}'
        if re.search(date_pattern, content):
            if 'available' in content_lower or 'जारी' in content:
                return 'result'
            else:
                return 'admit'
        else:
            return 'vacancy'
   
    return max_type


# ==========================================
# 3. Robust Blogger Post Scraper
# ==========================================
def extract_blogger_content(url):
    print(f"🔍 Scraping content from: {url}")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    html_content = None
    for attempt in range(1, 3):
        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            html_content = response.text
            break
        except Exception as e:
            print(f"⚠️ Requests attempt {attempt} failed: {e}")
            time.sleep(2)
    if not html_content:
        print("🌐 Switching to Playwright headless scraper for rendering...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                html_content = page.content()
                browser.close()
        except Exception as e:
            print(f"❌ Playwright Fallback also failed: {e}")
    if not html_content:
        print("❌ Could not fetch webpage HTML.")
        return None, None
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        title_element = (
            soup.find('h1', class_='post-title') or
            soup.find('h3', class_='post-title') or
            soup.find('h1', class_='entry-title') or
            soup.find('meta', property='og:title') or
            soup.find('h1')
        )
       
        if title_element:
            if title_element.name == 'meta':
                raw_title = title_element.get('content', '').strip()
            else:
                raw_title = title_element.get_text(strip=True)
        else:
            raw_title = soup.title.string.strip() if soup.title else "Job Update"
        clean_title = re.sub(
            r'(\s*[-|:_]\s*BG_ALL_GOVT_JOB.*|\s*[-|:_]\s*BK\s*GrowUp.*|\s*[-|:_]\s*ALL\s*GOVT\s*JOB.*)',
            '',
            raw_title,
            flags=re.IGNORECASE
        ).strip()
       
        if not clean_title or len(clean_title) < 5:
            clean_title = "Govt Job Latest Notification Update"
        body = soup.find('div', class_='post-body') or soup.find('div', class_='entry-content') or soup.find('article')
        if body:
            paragraphs = [p.get_text(strip=True) for p in body.find_all(['p', 'li', 'div']) if p.get_text(strip=True)]
            content_text = "\n".join(paragraphs)
        else:
            content_text = soup.get_text(strip=True)
           
        print(f"✅ Clean Post Title Extracted: {clean_title}")
        return clean_title, content_text[:4000]
       
    except Exception as e:
        print(f"❌ Parsing Error: {e}")
        return None, None


# ==========================================
# Google GenAI Imports (Add this at top of file)
# ==========================================
from google import genai
from google.genai import types

# ==========================================
# Gemini API Keys — ab GitHub Secrets se aayenge (hardcoded nahi)
# 3 keys: 1st fail -> 2nd try, 2nd fail -> 3rd try
# ==========================================
GEMINI_API_KEYS = [
    os.environ.get("GEMINI_API_KEY_1", "").strip(),
    os.environ.get("GEMINI_API_KEY_2", "").strip(),
    os.environ.get("GEMINI_API_KEY_3", "").strip(),
]
GEMINI_API_KEYS = [k for k in GEMINI_API_KEYS if k]  # khali keys hata do
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-3.6-flash")

# ==========================================
# Telegram Notifier — status aur error dono yaha se jaate hai
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def send_telegram(message):
    """Telegram par status/error bhejta hai. Agar token/chat id set nahi hai to sirf print karega."""
    print(f"📨 {message}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message[:4000]},
            timeout=15,
        )
    except Exception as e:
        print(f"⚠️ Telegram message bhejne me error: {e}")

# ==========================================
# 4. AI Generator (Gemini Primary + Fallback + g4f)
# ==========================================
def format_script_paragraphs(script):
    lines = script.split('\n')
    paragraphs = []
    current_para = []
   
    for line in lines:
        line = line.strip()
        if not line:
            if current_para:
                paragraphs.append(' '.join(current_para))
                current_para = []
            continue
       
        if line.endswith(':') or len(line) < 15:
            if current_para:
                paragraphs.append(' '.join(current_para))
                current_para = []
            paragraphs.append(line)
            continue
       
        current_para.append(line)
   
    if current_para:
        paragraphs.append(' '.join(current_para))
   
    return '\n\n'.join(paragraphs)


def generate_youtube_assets(title, content, max_retries=3):
    print("🤖 Generating High-CTR Title, Dynamic Clickbait Script & SEO Assets...")
    content_type = detect_content_type(title, content)
    print(f"📊 Detected Content Type: {content_type.upper()}")
   
    type_prompts = {
        'result': "CRITICAL: RESULT ANNOUNCEMENT - Focus on urgency, shock, cut-off shock, and direct score check.",
        'admit': "CRITICAL: ADMIT CARD RELEASED - Focus on exam date urgency, hall ticket release, and last-minute tips.",
        'vacancy': "CRITICAL: NEW BIG VACANCY - Focus on huge seats, age relaxation, salary, and urgent last date."
    }
   
    prompt = f"""
    You are a highly energetic YouTube Creator for channel 'BK GrowUp'.
    Create viral YouTube video assets based on this Educational / Govt Job Post:
    BLOG TITLE: {title}
    BLOG CONTENT:
    {content}
   
    CONTENT TYPE: {content_type.upper()}
    {type_prompts.get(content_type, '')}

    CRITICAL SCRIPT & LANGUAGE RULES:
    1. SCRIPT LANGUAGE & TONE: Dynamic, conversational spoken Hinglish/Hindi (Youth Hindi). Avoid heavy pure Sanskritized Hindi words (like 'आयोजन', 'प्रवेश पत्र', 'सत्यापन', 'सुलभ'). Replace with natural words (Admit Card, Cutoff, Exam Date, Verification, Download, Site Link, Direct).
    2. DYNAMIC CLICKBAIT HOOK (IMPORTANT): DO NOT use a fixed repeated intro! Create a unique, high-energy, high-curiosity Hook specific to this post title and topic in the first 2 sentences. Create shock/urgency (e.g., "अरे भाई! रेलवे वालों के लिए अचानक बड़ी खबर निकल कर आ रही है...", "अगर आपने भी फॉर्म भरा था तो रुक जाओ, अभी-अभी बड़ा अपडेट आया है...").
    3. EXPLANATION STYLE: Explain everything clearly, step-by-step, in a practical tone (e.g., "स्क्रीन पर दी गई इस टेबल पर नजर डालो...", "कम से कम 2 से 3 प्रिंटआउट निकालकर रख लो...").
    4. SCRIPT LENGTH & WORD COUNT: The script MUST be 500 to 650 words long to guarantee a video length of 3 to 4 minutes when spoken.
    5. PARAGRAPHS: Write the script broken down into clear, readable paragraphs (3 to 5 sentences per paragraph).
    6. TITLES: Create 3 high-CTR, viral, clickbait Hindi/Hinglish titles with emojis. Do NOT include site header names like 'BG_ALL_GOVT_JOB' in titles.

    OUTPUT FORMAT (Strictly valid raw JSON without markdown formatting):
    {{
      "seo_title": ["Title 1", "Title 2", "Title 3"],
      "video_script": "Full simple Hindi script in PARAGRAPHS...",
      "seo_description": "SEO description with emojis...",
      "tags": ["tag1", "tag2", "tag3"],
      "hashtags": "#hashtag1 #hashtag2 #hashtag3"
    }}
    """

    # ---------------------------------------------------------
    # OPTION 1: Gemini API — 3 Key Fallback (key1 fail -> key2 -> key3)
    # ---------------------------------------------------------
    if not GEMINI_API_KEYS:
        send_telegram("⚠️ Koi Gemini API key set nahi hai (GEMINI_API_KEY_1/2/3). g4f fallback try kiya jayega.")

    for key_index, api_key in enumerate(GEMINI_API_KEYS, start=1):
        try:
            gemini_client = genai.Client(api_key=api_key)
        except Exception as e:
            print(f"⚠️ Gemini Key #{key_index} client init fail: {e}")
            continue

        for attempt in range(1, max_retries + 1):
            try:
                print(f"⚡ [Gemini Key #{key_index}] {GEMINI_MODEL_NAME} Attempt {attempt}/{max_retries}...")
                response = gemini_client.models.generate_content(
                    model=GEMINI_MODEL_NAME,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.8,
                    ),
                )

                raw_text = response.text.strip()
                clean_json = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE)
                data = json.loads(clean_json)

                if data.get("video_script"):
                    data["video_script"] = format_script_paragraphs(data["video_script"])

                if data.get("video_script") and data.get("seo_title"):
                    print(f"✅ AI Package generated using Gemini Key #{key_index}!")
                    return data
            except Exception as e:
                print(f"⚠️ Gemini Key #{key_index} Attempt {attempt} failed ({e}).")
                time.sleep(2)

        # Is key ke saare retries fail ho gaye -> agli key try karo
        send_telegram(f"⚠️ Gemini Key #{key_index} kaam nahi kar rahi, agli key try ho rahi hai...")

    # ---------------------------------------------------------
    # OPTION 2: Secondary / Fallback - g4f Library
    # ---------------------------------------------------------
    print("🔄 Gemini API models failed/unavailable. Switching to [Option 2] g4f Library Fallback...")
    for attempt in range(1, max_retries + 1):
        try:
            print(f"🔄 g4f Generation Attempt {attempt}/{max_retries}...")
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8
            )
           
            raw_text = response.choices[0].message.content.strip()
            clean_json = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE)
            data = json.loads(clean_json)
           
            if data.get("video_script"):
                data["video_script"] = format_script_paragraphs(data["video_script"])
           
            if data.get("video_script") and data.get("seo_title"):
                print(f"✅ AI Package generated successfully using g4f Fallback!")
                return data
        except Exception as e:
            print(f"⚠️ g4f Attempt {attempt} failed ({e}). Retrying...")
            time.sleep(2)
           
    print("❌ AI Generation Error: Both Gemini API and g4f Library failed.")
    return None

# ==========================================
# 5. Smart Script Splitter (Strict 1000 Chars Limit)
# ==========================================
def split_script_into_chunks(script, max_chars=MAX_CHUNK_CHARACTERS):
    paragraphs = [p.strip() for p in script.split('\n\n') if p.strip()]
    chunks = []
    current_chunk = ""
    for p in paragraphs:
        if len(current_chunk) + len(p) + 2 <= max_chars:
            current_chunk += (p + "\n\n")
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
           
            if len(p) > max_chars:
                sentences = re.split(r'([।!?\n])', p)
                sub_chunk = ""
                for i in range(0, len(sentences), 2):
                    sentence = sentences[i]
                    punct = sentences[i+1] if i+1 < len(sentences) else ""
                    full_sentence = sentence + punct
                   
                    if len(sub_chunk) + len(full_sentence) <= max_chars:
                        sub_chunk += full_sentence
                    else:
                        if sub_chunk:
                            chunks.append(sub_chunk.strip())
                        sub_chunk = full_sentence
                if sub_chunk:
                    chunks.append(sub_chunk.strip())
            else:
                current_chunk = p + "\n\n"
    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks


# ==========================================
# 6. OpenAI.fm Direct API + Edge TTS Fallback (FIXED)
# ==========================================
def generate_fable_voice_openai_fm(text_chunk, output_path):
    """
    Direct call to openai.fm/api/generate — reliable full audio download.
    Playwright network interception was capturing only partial chunks.
    """
    try:
        url = "https://www.openai.fm/api/generate"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Origin": "https://www.openai.fm",
            "Referer": "https://www.openai.fm/",
            "Accept": "*/*",
        }
        
        # Primary: POST with form-data
        files = {
            "input": (None, text_chunk),
            "voice": (None, "fable"),
            "prompt": (None, "Speak clearly in a natural, friendly Indian Hindi male tone. Moderate pace, clear pronunciation."),
            "vibe": (None, "audio")
        }
        
        response = requests.post(url, files=files, headers=headers, timeout=90, stream=True)
        
        # Fallback to GET if POST fails
        if response.status_code != 200:
            params = {
                "input": text_chunk,
                "voice": "fable",
                "prompt": "Speak clearly in a natural, friendly Indian Hindi male tone. Moderate pace, clear pronunciation."
            }
            response = requests.get(url, params=params, headers=headers, timeout=90, stream=True)
        
        response.raise_for_status()
        
        content_type = response.headers.get("content-type", "").lower()
        if "audio" not in content_type and "mpeg" not in content_type and "wav" not in content_type and "octet-stream" not in content_type:
            raise Exception(f"Unexpected content-type: {content_type}")
        
        # Write complete response body
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        # Validate size (very small files are incomplete)
        file_size = os.path.getsize(output_path)
        if file_size < 8000:  # roughly less than 0.5 second of speech
            raise Exception(f"Downloaded audio too small ({file_size} bytes) — incomplete response")
        
        return True
        
    except Exception as e:
        print(f"⚠️ OpenAI.fm Direct API Error: {e}")
        return False


def generate_edge_tts_voice(text, output_audio_path):
    voice = "hi-IN-MadhurNeural"
   
    async def _save():
        communicate = edge_tts.Communicate(text, voice, rate="+0%")
        await communicate.save(output_audio_path)
       
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_save())
    loop.close()


def generate_male_voice_with_ffmpeg(text, output_audio_path):
    print("🎙️ Generating Natural Hindi Voiceover...")
    temp_dir = "temp_voice"
    os.makedirs(temp_dir, exist_ok=True)
   
    chunks = split_script_into_chunks(text, max_chars=MAX_CHUNK_CHARACTERS)
    print(f"🧩 Script split into {len(chunks)} part(s) [Limit: {MAX_CHUNK_CHARACTERS} chars/part].")
   
    audio_parts = []
    fable_failed = False
    
    for idx, chunk in enumerate(chunks, start=1):
        part_filename = os.path.join(temp_dir, f"part_{idx:03d}.mp3")
        success = False
       
        if not fable_failed:
            for attempt in range(1, MAX_RETRIES + 1):
                print(f"🎙️ Generating Part {idx}/{len(chunks)} ({len(chunk)} chars) with OpenAI.fm (Fable Voice) - Attempt {attempt}...")
                if generate_fable_voice_openai_fm(chunk, part_filename):
                    # Extra size check
                    if os.path.getsize(part_filename) >= 8000:
                        success = True
                        break
                    else:
                        print(f"⚠️ Part {idx} file too small after download. Retrying...")
                time.sleep(2)
           
            if not success:
                print("⚠️ OpenAI.fm voice generation failed. Falling back to Edge TTS (hi-IN-MadhurNeural).")
                fable_failed = True
        
        if fable_failed or not success:
            try:
                print(f"🔊 Generating Part {idx}/{len(chunks)} via Edge TTS Fallback...")
                generate_edge_tts_voice(chunk, part_filename)
                success = True
            except Exception as e:
                print(f"❌ Part {idx} generation completely failed: {e}")
                return False
        
        audio_parts.append(part_filename)
    
    if audio_parts:
        print("🔗 Concatenating and Merging All Audio Parts sequentially...")
        list_file = os.path.join(temp_dir, "concat_list.txt")
       
        with open(list_file, "w", encoding="utf-8") as f:
            for p in audio_parts:
                clean_p = os.path.abspath(p).replace('\\', '/')
                f.write(f"file '{clean_p}'\n")
        
        try:
            # Re-encode to MP3 with fixed CBR to prevent broken headers/durations
            cmd_concat = [
                'ffmpeg', '-f', 'concat', '-safe', '0', '-i', list_file,
                '-af', 'loudnorm=I=-16:LRA=11:TP=-1.5',
                '-ar', '44100',
                '-ac', '2',
                '-b:a', '128k',
                '-c:a', 'libmp3lame',
                '-write_xing', '0',
                '-y', output_audio_path
            ]
            subprocess.run(cmd_concat, capture_output=True, check=True)
            print(f"🔊 Final Audio merged successfully: {output_audio_path}")
            return True
        except Exception as e:
            print(f"❌ Audio Joining Error: {e}")
            return False
        finally:
            for f in audio_parts + [list_file]:
                if os.path.exists(f):
                    try:
                        os.remove(f)
                    except:
                        pass
    return False


# ==========================================
# 7. Video Compression & Assembly
# ==========================================
def ensure_1080p_ffmpeg(input_path, output_path):
    try:
        cmd = [
            'ffmpeg', '-i', input_path,
            '-vf', 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2',
            '-c:v', 'libx264', '-c:a', 'aac', '-b:a', '128k', '-preset', 'fast', '-y', output_path
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        return output_path
    except Exception:
        return input_path


def compress_video_with_ffmpeg(input_path, output_path, target_size_mb=95):
    current_size = os.path.getsize(input_path) / (1024 * 1024)
    if current_size <= target_size_mb:
        print(f"✅ Video size optimal: {current_size:.1f}MB")
        return ensure_1080p_ffmpeg(input_path, output_path)
   
    print(f"📦 Compressing Video from {current_size:.1f}MB...")
    temp_output = output_path.replace('.mp4', '_temp_compress.mp4')
   
    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2',
        '-c:v', 'libx264', '-b:v', '1200k', '-maxrate', '2000k', '-bufsize', '4000k',
        '-c:a', 'aac', '-b:a', '96k', '-r', '24', '-preset', 'fast', '-y', temp_output
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        if os.path.exists(output_path):
            os.remove(output_path)
        os.rename(temp_output, output_path)
        return output_path
    except Exception as e:
        print(f"⚠️ Compression failed, fallback to 1080p ensure: {e}")
        return ensure_1080p_ffmpeg(input_path, output_path)


def record_website_video(url, output_clip_path, target_duration):
    print(f"📹 Auto-Recording Website for {target_duration:.1f}s...")
    temp_dir = "temp_rec"
    os.makedirs(temp_dir, exist_ok=True)
    keywords = ["Result", "Admit Card", "Vacancy", "Apply", "Notification", "Download", "Eligibility"]
   
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                record_video_dir=temp_dir,
                record_video_size={'width': 1920, 'height': 1080}
            )
           
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
            page.evaluate("document.body.style.zoom = '1.4'")
            time.sleep(1)
           
            js_code = """
            (keywords) => {
                keywords.forEach(kw => {
                    const regex = new RegExp(`(${kw})`, 'gi');
                    const elements = document.querySelectorAll('p, li, span, td, h1, h2, h3, div, a, strong');
                    elements.forEach(el => {
                        if (el.children.length === 0 && el.innerText && regex.test(el.innerText)) {
                            el.innerHTML = el.innerText.replace(
                                regex,
                                '<mark style="background-color: #fef08a; color: #000; padding: 2px 4px; font-weight: bold;">$1</mark>'
                            );
                        }
                    });
                });
            }
            """
            page.evaluate(js_code, keywords)
            time.sleep(1)
           
            start_time = time.time()
            max_duration = min(target_duration, 240)
           
            page_height = page.evaluate("document.body.scrollHeight")
            viewport_height = page.evaluate("window.innerHeight")
            total_scroll = max(0, page_height - viewport_height)
           
            scroll_step = 1.5
            current_scroll = 0
            direction = 1
           
            while time.time() - start_time < max_duration:
                current_scroll += scroll_step * direction
                if current_scroll > total_scroll:
                    current_scroll = total_scroll
                    direction = -1
                elif current_scroll < 0:
                    current_scroll = 0
                    direction = 1
               
                page.evaluate(f"window.scrollTo(0, {current_scroll})")
                time.sleep(0.04)
            rec_path = page.video.path()
            context.close()
            browser.close()
           
            if os.path.exists(rec_path):
                if os.path.exists(output_clip_path):
                    os.remove(output_clip_path)
                os.rename(rec_path, output_clip_path)
                print(f"✅ Website Recording Saved!")
                return True
               
    except Exception as e:
        print(f"⚠️ Screen Recording Warning: {e}")
        return False


def get_audio_duration_robust(audio_path):
    """
    4-Layer Fallback method to get the true duration of the audio file in seconds.
    """
    # 1. FFprobe Stream Duration
    try:
        cmd = [
            'ffprobe', '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', audio_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        dur = float(res.stdout.strip())
        if dur > 2.0:
            return dur
    except Exception:
        pass
    
    # 2. FFprobe Format Duration
    try:
        cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', audio_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        dur = float(res.stdout.strip())
        if dur > 2.0:
            return dur
    except Exception:
        pass
    
    # 3. MoviePy AudioFileClip
    try:
        a = AudioFileClip(audio_path)
        dur = float(a.duration)
        a.close()
        if dur > 2.0:
            return dur
    except Exception:
        pass
    
    # 4. File Size Estimation Fallback (128kbps MP3 calculation)
    try:
        file_size_bytes = os.path.getsize(audio_path)
        # 128 kbps = 16000 bytes per second
        estimated_dur = file_size_bytes / 16000.0
        if estimated_dur > 2.0:
            return estimated_dur
    except Exception:
        pass
    
    return 60.0  # Safe Default


def create_video_with_ffmpeg(audio_path, post_url, video_title, output_video_path):
    print("🎬 Assembling 1080p HD Video...")
    temp_img_path = "temp_frame_1080p.png"
    web_clip_path = "temp_website_clip.mp4"
    temp_video_path = output_video_path.replace('.mp4', '_temp.mp4')
   
    try:
        # ⚡ 4-Layer Robust Duration Fetch
        exact_duration = get_audio_duration_robust(audio_path)
        duration = min(exact_duration, 240)
        print(f"⏱️ Calculated Exact Audio Duration: {duration:.2f} seconds")
        
        audio = AudioFileClip(audio_path)
        record_success = record_website_video(post_url, web_clip_path, duration)
       
        if record_success and os.path.exists(web_clip_path):
            bg_video = VideoFileClip(web_clip_path)
            if bg_video.size != (1920, 1080):
                bg_video = bg_video.resize(newsize=(1920, 1080))
           
            bg_video = bg_video.loop(duration=duration) if bg_video.duration < duration else bg_video.subclip(0, duration)
            final_video = bg_video.set_audio(audio)
        else:
            img = Image.new('RGB', (1920, 1080), color=(15, 23, 42))
            draw = ImageDraw.Draw(img)
            draw.rectangle([(60, 50), (1860, 1030)], outline=(234, 179, 8), width=5)
           
            try:
                font = ImageFont.truetype("arial.ttf", 40)
            except:
                font = ImageFont.load_default()
           
            draw.text((140, 150), "📢 BK GROWUP - GOVT JOB UPDATE", fill=(234, 179, 8), font=font)
            clean_display_title = video_title[:55] + "..." if len(video_title) > 55 else video_title
            draw.text((140, 250), clean_display_title, fill=(255, 255, 255), font=font)
            draw.text((140, 350), "🔗 Apply Link In Description", fill=(100, 200, 255), font=font)
           
            img.save(temp_img_path)
            final_video = ImageClip(temp_img_path).set_duration(duration).set_audio(audio)
       
        final_video.write_videofile(temp_video_path, fps=24, codec='libx264', audio_codec='aac', verbose=False, logger=None)
       
        final_video.close()
        audio.close()
        if 'bg_video' in locals():
            bg_video.close()
       
        compressed_path = compress_video_with_ffmpeg(temp_video_path, output_video_path)
        return True
       
    except Exception as e:
        print(f"❌ Video Assembly Error: {e}")
        return False
    finally:
        for f in [temp_img_path, web_clip_path, temp_video_path]:
            if os.path.exists(f) and f != output_video_path:
                try:
                    os.remove(f)
                except:
                    pass


# ==========================================
# 8. YouTube Auto Uploader
# ==========================================
def upload_to_youtube(video_path, title, description, tags):
    print("📤 Uploading Video to YouTube as UNLISTED...")
    youtube = get_youtube_service()
    if not youtube:
        print("⚠️ YouTube Upload Skipped.")
        return None
    tags_list = tags if isinstance(tags, list) else [t.strip() for t in str(tags).split(',')]
    body = {
        'snippet': {
            'title': title[:98],
            'description': description[:4900],
            'tags': [t[:30] for t in tags_list[:20]],
            'categoryId': '27'
        },
        'status': {
            'privacyStatus': 'unlisted',
            'selfDeclaredMadeForKids': False
        }
    }
    try:
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
        request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
       
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"⏳ Upload Progress: {int(status.progress() * 100)}%")
        video_id = response.get('id')
        print(f"✅ Video Uploaded Successfully! URL: https://youtu.be/{video_id}")
        return f"https://youtu.be/{video_id}"
    except Exception as e:
        print(f"❌ YouTube Upload Failed: {e}")
        return None


def get_post_url():
    """URL 3 jagah se aa sakta hai: env var (GitHub Actions/Telegram), CLI argument, ya manual input (local test)."""
    import sys
    if os.environ.get("POST_URL", "").strip():
        return os.environ["POST_URL"].strip()
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    return input("\n🔗 Enter Blogger Post URL: ").strip()


# ==========================================
# 9. Main Workflow
# ==========================================
def main():
    print("=" * 50)
    print("🚀 BK GrowUp Auto Job Bot + Safe YouTube Uploader")
    print("=" * 50)

    post_url = get_post_url()

    if post_url:
        send_telegram(f"🚀 Job shuru hua:\n{post_url}")
        blog_title, blog_content = extract_blogger_content(post_url)

        if not (blog_content and blog_title):
            send_telegram(f"❌ Page scrape nahi ho paya:\n{post_url}")

        if blog_content and blog_title:
            ai_data = generate_youtube_assets(blog_title, blog_content)

            if not ai_data:
                send_telegram(f"❌ AI script/title generate nahi ho paya (Gemini + g4f dono fail):\n{post_url}")

            if ai_data:
                output_dir = "bot_outputs"
                os.makedirs(output_dir, exist_ok=True)
               
                clean_name_raw = re.sub(r'[^\w\s-]', '', blog_title)
                filtered_words = [
                    w for w in clean_name_raw.split()
                    if w.lower() not in ['bg_all_govt_job', 'bg', 'all', 'govt', 'job', 'bk', 'growup', 'http', 'https']
                ]
               
                if filtered_words:
                    filename_base = "_".join(filtered_words[:5])
                else:
                    filename_base = f"Job_Notification_{int(time.time())}"
                
                txt_file = os.path.join(output_dir, f"{filename_base}_package.txt")
                audio_file = os.path.join(output_dir, f"{filename_base}_audio.mp3")
                video_file = os.path.join(output_dir, f"{filename_base}_video.mp4")
               
                titles = ai_data.get("seo_title", [])
                selected_title = titles[0] if isinstance(titles, list) and titles else blog_title
                seo_desc = f"{ai_data.get('seo_description', '')}\n\n{ai_data.get('hashtags', '')}"
                tags = ai_data.get('tags', [])
                script = ai_data.get('video_script', '')
               
                with open(txt_file, "w", encoding="utf-8") as f:
                    f.write(f"SELECTED TITLE: {selected_title}\n\n")
                    f.write("--- SCRIPT ---\n")
                    f.write(f"{script}\n\n")
                    f.write("--- SEO DESCRIPTION ---\n")
                    f.write(f"{seo_desc}\n\n")
                    f.write("--- TAGS ---\n")
                    f.write(f"{', '.join(tags)}\n")
               
                print(f"📄 Assets package saved at: {txt_file}")
                
                # 1. Voiceover Generation
                voice_success = generate_male_voice_with_ffmpeg(script, audio_file)
               
                # 2. Video Assembly
                if voice_success and os.path.exists(audio_file):
                    video_success = create_video_with_ffmpeg(audio_file, post_url, selected_title, video_file)
                   
                    # 3. YouTube Uploading
                    if video_success and os.path.exists(video_file):
                        yt_url = upload_to_youtube(video_file, selected_title, seo_desc, tags)
                        if yt_url:
                            send_telegram(f"✅ Video upload ho gaya!\n{selected_title}\n{yt_url}")
                        else:
                            send_telegram(f"❌ Video ban gaya lekin YouTube upload fail ho gaya:\n{selected_title}")
                    else:
                        send_telegram(f"❌ Video assembly fail ho gaya, upload skip:\n{post_url}")
                else:
                    send_telegram(f"❌ Voice generation fail ho gaya, video/upload skip:\n{post_url}")
    else:
        send_telegram("❌ Koi URL nahi mila (POST_URL env var ya input khali tha).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        err_text = f"❌ Bot crash ho gaya:\n{e}\n\n{traceback.format_exc()[-1500:]}"
        print(err_text)
        send_telegram(err_text)
        raise