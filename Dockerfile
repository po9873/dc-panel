FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

ENV PANEL_PORT=8000
# 建议在 docker run / compose 里设置 PANEL_PASSWORD 初始化登录密码
EXPOSE 8000

VOLUME ["/data"]

CMD ["python", "-m", "app.main"]
