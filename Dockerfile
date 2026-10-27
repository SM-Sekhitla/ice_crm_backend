FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home crm
COPY --chown=crm:crm . .
USER crm
EXPOSE 5002
# Apply the schema before API startup and initial administrator creation.
CMD ["/bin/sh", "-c", "python -m alembic upgrade head && exec python -m app"]
