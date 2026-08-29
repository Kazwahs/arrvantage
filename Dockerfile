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

# Run as a non-root user rather than the default root - limits what a
# compromised process could do inside the container. UID/GID 1000 is
# the default here, but the app can genuinely run as any non-root
# UID/GID at runtime (e.g. `docker run --user 5000:5000`) - the chmod
# below makes the app code readable/executable by any user, not just
# the specific one baked into this image, since ownership alone
# (chown) only helps the exact UID it names.
#
# IMPORTANT if you're updating an existing deployment: the container
# has been running as root until now, so your existing data directory
# on TrueNAS storage is likely root-owned. After this change, you may
# need to fix its ownership so the container can still write
# config.json - from TrueNAS Shell:
#   chown -R 1000:1000 /mnt/yourpool/apps/arrvantage-data
# (adjust the path to your actual dataset). If you skip this and
# config saves start failing, this permission mismatch is why.
RUN groupadd -g 1000 appuser && useradd -u 1000 -g appuser -M appuser \
    && chown -R appuser:appuser /app \
    && chmod -R o+rX /app
USER appuser

# EXPOSE is purely documentary (it doesn't restrict what a container
# can actually bind to at runtime) - kept at the default here since
# that's what most deployments will use, but PORT below can override it.
EXPOSE 5000
ENV PORT=5000

# gunicorn imports the `app` object from app.py directly - this never
# runs app.py's own `if __name__ == "__main__"` block, so the Flask
# dev server (and its debug mode) is never used in the container.
# --timeout raised from gunicorn's 30s default: page loads that fan out
# to external metadata APIs (TMDB/TVDB/Fanart.tv) for many library
# items at once can genuinely take a while for a large library, even
# running those lookups in parallel.
#
# --workers 1 is deliberate, not a placeholder to raise later: CONFIG
# and USERS are loaded into memory once per worker process at startup,
# so multiple workers each get their own separate copy - fine for
# read-only data, but a real bug for anything that gets *written* at
# runtime (like completing the setup wizard). With 2 workers, whichever
# one handles the setup POST updates its own in-memory USERS, but its
# sibling never finds out, since it only knows the file changed on
# disk, not what a different process now has in memory - so /setup and
# /login can each land on a different worker with a different answer
# for "do any users exist yet", bouncing between them indefinitely on
# a fresh install. A single worker makes that whole class of bug
# impossible, and matches what this app actually is: a personal,
# single-user tool with no database, where the concurrency multiple
# workers exist for was never really needed in the first place.
#
# Runs through a shell so ${PORT} actually gets substituted (exec-form
# CMD, i.e. CMD ["gunicorn", ...], does NOT expand env vars) - `exec`
# inside that shell replaces the shell process with gunicorn itself,
# so signals like SIGTERM reach gunicorn directly for a clean shutdown
# instead of being absorbed by an intermediate shell process.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --timeout 90 app:app"]