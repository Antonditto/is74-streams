# Собирать с контекстом = корень репозитория:
#   docker build -f homeassistant-addon/Dockerfile -t is74-streams .
FROM python:3.12-slim

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY homeassistant-addon/run.sh ./run.sh
RUN chmod +x ./run.sh

EXPOSE 8090

CMD ["./run.sh"]
