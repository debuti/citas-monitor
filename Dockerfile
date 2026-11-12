FROM python:3.13-slim
# tzdata: la hora local (Europe/Madrid) de los mensajes.
RUN pip install --no-cache-dir tzdata
WORKDIR /app
COPY monitor.py catalog.py bot.py ./
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data TZ=Europe/Madrid
VOLUME /data
CMD ["python", "bot.py"]
