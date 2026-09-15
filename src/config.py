import os
from dotenv import load_dotenv

load_dotenv()

GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "config/google_service_account.json")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "소리튠_과제체킹_명단")

_sa_content = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT")
if _sa_content and not os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
    os.makedirs(os.path.dirname(GOOGLE_SERVICE_ACCOUNT_JSON) or ".", exist_ok=True)
    with open(GOOGLE_SERVICE_ACCOUNT_JSON, "w", encoding="utf-8") as f:
        f.write(_sa_content)

UPLOAD_PASSWORD = os.getenv("UPLOAD_PASSWORD", "changeme")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Seoul")

DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "https://boot.soritune.com")

HAMUMMAL_TEST_MODE = os.getenv("HAMUMMAL_TEST_MODE", "").strip().lower() in ("1", "true", "yes")

MISSION_TYPE_IDS = {
    "zoom_daily": 1,
    "daily_mission": 7,
    "inner33": 2,
    "speak_mission": 3,
}

OUT_MEMBER_STATUS = "out_of_group_management"
OUT_SCORE_THRESHOLD = -25
