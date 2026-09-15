#!/usr/bin/env bash
# 오라클 클라우드 VM(Ubuntu)에서 최초 1회 실행하는 셋업 스크립트.
# 사용법: 프로젝트 폴더를 서버에 올린 뒤, 그 폴더 안에서 실행
#   bash deploy/setup_server.sh
set -e

sudo apt update
sudo apt install -y python3-venv python3-pip git

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Playwright + 크롬 브라우저 및 필요한 리눅스 라이브러리까지 한 번에 설치
playwright install --with-deps chromium

mkdir -p data

echo "설치 완료. 다음 순서로 진행하세요:"
echo "1) .env 파일 채우기 (cp .env.example .env 후 값 입력)"
echo "2) python -m src.naver_cafe_crawler login  (네이버 최초 로그인, 화면이 없는 서버라 --headful 대신 SSH -X 또는 로컬에서 로그인 후 세션파일만 서버로 옮기는 방법 권장)"
echo "3) sudo cp deploy/webapp.service /etc/systemd/system/"
echo "   sudo sed -i \"s#__PROJECT_DIR__#$(pwd)#g\" /etc/systemd/system/webapp.service"
echo "   sudo systemctl daemon-reload && sudo systemctl enable --now webapp"
echo "4) crontab -e 로 deploy/crontab.txt 내용 등록 (경로는 $(pwd) 으로 수정)"
