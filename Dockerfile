# Python's official slim image - small, but has everything needed to
# install Flask/gunicorn without extra build tools.
FROM python:3.12-slim

# Without this, Python buffers stdout when it's not attached to a real
# terminal (always true in a container) - meaning print() statements
# (like the diagnostic logging in app.py) can sit invisible in a buffer
# instead of showing up in `docker logs` right away.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first, separately from app code - Docker caches
# this layer, so rebuilding after a code change doesn't reinstall
# everything from scratch, only when requirements.txt itself changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only what the app actually needs to run. config.json is
# deliberately NOT copied here - it's created fresh (or read from a
# mounted volume) at runtime, so your API keys and password hashes
# never end up baked into the image itself.
COPY app.py .
COPY templates/ templates/
COPY static/ static/

# Where config.json will live inside the container - mount a volume
# here (see docker-compose.yml) so it survives image rebuilds/updates.
ENV CONFIG_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 5000

# gunicorn imports the `app` object from app.py directly - this never
# runs app.py's own `if __name__ == "__main__"` block, so the Flask
# dev server (and its debug mode) is never used in the container.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]