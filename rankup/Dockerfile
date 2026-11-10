FROM python:3.12-slim
ARG BUILD_ARCH
ARG BUILD_VERSION=0.9.17
LABEL io.hass.type="app" io.hass.arch="${BUILD_ARCH}" io.hass.version="${BUILD_VERSION}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app /app
EXPOSE 8095
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8095", "--proxy-headers"]
