FROM python:3.9-slim

WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app.py .
COPY database.py .
COPY templates/ templates/
COPY static/ static/

# 创建非root用户
RUN groupadd -r webhook && useradd -r -g webhook webhook
USER webhook

# 设置环境变量
ENV PORT=5000

# 暴露端口
EXPOSE 5000

# 启动应用
CMD ["python", "app.py"]
