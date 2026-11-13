FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir "uv>=0.5,<1"

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Score OCR is optional and large (onnxruntime, numpy, pillow). Build with
# `--build-arg INSTALL_OCR=true` to read report screenshots, then run `/ocr mode`.
ARG INSTALL_OCR=false
RUN if [ "$INSTALL_OCR" = "true" ]; then \
        uv sync --frozen --no-dev --group ocr --no-install-project; \
    else \
        uv sync --frozen --no-dev --no-install-project; \
    fi

COPY . .
RUN DEBUG=true python manage.py collectstatic --noinput

RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-"]
