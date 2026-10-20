import os
import sys
import json
import logging

sys.path.insert(0, r"c:\Users\kksjmj\Desktop\ai\백업시스템")
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("GDriveAuth")

CLIENT_SECRET_FILE = r"C:\Users\kksjmj\Desktop\ai\백업시스템\client_secret_333866351629-873p5lf4sl3g8ekac0rjjd8jvpbana49.apps.googleusercontent.com.json"
BASE_DATA_DIR = r"c:\Users\kksjmj\Desktop\ai\백업시스템\data"
TOKEN_PATH = os.path.join(BASE_DATA_DIR, "gdrive_oauth_token.json")
SCOPES = ["https://www.googleapis.com/auth/drive"]

def main():
    if not os.path.exists(CLIENT_SECRET_FILE):
        print(f"❌ 클라이언트 시크릿 파일을 찾을 수 없습니다: {CLIENT_SECRET_FILE}")
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    print("=" * 65)
    print("🌐 Google Drive 개인 계정 1회성 원클릭 인증 도구")
    print("=" * 65)
    print("1. 구글 인증 세션을 시작합니다...")
    
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, scopes=SCOPES)
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

    print("\n2. 웹 브라우저를 직접 엽니다...")
    print(f"👉 만약 브라우저가 안 뜨면 아래 URL을 복사하여 브라우저에 붙여넣으세요:\n{auth_url}\n")
    
    # Windows Explorer shell start로 활성 데스크톱 브라우저 강제 팝업
    os.system(f'start "" "{auth_url}"')

    print("3. 구글 로그인 및 권한 허용 대기 중 (인증 완료 시 자동 종료됩니다)...")
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    os.makedirs(BASE_DATA_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    # 설치 디렉터리 및 배포 폴더에도 토큰 동기화
    for target_dir in [r"D:\백업시스템_설치용\data", r"C:\Users\kksjmj\AppData\Local\Programs\백업시스템\data"]:
        try:
            os.makedirs(target_dir, exist_ok=True)
            with open(os.path.join(target_dir, "gdrive_oauth_token.json"), "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        except Exception:
            pass

    print("\n" + "=" * 65)
    print(f"🎉 인증 완료! 토큰이 성공적으로 저장되었습니다:\n  -> {TOKEN_PATH}")
    print("이제 백업시스템 웹 대시보드에서 구글 드라이브 클라우드 백업이 정상 가동됩니다.")
    print("=" * 65)
    return 0

if __name__ == "__main__":
    sys.exit(main())
