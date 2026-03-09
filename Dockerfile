FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

# Evita bytecode e garante logs flushados
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && apt-get clean

RUN pip install --upgrade \
        pip==25.3 \
        setuptools==78.1.1 \
        virtualenv==20.26.6

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --upgrade playwright
RUN playwright install --with-deps chromium

COPY . .

RUN chown -R pwuser:pwuser /app
USER pwuser

EXPOSE 5000

CMD ["python", "app.py"]
