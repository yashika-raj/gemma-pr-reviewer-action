FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src ./src

# GitHub overrides the working directory with the checked-out workspace, so the
# entrypoint uses an absolute path. That also keeps the repository under review
# off the import path.
ENTRYPOINT ["python", "/app/src/main.py"]
