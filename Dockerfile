FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pearl ./pearl
RUN useradd --create-home bot && mkdir /app/data && chown bot:bot /app/data
USER bot
CMD ["python", "-m", "pearl"]
