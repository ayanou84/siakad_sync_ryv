import os
import time
import sys
import logging
import asyncio
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai.errors import APIError
from playwright.async_api import async_playwright

# ==========================================
# 0. KONFIGURASI LOGGING SYSTEM
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("siakad_bot.log", mode="a", encoding="utf-8")
    ]
)
logger = logging.getLogger("SIAKAD_BOT")

load_dotenv()
try:
    supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
except Exception as e:
    logger.error(f"Gagal menginisialisasi Supabase Client: {str(e)}")

# ==========================================
# 1. ENGINE LLM DENGAN LOCK MODEL & FAILOVER
# ==========================================
def generate_content_with_failover(prompt: str) -> str:
    # Model Utama: Gemini 3.5 Flash Lite (API ID: gemini-2.5-flash)
    try:
        logger.info("🤖 Mengirim prompt ke Model Utama: Gemini 3.5 Flash Lite...")
        client = genai.Client(api_key=os.getenv("GEMINI_PRIMARY_KEY"))
        response = client.models.generate_content(
            model='gemini-2.5-flash', 
            contents=prompt
        )
        logger.info("✅ Respon berhasil didapatkan dari Gemini 3.5 Flash Lite.")
        return response.text
    except APIError as e:
        if e.code == 429:
            logger.warning("⚠️ API Utama (3.5 Lite) terkena Rate Limit! Mengalihkan ke Cadangan...")
            # Model Cadangan: Gemini 3.1 Flash (API ID: gemini-1.5-flash)
            try:
                logger.info("🔄 Mengirim prompt ke Model Cadangan: Gemini 3.1 Flash...")
                client_backup = genai.Client(api_key=os.getenv("GEMINI_BACKUP_KEY"))
                response = client_backup.models.generate_content(
                    model='gemini-1.5-flash', 
                    contents=prompt
                )
                logger.info("✅ Respon berhasil didapatkan dari Gemini 3.1 Flash.")
                return response.text + "\n\n*(Responded by Gemini 3.1 Flash Backup)*"
            except Exception as err:
                logger.critical(f"❌ Kedua model AI Gagal/Limit: {str(err)}")
                return f"❌ Kedua API Key Limit/Eror: {str(err)}"
        else:
            logger.error(f"❌ Terjadi kesalahan API Google: {str(e)}")
            return f"❌ AI Error: {str(e)}"
    except Exception as general_err:
        logger.error(f"❌ Terjadi kesalahan sistem internal AI: {str(general_err)}")
        return f"❌ AI System Error: {str(general_err)}"

# ==========================================
# 2. ENGINE 1: HTTPX + BEAUTIFULSOUP (FAST)
# ==========================================
async def scrape_via_httpx(nim: str, password: str) -> str:
    logger.info("🚀 [ENGINE 1] Memulai percobaan via HTTPX + BeautifulSoup...")
    login_url = "https://siakad.mtu.ac.id"
    nilai_url = "https://siakad.mtu.ac.id"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        try:
            payload = {"username": nim, "password": password}
            logger.info(f"🔑 Mengirim POST request login ke {login_url}")
            await client.post(login_url, data=payload, headers=headers)
            
            logger.info(f"📄 Mengambil halaman internal nilai di {nilai_url}")
            nilai_res = await client.get(nilai_url, headers=headers)
            
            soup = BeautifulSoup(nilai_res.text, "html.parser")
            raw_text = soup.get_text(separator="\n", strip=True)
            
            if len(raw_text) < 500 or "masukkan username" in raw_text.lower() or "login" in raw_text.lower()[:300]:
                raise Exception("Deteksi gagal melewati dinding login atau konten kosong.")
                
            logger.info("✅ [ENGINE 1 SUCCESS] Konten halaman nilai berhasil diambil via HTTPX!")
            return raw_text
        except Exception as e:
            logger.warning(f"⚠️ [ENGINE 1 FAILED] HTTPX gagal mengeksekusi. Detail: {str(e)}")
            return "FAILED"

