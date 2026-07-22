"""Gunicorn configuration for Render deployment.

Render sets WEB_CONCURRENCY which overrides --workers. We do NOT use
preload_app because libsql's Rust tokio runtime cannot survive fork() —
each worker must create its own connection after fork.

Threads are enabled so a single worker can handle concurrent requests
(important on free tier with 1 worker — without threads, a slow Turso
query on one request blocks all others).
"""
import os

bind = "0.0.0.0:10000"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "60"))
keepalive = 5
max_requests = 100  # Recycle workers periodically to prevent memory leaks
max_requests_jitter = 20

worker_class = "gthread"
accesslog = "-"
errorlog = "-"
capture_output = True
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
