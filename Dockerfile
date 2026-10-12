FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md requirements.txt ./
COPY src ./src
RUN pip install --no-cache-dir --require-hashes -r requirements.txt && pip install --no-cache-dir --no-deps . && groupadd --gid 10001 bot && useradd --uid 10001 --gid 10001 --create-home bot && mkdir -p /app/data && chown bot:bot /app/data
COPY scripts/shadow_report.py ./scripts/shadow_report.py
USER bot
ENTRYPOINT ["python", "-m", "betbot.runtime"]
CMD ["betbot", "run"]
