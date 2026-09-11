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
# 1. ENGINE LLM DENGAN FAILOVER
# ==========================================
def generate_content_with_failover(prompt: str) -> str:
    try:
        logger.info("🤖 Mengirim prompt ke Model Utama: Gemini Flash...")
        client = genai.Client(api_key=os.getenv("GEMINI_PRIMARY_KEY"))
        response = client.models.generate_content(
            model='gemini-1.5-flash', 
            contents=prompt
        )
        logger.info("✅ Respon berhasil didapatkan dari Gemini Model Utama.")
        return response.text
    except APIError as e:
        if e.code == 429:
            logger.warning("⚠️ API Utama terkena Rate Limit! Mengalihkan ke Cadangan...")
            try:
                client_backup = genai.Client(api_key=os.getenv("GEMINI_BACKUP_KEY"))
                response = client_backup.models.generate_content(
                    model='gemini-1.5-flash', 
                    contents=prompt
                )
                return response.text + "\n\n*(Responded by Backup Model)*"
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
    login_url = "https://siakad.mtu.ac.id/index.php/login"
    dashboard_url = "https://siakad.mtu.ac.id/index.php/login"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": login_url
    }
    
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        try:
            # Menggunakan atribut 'identity' dan 'password' sesuai form HTML asli
            payload = {"identity": nim, "password": password}
            logger.info(f"🔑 Mengirim POST request login ke {login_url}")
            res_login = await client.post(login_url, data=payload, headers=headers)
            
            logger.info(f"📄 Status HTTP Login: {res_login.status_code}")
            
            logger.info(f"📄 Mengambil halaman internal nilai di {dashboard_url}")
            nilai_res = await client.get(dashboard_url, headers=headers)
            
            soup = BeautifulSoup(nilai_res.text, "html.parser")
            raw_text = soup.get_text(separator="\n", strip=True)
            
            if len(raw_text) < 500 or "nama pengguna atau email" in raw_text.lower():
                raise Exception("Masih tertahan di laman login atau terkena blokir 403.")
                
            logger.info("✅ [ENGINE 1 SUCCESS] Konten halaman nilai berhasil diambil via HTTPX!")
            return raw_text
        except Exception as e:
            logger.warning(f"⚠️ [ENGINE 1 FAILED] HTTPX gagal mengeksekusi. Detail: {str(e)}")
            return "FAILED"

# ==========================================
# 3. ENGINE 2: PLAYWRIGHT (ROBUST FALLBACK + DEBUG)
# ==========================================
async def run_playwright_scraper(nim: str, password: str) -> str:
    logger.info("🔄 [ENGINE 2] Memulai Fallback Otomatis menggunakan Playwright...")
    async with async_playwright() as p:
        try:
            logger.info("🌐 Meluncurkan browser Chromium virtual...")
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            target_url = "https://siakad.mtu.ac.id/index.php/login"
            logger.info(f"🔗 Membuka halaman login {target_url}")
            response = await page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            
            logger.info(f"📡 Status HTTP Response: {response.status if response else 'No Response'}")
            
            # Simpan screenshot awal halaman login
            await page.screenshot(path="debug_login_page.png")
            
            # Mengisi form login menggunakan selector name='identity'
            logger.info("⌨️ Mengisi form identity dan password...")
            await page.fill("input[name='identity']", nim, timeout=10000)
            await page.fill("input[name='password']", password, timeout=10000)
            
            logger.info("🖱️ Mengklik tombol submit dan menunggu navigasi...")
            await asyncio.gather(
                page.wait_for_navigation(wait_until="networkidle"),
                page.click("button[type='submit']")
            )
            
            # Simpan screenshot setelah submit login
            await page.screenshot(path="debug_after_login.png")
            
            raw_text = await page.locator("body").inner_text()
            logger.info("✅ [ENGINE 2 SUCCESS] Konten halaman sukses diekstrak oleh Playwright!")
            await browser.close()
            return raw_text
        except Exception as e:
            try:
                await page.screenshot(path="error_playwright.png")
                logger.info("📸 Error screenshot berhasil disimpan ke 'error_playwright.png'")
            except:
                pass
            logger.error(f"❌ [ENGINE 2 CRITICAL ERROR] Playwright gagal. Detail: {str(e)}")
            await browser.close()
            return f"Error Scraper: {str(e)}"

# ==========================================
# 4. INTI PROSES & RUNNER
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
        supabase.table("siakad_cache").upsert({"telegram_id": tg_id, "raw_data": raw_content, "updated_at": "now()"}).execute()
        logger.info("✅ Penyimpanan data ke database Supabase selesai.")
    except Exception as db_err:
        logger.error(f"❌ Gagal menulis data ke Supabase: {str(db_err)}")

async def main_github_cron():
    logger.info("⏰ Memulai eksekusi mode: GITHUB ACTIONS CRON JOB")
    nim = os.getenv("SIAKAD_USER")
    password = os.getenv("SIAKAD_PASSWORD")
    
    if not nim or not password:
        logger.critical("❌ Kredensial SIAKAD_USER atau SIAKAD_PASSWORD kosong di Secrets!")
        return

    res = supabase.table("users").select("telegram_id").limit(1).execute()
    tg_id = res.data[0]['telegram_id'] if res.data else 12345
    await process_user_scrape(tg_id, nim, password)

if __name__ == "__main__":
    asyncio.run(main_github_cron())
