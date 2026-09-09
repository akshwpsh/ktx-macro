# 텔레그램 봇(bot.py) 전용 이미지. Streamlit 은 넣지 않는다.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul \
    KTX_JOBS_FILE=/data/jobs.json

# git: korail-mobile-api 를 GitHub 에서 받는다. tzdata: 로그·구입기한 표시를 한국 시간으로.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-bot.txt .
RUN pip install --no-cache-dir -r requirements-bot.txt

COPY ktx_macro.py bot.py ./

# 등록한 구간(jobs.json)은 여기 둔다. 컨테이너를 갈아엎어도 남도록 볼륨으로 마운트한다.
VOLUME /data

CMD ["python", "bot.py"]
