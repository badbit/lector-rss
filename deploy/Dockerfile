FROM python:3.12-slim

# libxml2/libxslt los necesita trafilatura para la extracción de texto completo.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 libxslt1.1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY packages/core /app/packages/core
COPY packages/hub  /app/packages/hub
RUN pip install --no-cache-dir /app/packages/core /app/packages/hub

VOLUME ["/data"]
EXPOSE 8787
CMD ["rsshub", "--host", "0.0.0.0", "--port", "8787"]
