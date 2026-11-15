FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt && python seed.py
ENV COOKIE_SECURE=1
CMD ["gunicorn","-w","1","--threads","4","-b","0.0.0.0:8000","app:app"]
