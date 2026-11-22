# AgencyOS Assistant chat service. Build: docker build -t agencyos-assistant .
# Run API:  docker run --env-file .env -p 8090:8090 agencyos-assistant
# Run jobs: docker run --env-file .env agencyos-assistant python -m chat_service.jobs
# Migrate:  docker run --env-file .env agencyos-assistant alembic upgrade head
FROM python:3.12-slim AS build
WORKDIR /app
RUN pip install --no-cache-dir --upgrade pip
COPY pyproject.toml README.md ./
COPY chat_service ./chat_service
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CHAT_ENV=production
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /install /usr/local
COPY chat_service ./chat_service
COPY migrations ./migrations
COPY alembic.ini ./
USER app
EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2).status == 200 else 1)"
# Several workers per container; run several containers behind the load balancer (limits are shared via Redis).
CMD ["uvicorn", "chat_service.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090", \
     "--workers", "4", "--proxy-headers", "--timeout-keep-alive", "75"]
