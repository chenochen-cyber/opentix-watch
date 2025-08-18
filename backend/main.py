from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scraper import scrape_status as _scrape_status

app = FastAPI()

# CORS 設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Render 會先打 "/" 測試服務有沒有起來
@app.get("/")
def home():
    return {"message": "Service is running"}

# API：檢查票券狀態
@app.get("/api/status")
async def api_status(targets: str):
    # 這裡原本多了一個 headless=True，已經拿掉
    data = _scrape_status(targets)
    return data
