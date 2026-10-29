FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Dubai STATE_DIR=/state

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && useradd --uid 1001 --user-group --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /state && chown app:app /state

WORKDIR /app
COPY attachtext.py mailparse.py reader.py store.py server.py ./

USER app
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
