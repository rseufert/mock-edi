FROM python:3.12-slim
WORKDIR /app
COPY mockedi ./mockedi
COPY pyproject.toml README.md LICENSE ./
RUN pip install --no-cache-dir .
EXPOSE 8080
ENTRYPOINT ["mock-edi", "--host", "0.0.0.0", "--port", "8080"]
