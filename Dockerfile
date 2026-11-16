FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY configs ./configs
RUN pip install --no-cache-dir .
# Credentials are NEVER baked into the image: pass QISKIT_IBM_TOKEN / QISKIT_IBM_INSTANCE at run time.
ENTRYPOINT ["qportfolio"]
CMD ["--help"]
