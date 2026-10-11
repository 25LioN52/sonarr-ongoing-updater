FROM python:3.14-alpine

WORKDIR /app
COPY ongoing_updater.py /app/ongoing_updater.py

ENV PYTHONUNBUFFERED=1 \
    STATE_FILE=/data/state.json

VOLUME /data

ENTRYPOINT ["python", "/app/ongoing_updater.py"]
