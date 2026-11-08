FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

# 把构建时间写进镜像：面板拿它和远端 latest 的构建时间比较来判断有没有新版。
# 不能只看容器启动时间——容器重启后启动时间会晚于镜像构建时间，会把
# 「确实有新镜像」误判成「没有更新」。
ARG BUILD_TIME=unknown
RUN echo "$BUILD_TIME" > /app/BUILD_TIME

ENV PANEL_PORT=8000
# 建议在 docker run / compose 里设置 PANEL_PASSWORD 初始化登录密码
EXPOSE 8000

VOLUME ["/data"]

CMD ["python", "-m", "app.main"]
