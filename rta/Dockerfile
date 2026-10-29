FROM python:3.12-slim

RUN pip install --no-cache-dir playwright \
 && playwright install --with-deps chromium

RUN pip install --no-cache-dir fastapi==0.161.1 uvicorn==0.53.0

WORKDIR /work
COPY search_trips.py recon4.py book.py server.py ./

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
