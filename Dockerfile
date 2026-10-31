FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
RUN useradd --create-home app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py oauth.py streams.py ./
USER app
EXPOSE 8080
CMD ["python", "-W", "ignore::DeprecationWarning", "app.py"]
