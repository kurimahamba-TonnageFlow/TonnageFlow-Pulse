FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home pulse
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pulse-browsers
RUN python -m playwright install --with-deps chromium && chmod -R a+rX /opt/pulse-browsers
COPY src ./src
USER pulse
EXPOSE 8000
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
