FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py fit.py store.py csvio.py ./
COPY templates ./templates

ENV DB_PATH=/data/trial.db \
    PORT=8000
VOLUME /data
EXPOSE 8000

CMD ["python", "app.py"]
