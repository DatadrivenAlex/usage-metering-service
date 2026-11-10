FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home app && mkdir data && chown app:app data
USER app
EXPOSE 8000
CMD ["uvicorn", "metering.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
