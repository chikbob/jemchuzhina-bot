"""Portable WSGI entry point for Docker, Gunicorn and ordinary Python hosting."""

from pearl.web import create_app

app = create_app()
