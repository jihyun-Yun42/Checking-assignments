# Vercel의 Python 런타임은 프로젝트 루트(또는 src/, app/ 폴더) 안에서
# app.py / index.py / server.py / main.py / wsgi.py / asgi.py 를
# 자동으로 엔트리포인트로 인식한다. 실제 Flask 앱은 webapp/app.py에
# 있으므로, 여기서는 그 app 객체를 그대로 가져오기만 한다.
#
# 이렇게 하면 pyproject.toml 없이도(=Vercel의 uv 기반 커스텀 엔트리포인트
# 설정 없이도) requirements.txt만으로 zero-config 배포가 가능해진다.
from webapp.app import app  # noqa: F401
