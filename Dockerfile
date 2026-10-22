FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir requests==2.32.3

COPY sync_daemon.py sync_nextcloud.py sync_paperless.py ./

CMD ["python", "-u", "sync_daemon.py"]