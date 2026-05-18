FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/

EXPOSE 8080

CMD ["python", "-m", "agent.src.main"]
