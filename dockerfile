FROM python:3.14.6-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mian.py bot_instance.py config.py database.py ./
COPY handlers/ ./handlers/
CMD ["python", "-u", "mian.py"]