# ==========================================
# 3. ENGINE 2: PLAYWRIGHT (ROBUST FALLBACK)
# ==========================================
async def run_playwright_scraper(nim: str, password: str) -> str:
    logger.info("🔄 [ENGINE 2] Memulai Fallback Otomatis menggunakan Playwright...")
    async with async_playwright() as p:
        try:
            logger.info("🌐 Meluncurkan browser Chromium virtual...")
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context()
            page = await context.new_page()
            
            logger.info("🔗 Membuka halaman login siakad.mtu.ac.id")
            await page.goto("https://siakad.mtu.ac.id", timeout=30000)
            
            logger.info("⌨️ Mengisi form username dan password...")
            await page.fill("input[name='username']", nim)
            await page.fill("input[name='password']", password)
            
            logger.info("🖱️ Mengklik tombol submit dan menunggu navigasi...")
            await asyncio.gather(
                page.wait_for_navigation(wait_until="networkidle"),
                page.click("button[type='submit']")
            )
            
            logger.info("📄 Mengalihkan browser ke halaman nilai akademik...")
            await page.goto("https://siakad.mtu.ac.id", wait_until="domcontentloaded")
            
            raw_text = await page.locator("body").inner_text()
            logger.info("✅ [ENGINE 2 SUCCESS] Konten halaman sukses diekstrak oleh Playwright!")
            await browser.close()
            return raw_text
        except Exception as e:
            logger.error(f"❌ [ENGINE 2 CRITICAL ERROR] Playwright gagal. Detail: {str(e)}")
            return f"Error Scraper: {str(e)}"

# ==========================================
# 4. INTI PROSES (HYBRID MANAGEMENT)
# ==========================================
async def process_user_scrape(tg_id, nim, password, status_request=None, last_prompt=None):
    logger.info(f"⚡ Memulai antrean proses untuk ID Telegram: {tg_id} (NIM: {nim})")
    
    raw_content = await scrape_via_httpx(nim, password)
    
    if raw_content == "FAILED":
        raw_content = await run_playwright_scraper(nim, password)
        
    if "Error Scraper" in raw_content:
        logger.error(f"❌ Sinkronisasi gagal untuk NIM {nim}. Kedua mesin gagal membaca web.")
        return
        
    try:
        logger.info("💾 Menyimpan data terbaru ke tabel siakad_cache Supabase...")
        supabase.table("siakad_cache").upsert({"telegram_id": tg_id, "raw_data": raw_content, "updated_at": "now()"}).execute()
        logger.info("✅ Penyimpanan data ke database selesai.")
    except Exception as db_err:
        logger.error(f"❌ Gagal menulis data ke Supabase: {str(db_err)}")
        
    if status_request == 'NEED_SCRAPE' and last_prompt:
        logger.info("💬 Pemicu dari Chat Telegram Direct terdeteksi. Menyiapkan jawaban AI...")
        prompt = f"Data SIAKAD:\n{raw_content}\n\nPertanyaan Mahasiswa: {last_prompt}"
        jawaban_ai = generate_content_with_failover(prompt)
        
        try:
            telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
            logger.info(f"📤 Mengirimkan jawaban akhir ke Chat Telegram ID: {tg_id}")
            import requests
            requests.post(f"https://telegram.org{telegram_token}/sendMessage", json={
                "chat_id": tg_id, "text": jawaban_ai, "parse_mode": "Markdown"
            })
        except Exception as tg_err:
            logger.error(f"❌ Gagal mengirim pesan balik ke API Telegram: {str(tg_err)}")

    try:
        supabase.table("users").update({"status_request": "IDLE"}).eq("telegram_id", tg_id).execute()
        logger.info(f"🏁 Status pengguna {tg_id} berhasil dikembalikan ke 'IDLE'.")
    except Exception as db_err:
        logger.error(f"❌ Gagal mengupdate status user ke IDLE: {str(db_err)}")

# ==========================================
# 5. RUNNERS
# ==========================================
async def main_github_cron():
    logger.info("⏰ Memulai eksekusi mode: GITHUB ACTIONS CRON JOB SUBUR")
    nim = os.getenv("SIAKAD_USER")
    password = os.getenv("SIAKAD_PASSWORD")
    
    if not nim or not password:
        logger.critical("❌ Kredensial SIAKAD_USER atau SIAKAD_PASSWORD kosong di GitHub Secrets!")
        return

    res = supabase.table("users").select("telegram_id").limit(1).execute()
    tg_id = res.data[0]['telegram_id'] if res.data else 12345
    await process_user_scrape(tg_id, nim, password)

async def main_vps_loop():
    logger.info("🤖 Memulai eksekusi mode: VPS MONITOR DAEMON LOOP (Active 24/7)")
    while True:
        try:
            res = supabase.table("users").select("*").eq("status_request", "NEED_SCRAPE").execute()
            if res.data:
                for user in res.data:
                    logger.info(f"🔔 Notifikasi masuk! Permintaan paksa refresh dari User {user['telegram_id']}")
                    await process_user_scrape(
                        user['telegram_id'], user['nim'], user['encrypted_password'], 
                        user['status_request'], user['last_prompt']
                    )
        except Exception as loop_err:
            logger.error(f"❌ Terjadi gangguan pada pembacaan loop database VPS: {str(loop_err)}")
        time.sleep(5)

if __name__ == "__main__":
    if os.getenv("GITHUB_ACTIONS") == "true":
        asyncio.run(main_github_cron())
    else:
        asyncio.run(main_vps_loop())
