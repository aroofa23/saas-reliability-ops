FROM python:3.12-slim
RUN pip install --no-cache-dir pytest requests
WORKDIR /tests
COPY . .
CMD ["pytest", "-v", "--tb=short", "/tests"]
