import os
import time
import asyncio
from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai.errors import APIError
from playwright.async_api import async_playwright

load_dotenv()
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# 1. ENGINE LLM DENGAN AUTOMATIC FAILOVER (Total 1k RPD)
def generate_content_with_failover(prompt: str) -> str:
    # Model Utama: Gemini 3.5 Flash Lite / Generasi Terbaru
    try:
        client = genai.Client(api_key=os.getenv("GEMINI_PRIMARY_KEY"))
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        return response.text
    except APIError as e:
        if e.code == 429: # Jika terkena limit kuota harian
            print("⚠️ API Utama Limit. Beralih ke Gemini 3.1 Flash / 1.5 Flash...")
            try:
                client_backup = genai.Client(api_key=os.getenv("GEMINI_BACKUP_KEY"))
                response = client_backup.models.generate_content(model='gemini-1.5-flash', contents=prompt)
                return response.text + "\n\n*(Responded by Backup Model)*"
            except Exception as err:
                return f"❌ Kedua API Key Limit: {str(err)}"
        return f"❌ AI Error: {str(e)}"

# 2. ENGINE PLAYWRIGHT (Optimal untuk VPS 1GB RAM)
async def run_playwright_scraper(nim: str, password: str) -> str:
    async with async_playwright() as p:
        # headless=True wajib agar tidak memakan RAM VPS Anda
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto("https://ryv.my.id", timeout=30000)
            await page.fill("input[name='username']", nim)
            await page.fill("input[name='password']", password)
            await asyncio.gather(
                page.wait_for_navigation(wait_until="networkidle"),
                page.click("button[type='submit']")
            )
            await page.goto("https://ryv.my.id", wait_until="domcontentloaded")
            return await page.locator("body").inner_text()
        except Exception as e:
            return f"Error: {str(e)}"
        finally:
            await browser.close()

# 3. FUNGSI UTAMA PENGOLAH DATA
async def process_user_scrape(user):
    tg_id = user['telegram_id']
    print(f"⚡ Memproses data untuk NIM: {user['nim']}")
    
    # Jalankan scraping
    raw_html_text = await run_playwright_scraper(user['nim'], user['encrypted_password'])
    
    if "Error" in raw_html_text:
        return
        
    # Simpan ke Cache Supabase
    supabase.table("siakad_cache").upsert({"telegram_id": tg_id, "raw_data": raw_html_text, "updated_at": "now()"}).execute()
    
    # Jika dipicu oleh Telegram Chat (bukan cron subuh), kirim jawaban langsung via LLM
    if user.get('status_request') == 'NEED_SCRAPE' and user.get('last_prompt'):
        prompt = f"Data SIAKAD:\n{raw_html_text}\n\nPertanyaan: {user['last_prompt']}"
        jawaban_ai = generate_content_with_failover(prompt)
        
        # Kirim HTTP POST ke Webhook API Telegram untuk membalas pesan user secara realtime
        import requests
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        requests.post(f"https://telegram.org{telegram_token}/sendMessage", json={
            "chat_id": tg_id, "text": jawaban_ai, "parse_mode": "Markdown"
        })

    # Kembalikan status ke IDLE
    supabase.table("users").update({"status_request": "IDLE"}).eq("telegram_id", tg_id).execute()

# LOOP UNTUK VPS (Daemon Mode)
async def main_vps_loop():
    print("🤖 VPS Monitor aktif, menunggu instruksi direct...")
    while True:
        # Cek apakah ada user yang meminta refresh data secara langsung dari Telegram
        res = supabase.table("users").select("*").eq("status_request", "NEED_SCRAPE").execute()
        if res.data:
            for user in res.data:
                await process_user_scrape(user)
        time.sleep(5) # Delay pengecekan database setiap 5 detik agar hemat resource VPS

# RUNNER UNTUK GITHUB ACTIONS (Cron Mode)
async def main_github_cron():
    print("⏰ GitHub Actions Cron dimulai...")
    res = supabase.table("users").select("*").execute()
    if res.data:
        for user in res.data:
            await process_user_scrape(user)

if __name__ == "__main__":
    # Mendeteksi apakah dijalankan di GitHub Actions atau VPS lokal
    if os.getenv("GITHUB_ACTIONS") == "true":
        asyncio.run(main_github_cron())
    else:
        asyncio.run(main_vps_loop())
