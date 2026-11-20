# 개인정보처리방침 / Privacy Policy

최종 수정: 2026-11-20 / Last updated: 2026-11-20

## 한국어

Kova2는 Windows용 백업 프로그램입니다. 개발자는 사용자의 파일, 백업 내용, 계정 정보를 수집하거나 개발자의 서버로 전송하지 않습니다. 개발자가 운영하는 서버는 없습니다.

**프로그램이 처리하는 정보**
- 백업 대상 파일과 백업 저장소는 사용자의 PC 또는 사용자가 지정한 저장 위치(외장 디스크, 네트워크 폴더, 사용자가 연결한 클라우드)에만 저장됩니다.
- 설정, 로그, 암호화 키는 사용자의 PC에 저장됩니다. 암호화 키는 Windows 사용자 계정 보호 기능(DPAPI)으로 보호됩니다.
- 업데이트 확인을 위해 프로그램은 GitHub의 릴리즈 정보를 조회합니다. 이때 GitHub는 일반적인 접속 정보(IP 주소 등)를 처리할 수 있으며, 이는 GitHub의 개인정보처리방침을 따릅니다.

**Google 드라이브 연결 (사용자가 연결을 선택한 경우에만)**
- 사용자가 "구글 드라이브 연결"을 선택하면 Google 로그인 화면에서 동의한 뒤, 프로그램은 `https://www.googleapis.com/auth/drive.file` 범위만 요청합니다. 이 범위는 Kova2가 만든 파일과 폴더에만 접근할 수 있게 하며, 사용자의 다른 드라이브 파일은 볼 수 없습니다.
- 로그인 토큰은 사용자의 PC에만 저장되며 개발자나 제3자에게 전송되지 않습니다.
- Google 드라이브에 올라가는 데이터는 사용자의 백업 파일뿐이며, 사용자 본인의 Google 계정에 저장됩니다.
- 사용자는 언제든 프로그램에서 연결을 해제하거나 Google 계정의 "보안 > 타사 앱 액세스"에서 권한을 취소할 수 있습니다.
- Kova2가 Google 사용자 데이터를 사용하는 목적은 사용자의 백업을 사용자의 Google 드라이브에 저장하고 복원하는 것 하나뿐이며, 광고·판매·제3자 공유·분석에 쓰지 않습니다. Google API 서비스 사용자 데이터 정책의 제한적 사용 요건을 준수합니다.

**문의**: https://github.com/kks3365550/Kova2/issues

## English

Kova2 is a backup program for Windows. The developer does not collect your files, backup contents, or account information, and does not send them to any developer-operated server. There is no developer-operated server.

**What the program handles**
- Files being backed up and the backup repository are stored only on your PC or on storage locations you choose (external disk, network folder, or a cloud service you connect).
- Settings, logs, and encryption keys are stored on your PC. Encryption keys are protected with Windows DPAPI.
- To check for updates, the program queries GitHub release information. GitHub may process ordinary connection data (such as your IP address) under GitHub's own privacy policy.

**Google Drive connection (only if you choose to connect it)**
- If you choose "Connect Google Drive", the program requests only the `https://www.googleapis.com/auth/drive.file` scope after you consent on the Google sign-in screen. This scope allows access only to files and folders created by Kova2; it cannot see your other Drive files.
- The sign-in token is stored only on your PC and is not sent to the developer or any third party.
- The only data uploaded to Google Drive is your backup data, stored in your own Google account.
- You can disconnect at any time in the program, or revoke access in your Google Account under Security > Third-party access.
- Kova2 uses Google user data solely to store and restore your backups in your own Google Drive. It does not use the data for advertising, sale, sharing with third parties, or analytics. Its use of Google user data adheres to the Google API Services User Data Policy, including the Limited Use requirements.

**Contact**: https://github.com/kks3365550/Kova2/issues
