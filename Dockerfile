# Basis-Image
FROM python:3.11-slim

# Arbeitsverzeichnis
WORKDIR /app

# System-Pakete (für cryptography + lxml etc.)
RUN apt-get update && apt-get install -y \
    build-essential \
    libffi-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    && apt-get clean

# Requirements kopieren und installieren
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# App-Dateien kopieren
COPY . .

# Port (Cloud Run)
ENV PORT=8080

# Flask starten
CMD ["python", "app.py"]
