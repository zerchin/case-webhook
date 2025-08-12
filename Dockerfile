FROM python:latest
RUN apt-get update && apt install -y vim jq

WORKDIR /case_system

RUN pip install --no-cache-dir flask requests pymysql

COPY app.py /app/app.py

WORKDIR /app

EXPOSE $PORT

CMD ["python", "/app/app.py"]
