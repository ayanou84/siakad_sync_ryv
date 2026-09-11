import os
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("SIAKAD_BOT")

load_dotenv()
try:
    supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
except Exception as e:
    logger.error(f"Gagal menginisialisasi Supabase Client: {str(e)}")

def generate_content_with_failover(prompt: str) -> str:
    try:
        logger.info("🤖 Mengirim prompt ke Gemini 3.5 Flash Lite...")
        client = genai.Client(api_key=os.getenv("GEMINI_PRIMARY_KEY"))
        response = client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=prompt
        )
        return response.text
    except APIError as e:
        if e.code == 429:
            logger.warning("⚠️ API Utama Limit! Mengalihkan ke Gemini 3.1 Flash Lite...")
            try:
                client_backup = genai.Client(api_key=os.getenv("GEMINI_BACKUP_KEY"))
                response = client_backup.models.generate_content(
                    model='gemini-3.1-flash-lite',
                    contents=prompt
                )
                return response.text + "\n\n*(Responded by Backup Model)*"
            except Exception as err:
                return f"❌ Kedua API Key Limit/Error: {str(err)}"
        else:
            return f"❌ AI Error: {str(e)}"
    except Exception as general_err:
        return f"❌ AI System Error: {str(general_err)}"

async def scrape_via_httpx(nim: str, password: str) -> str:
    logger.info("🚀 [ENGINE 1] Memulai HTTPX...")
    login_url = "https://siakad.mtu.ac.id/index.php/login"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": login_url
    }
    
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        try:
            payload = {"identity": nim, "password": password}
            res_login = await client.post(login_url, data=payload, headers=headers)
            soup = BeautifulSoup(res_login.text, "html.parser")
            raw_text = soup.get_text(separator="\n", strip=True)
            
            if len(raw_text) < 500 or "nama pengguna atau email" in raw_text.lower():
                raise Exception("Masih tertahan di laman login atau 403 Forbidden.")
            return raw_text
        except Exception as e:
            logger.warning(f"⚠️ [ENGINE 1 FAILED] HTTPX gagal: {str(e)}")
            return "FAILED"

async def run_playwright_scraper(nim: str, password: str) -> str:
    logger.info("🔄 [ENGINE 2] Memulai Playwright...")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            await page.goto("https://siakad.mtu.ac.id/index.php/login", timeout=30000, wait_until="domcontentloaded")
            await page.screenshot(path="debug_login.png")
            
            await page.fill("input[name='identity']", nim, timeout=10000)
            await page.fill("input[name='password']", password, timeout=10000)
            
            await asyncio.gather(
                page.wait_for_navigation(wait_until="networkidle"),
                page.click("button[type='submit']")
            )
            
            await page.screenshot(path="debug_after_login.png")
            raw_text = await page.locator("body").inner_text()
            await browser.close()
            return raw_text
        except Exception as e:
            try:
                await page.screenshot(path="error_playwright.png")
            except:
                pass
            logger.error(f"❌ [ENGINE 2 ERROR] Playwright gagal: {str(e)}")
            await browser.close()
            return f"Error Scraper: {str(e)}"

async def main():
    nim = os.getenv("SIAKAD_USER")
    password = os.getenv("SIAKAD_PASSWORD")
    if not nim or not password:
        logger.critical("❌ Kredensial SIAKAD tidak ditemukan di environment!")
        return

    raw_content = await scrape_via_httpx(nim, password)
    if raw_content == "FAILED":
        raw_content = await run_playwright_scraper(nim, password)
        
    if "Error Scraper" in raw_content:
        return

    res = supabase.table("users").select("telegram_id").limit(1).execute()
    tg_id = res.data[0]['telegram_id'] if res.data else 12345
    
    supabase.table("siakad_cache").upsert({"telegram_id": tg_id, "raw_data": raw_content, "updated_at": "now()"}).execute()
    logger.info("✅ Cache berhasil di-update ke Supabase.")

if __name__ == "__main__":
    asyncio.run(main())
