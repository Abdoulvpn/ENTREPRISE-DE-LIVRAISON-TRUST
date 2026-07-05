FROM python:3.11-slim
WORKDIR /app

# Install build deps and runtime deps
COPY requirements.txt ./
RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . /app

ENV FLASK_ENV=production
ENV PORT=5000

EXPOSE 5000

CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
