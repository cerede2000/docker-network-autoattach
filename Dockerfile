FROM python:3.12-slim

# Créer un user non-root
RUN useradd -u 1000 -m watcher

# Installer les dépendances Python
RUN pip install --no-cache-dir requests

WORKDIR /app

COPY watcher.py /app/watcher.py

USER watcher

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-u", "/app/watcher.py"]
