# Playwright's image ships Chromium and its system libraries — needed for the LabSuit auto-update robot.
# Keep the tag in step with the playwright version in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt
COPY . .
RUN python3 manage.py collectstatic --noinput

CMD python3 manage.py migrate --noinput && \
    gunicorn labfund.wsgi --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 120
